"""Direct-provider account selection and shared availability state.

The caller owns provider fallback and calls ``next_account`` again after every failed
attempt. This module does not manage OmniRoute's commodity account pool.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from research_engine.providers.base import Capability, ErrorKind, ProviderError, Result
from research_engine.storage.db import Account, Database, ProviderRow, utcnow


SELECTION_MODES = frozenset({"priority", "round_robin", "quota_aware"})
# A remaining-quota value is not a forecast: only recent observations of the
# same unit, scope and *known* reset window are comparable.
QUOTA_OBSERVATION_TTL = timedelta(minutes=5)
_SHARED_REASONS = frozenset({"rate_limited", "exhausted", "retry_estimate:rate_limited",
                            "retry_estimate:exhausted", "quota_retry_estimate"})


@dataclass(frozen=True)
class QuotaObservation:
    remaining: float
    units: str
    scope: str
    observed_at: datetime
    reset_at: datetime | None = None


@dataclass(frozen=True)
class AccountChoice:
    id: int
    quota_group: str | None


def selection_mode(options: dict[str, Any] | None) -> str:
    """Reject bad saved config instead of silently spending through another mode."""
    mode = (options or {}).get("selection_mode", "priority")
    if mode not in SELECTION_MODES:
        raise ValueError("Invalid provider selection_mode")
    return mode


def _active(when: datetime | None, now: datetime) -> bool:
    return when is not None and when > now


def _current_observation(account: Account, now: datetime) -> QuotaObservation | None:
    remaining = account.quota_remaining
    observed_at = getattr(account, "quota_observed_at", None)
    units = getattr(account, "quota_units", None)
    scope = getattr(account, "quota_scope", None)
    reset_at = account.quota_reset_at
    if (remaining is None or not math.isfinite(remaining) or not units or not scope
            or observed_at is None or observed_at > now or now - observed_at > QUOTA_OBSERVATION_TTL
            or not _active(reset_at, now)):
        return None
    return QuotaObservation(remaining, units, scope, observed_at, reset_at)


class AccountSelector:
    """Single-process selector; DB state is loaded fresh on each retry.

    ``next_account`` and ``eligible`` are synchronous: run them in a worker
    thread from the async router. Selection rotation is protected by a small
    process-local lock and does not persist a scheduler or reserve an account.
    """

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._turn: dict[tuple[str, int], int] = {}

    def next_account(
        self, provider: str, capability: Capability, *, excluded: set[int] | frozenset[int] = frozenset(),
    ) -> tuple[AccountChoice | None, str]:
        """Pick one eligible account, excluding accounts already tried on this call."""
        with self._lock, self.db.session() as session:
            row = session.get(ProviderRow, provider)
            if row is None or not row.enabled:
                return None, "provider disabled"
            mode = selection_mode(row.options)
            accounts = list(session.scalars(select(Account).where(Account.provider == provider)
                                            .order_by(Account.priority, Account.id)))
            now = utcnow()
            eligible = [account for account in accounts if account.id not in excluded
                        and self._eligible(account, capability, accounts, now)]
            if not eligible:
                return None, "no usable account"
            if mode == "round_robin":
                tier = [account for account in eligible if account.priority == eligible[0].priority]
                key = (provider, tier[0].priority)
                # Keep the cursor anchored to the stable tier, even when a
                # failed account is excluded for this request.
                all_tier_ids = [member.id for member in accounts if member.priority == tier[0].priority]
                turn = self._turn.get(key, 0)
                by_id = {member.id: member for member in tier}
                ordered_ids = all_tier_ids[turn:] + all_tier_ids[:turn]
                account = next((by_id[id_] for id_ in ordered_ids if id_ in by_id), tier[0])
                self._turn[key] = (all_tier_ids.index(account.id) + 1) % len(all_tier_ids)
            elif mode == "quota_aware":
                observations = [_current_observation(account, now) for account in eligible]
                comparable = all(observations) and len({
                    (item.units, item.scope, item.reset_at) for item in observations
                }) == 1
                if comparable:
                    account = max(zip(eligible, observations, strict=True),
                                  key=lambda pair: (pair[1].remaining, -pair[0].priority, -pair[0].id))[0]
                else:
                    account = eligible[0]
            else:
                account = eligible[0]
            return AccountChoice(account.id, account.quota_group), ""

    def eligible(self, provider: str, capability: Capability, account_id: int) -> bool:
        """Recheck immediately before a call, after another account can change the group."""
        with self.db.session() as session:
            row = session.get(ProviderRow, provider)
            if row is None or not row.enabled:
                return False
            account = session.get(Account, account_id)
            if account is None or account.provider != provider:
                return False
            accounts = list(session.scalars(select(Account).where(Account.provider == provider)))
            return self._eligible(account, capability, accounts, utcnow())

    @staticmethod
    def _eligible(account: Account, capability: Capability, accounts: list[Account], now: datetime) -> bool:
        if (not account.enabled or account.credential != "ok" or _active(account.cooldown_until, now)
                or capability.value in (account.blocked_capabilities or {})):
            return False
        if not account.quota_group:
            return True
        for member in accounts:
            if member.quota_group != account.quota_group:
                continue
            if _active(member.cooldown_until, now) and member.cooldown_reason in _SHARED_REASONS:
                return False
            observation = _current_observation(member, now)
            if observation is not None and observation.remaining <= 0:
                return False
        return True

    def record_availability(
        self, account_id: int, capability: Capability, *, error: ProviderError | None = None,
        result: Result | None = None, observation: QuotaObservation | None = None,
    ) -> None:
        """Persist typed failure and quota evidence; never invent a billing reset.

        Only explicit, comparable quota observations may drive quota-aware ranking.
        ``result.quota_remaining`` alone has no unit/window and is not treated as
        a comparable observation. Unknown-reset exhaustion sets a bounded retry
        estimate as a cooldown, *not* as ``quota_reset_at``.
        """
        if observation is not None and (
            not math.isfinite(observation.remaining) or not observation.units or not observation.scope
            or observation.observed_at.tzinfo is None
            or (observation.reset_at is not None and observation.reset_at.tzinfo is None)
        ):
            raise ValueError("Invalid quota observation")
        if error is not None and error.reset_at is not None and error.reset_at.tzinfo is None:
            raise ValueError("Provider quota reset must be timezone-aware")
        with self.db.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                return
            now = utcnow()
            if observation is not None:
                members = ([account] if not account.quota_group else list(session.scalars(
                    select(Account).where(Account.provider == account.provider,
                                          Account.quota_group == account.quota_group)
                )))
                for member in members:
                    member.quota_remaining = observation.remaining
                    member.quota_units = observation.units
                    member.quota_scope = observation.scope
                    member.quota_observed_at = observation.observed_at
                    member.quota_reset_at = observation.reset_at
            if error is None:
                account.credential = "ok"
                account.transient_failures = 0
                if result is not None and result.quota_remaining is not None:
                    if observation is None:
                        # Bare adapter counts have no scope/units/time: retain
                        # the raw value for the operator, not for ranking.
                        reset_at = None
                        if result.quota_reset_at:
                            reset_at = datetime.fromisoformat(result.quota_reset_at.replace("Z", "+00:00"))
                            if reset_at.tzinfo is None:
                                raise ValueError("Provider quota reset must be timezone-aware")
                        members = ([account] if not account.quota_group else list(session.scalars(
                            select(Account).where(Account.provider == account.provider,
                                                  Account.quota_group == account.quota_group)
                        )))
                        for member in members:
                            member.quota_remaining = result.quota_remaining
                            member.quota_observed_at = None
                            member.quota_units = member.quota_scope = None
                            member.quota_reset_at = reset_at
                    if result.quota_remaining <= 0:
                        until = account.quota_reset_at or now + timedelta(seconds=300)
                        reason = "exhausted" if account.quota_reset_at else "quota_retry_estimate"
                        self._cool_group(session, account, until, reason)
                elif observation is not None and observation.remaining <= 0:
                    until = observation.reset_at or now + timedelta(seconds=300)
                    reason = "exhausted" if observation.reset_at else "quota_retry_estimate"
                    self._cool_group(session, account, until, reason)
                return
            if error.kind == ErrorKind.AUTH:
                account.credential = "needs_auth"
            elif error.kind == ErrorKind.PLAN and error.block_capability:
                account.blocked_capabilities = {
                    **(account.blocked_capabilities or {}), capability.value: str(error)[:300],
                }
            elif error.kind in {ErrorKind.RATE_LIMITED, ErrorKind.EXHAUSTED}:
                seconds = error.retry_after
                known_reset = error.reset_at
                if known_reset is not None:
                    until = known_reset.astimezone(UTC)
                    reason = error.kind.value
                elif seconds is not None and math.isfinite(seconds) and seconds >= 0:
                    until = now + timedelta(seconds=min(seconds, 300))
                    reason = f"retry_estimate:{error.kind.value}"
                else:
                    until = now + timedelta(seconds=60 if error.kind == ErrorKind.RATE_LIMITED else 300)
                    reason = f"retry_estimate:{error.kind.value}"
                if error.kind == ErrorKind.EXHAUSTED and observation is None:
                    members = ([account] if not account.quota_group else list(session.scalars(
                        select(Account).where(Account.provider == account.provider,
                                              Account.quota_group == account.quota_group)
                    )))
                    for member in members:
                        member.quota_remaining = 0
                        member.quota_reset_at = known_reset
                        member.quota_units = member.quota_scope = member.quota_observed_at = None
                self._cool_group(session, account, until, reason)
            elif error.kind == ErrorKind.TRANSIENT:
                account.transient_failures += 1
                if account.transient_failures >= 3:
                    account.cooldown_until = now + timedelta(
                        seconds=min(300, 30 * 2 ** min(account.transient_failures - 3, 4))
                    )
                    account.cooldown_reason = "errors"

    @staticmethod
    def _cool_group(session: Any, account: Account, until: datetime, reason: str) -> None:
        members = ([account] if not account.quota_group else list(session.scalars(
            select(Account).where(Account.provider == account.provider,
                                  Account.quota_group == account.quota_group)
        )))
        for member in members:
            if not _active(member.cooldown_until, until):
                member.cooldown_until, member.cooldown_reason = until, reason
