"""Direct specialist account selection; DB fixtures make no provider calls."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from research_engine.providers.base import Capability, ErrorKind, ProviderError, Result
from research_engine.router.accounts import AccountSelector, QuotaObservation, selection_mode
from research_engine.storage.db import Account, Database, ProviderRow, utcnow


@pytest.fixture
def pool(tmp_path):
    db = Database(tmp_path / "accounts.db")
    db.initialize()
    with db.session() as session:
        session.add(ProviderRow(name="specialist", options={"selection_mode": "priority"},
                                capabilities=[Capability.PAPER_SEARCH.value]))
        session.add(ProviderRow(name="other", options={}, capabilities=[Capability.PAPER_SEARCH.value]))
        session.flush()
        session.add_all([
            Account(provider="specialist", credential="ok", priority=0, quota_group="shared"),
            Account(provider="specialist", credential="ok", priority=0, quota_group="shared"),
            Account(provider="specialist", credential="ok", priority=1),
            Account(provider="other", credential="ok", quota_group="shared"),
        ])
    selector = AccountSelector(db)
    try:
        yield db, selector
    finally:
        db.close()


def _ids(db):
    with db.session() as session:
        return [row.id for row in session.scalars(
            select(Account).where(Account.provider == "specialist").order_by(Account.id)
        )]


def _mode(db, mode):
    with db.session() as session:
        session.get(ProviderRow, "specialist").options = {"selection_mode": mode}


def test_priority_failover_filters_all_ineligible_states(pool):
    db, selector = pool
    first, second, third = _ids(db)
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
    assert selector.next_account("specialist", Capability.PAPER_SEARCH, excluded={first})[0].id == second
    with db.session() as session:
        session.get(Account, first).blocked_capabilities = {"paper_search": "plan"}
        session.get(Account, second).cooldown_until = utcnow() + timedelta(minutes=1)
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == third
    with db.session() as session:
        session.get(Account, third).credential = "needs_auth"
    assert selector.next_account("specialist", Capability.PAPER_SEARCH) == (None, "no usable account")
    with db.session() as session:
        session.get(ProviderRow, "specialist").enabled = False
    assert selector.next_account("specialist", Capability.PAPER_SEARCH) == (None, "provider disabled")


def test_round_robin_rotates_only_best_priority_tier_and_handles_exclusion(pool):
    db, selector = pool
    first, second, third = _ids(db)
    _mode(db, "round_robin")
    assert [selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id for _ in range(4)] == [
        first, second, first, second,
    ]
    assert selector.next_account("specialist", Capability.PAPER_SEARCH, excluded={first})[0].id == second
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
    with db.session() as session:
        session.get(Account, first).enabled = False
        session.get(Account, second).enabled = False
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == third


def test_round_robin_concurrent_picks_remain_balanced(pool):
    db, selector = pool
    first, second, _ = _ids(db)
    _mode(db, "round_robin")
    with ThreadPoolExecutor(max_workers=10) as workers:
        picks = list(workers.map(lambda _: selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id,
                                 range(40)))
    assert picks.count(first) == picks.count(second) == 20


def test_quota_aware_uses_only_fresh_comparable_observations(pool):
    db, selector = pool
    first, second, third = _ids(db)
    _mode(db, "quota_aware")
    now = utcnow()
    reset_at = now + timedelta(hours=1)
    # An unknown observation in the pool prevents selective quota comparison.
    selector.record_availability(first, Capability.PAPER_SEARCH, observation=QuotaObservation(
        3, "requests", "monthly", now, reset_at,
    ))
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
    selector.record_availability(third, Capability.PAPER_SEARCH, observation=QuotaObservation(
        5, "requests", "monthly", now, reset_at,
    ))
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == third
    with db.session() as session:
        assert session.get(Account, second).quota_remaining == 3
    selector.record_availability(third, Capability.PAPER_SEARCH, observation=QuotaObservation(
        99, "tokens", "monthly", now, reset_at,
    ))
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
    selector.record_availability(third, Capability.PAPER_SEARCH, observation=QuotaObservation(
        99, "requests", "monthly", now - timedelta(minutes=6), reset_at,
    ))
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
    assert selector.next_account("specialist", Capability.PAPER_SEARCH, excluded={first})[0].id == second


def test_zero_quota_result_propagates_shared_eligibility_without_invented_reset(pool):
    db, selector = pool
    first, second, third = _ids(db)
    selector.record_availability(first, Capability.PAPER_SEARCH, result=Result(quota_remaining=0))
    assert selector.eligible("specialist", Capability.PAPER_SEARCH, second) is False
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == third
    with db.session() as session:
        for account_id in (first, second):
            account = session.get(Account, account_id)
            assert account.quota_reset_at is None
            assert account.cooldown_reason == "quota_retry_estimate"


def test_rate_exhaustion_shared_only_with_same_provider_and_unknown_reset(pool):
    db, selector = pool
    first, second, third = _ids(db)
    selector.record_availability(first, Capability.PAPER_SEARCH,
                                 error=ProviderError(ErrorKind.EXHAUSTED, "limit"))
    assert selector.eligible("specialist", Capability.PAPER_SEARCH, second) is False
    assert selector.eligible("specialist", Capability.PAPER_SEARCH, third) is True
    with db.session() as session:
        other = session.scalar(select(Account).where(Account.provider == "other"))
        assert other.cooldown_until is None
        account = session.get(Account, first)
        assert account.quota_reset_at is None
        assert account.cooldown_reason == "retry_estimate:exhausted"


def test_known_reset_persists_but_auth_plan_and_internal_fault_are_not_shared(pool):
    db, selector = pool
    first, second, _ = _ids(db)
    reset_at = utcnow() + timedelta(minutes=12)
    selector.record_availability(first, Capability.PAPER_SEARCH,
                                 error=ProviderError(ErrorKind.RATE_LIMITED, "slow", reset_at=reset_at))
    with db.session() as session:
        assert session.get(Account, second).cooldown_until == reset_at
    with db.session() as session:
        for id_ in (first, second):
            session.get(Account, id_).cooldown_until = None
    selector.record_availability(first, Capability.PAPER_SEARCH,
                                 error=ProviderError(ErrorKind.AUTH, "bad token"))
    assert selector.eligible("specialist", Capability.PAPER_SEARCH, second)
    selector.record_availability(second, Capability.PAPER_SEARCH,
                                 error=ProviderError(ErrorKind.PLAN, "not entitled"))
    with db.session() as session:
        account = session.get(Account, second)
        assert account.blocked_capabilities == {"paper_search": "plan: not entitled"}
        assert account.credential == "ok"
        assert session.get(Account, first).credential == "needs_auth"
    # Unexpected exceptions are handled by the router, not translated to account failures here.
    with db.session() as session:
        assert session.get(Account, second).transient_failures == 0


def test_bad_mode_rejected_and_legacy_quota_does_not_rank(pool):
    db, selector = pool
    first, second, _ = _ids(db)
    with pytest.raises(ValueError, match="selection_mode"):
        selection_mode({"selection_mode": "invalid"})
    _mode(db, "quota_aware")
    with db.session() as session:
        session.get(Account, second).quota_remaining = 1000  # no units/scope/observation time
    assert selector.next_account("specialist", Capability.PAPER_SEARCH)[0].id == first
