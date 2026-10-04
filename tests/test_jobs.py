"""Fixture checks for persistence, isolation and cancellation, with no live APIs."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import select

from research_engine.jobs.runner import JobRunner
from research_engine.providers.base import CallContext, Capability, ErrorKind, JobUpdate, Provider, ProviderError
from research_engine.server.schemas import Hit, Result
from research_engine.storage.db import Account, ClientToken, Database, Job, create_client_token


class FakeProvider(Provider):
    name = "async_fixture"
    capabilities = frozenset({Capability.SITE_CRAWL})
    keyless = True
    poll_interval_s = 0.01

    def __init__(self):
        self.updates: deque[JobUpdate | Exception] = deque()
        self.polls: list[tuple[str, int]] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.cancelled_polls = 0
        self.starts = 0
        self.cancellations: list[str] = []
        self.cancel_response: bool | None | Exception = True
        self.cancel_gate: asyncio.Event | None = None
        self.complete_when_cancelled = False

    async def start(self, cap, req, ctx):
        self.starts += 1
        raise AssertionError("The job runner must never launch upstream work")

    async def poll(self, ctx, ref):
        self.polls.append((ref, ctx.account_id))
        gate = self.gates.get(len(self.polls))
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancelled_polls += 1
                if self.complete_when_cancelled:
                    return JobUpdate("completed", Result(urls=["https://example.org/late"]))
                raise
        update = self.updates.popleft() if self.updates else JobUpdate("running", poll_after_s=0.01)
        if isinstance(update, Exception):
            raise update
        return update

    async def cancel(self, ctx, ref):
        self.cancellations.append(ref)
        if self.cancel_gate is not None:
            await self.cancel_gate.wait()
        if isinstance(self.cancel_response, Exception):
            raise self.cancel_response
        return self.cancel_response


@dataclass
class State:
    db: Database
    provider: FakeProvider
    account: int
    owner: int
    other: int
    registry: dict[str, Provider]

    async def context(self, provider: str, account: int, timeout_s: float) -> CallContext:
        assert provider == self.provider.name
        assert account == self.account
        return CallContext(
            client=None, credentials={}, account_id=account, provider=provider,
            deadline=time.monotonic() + timeout_s,
        )

    def runner(self, finalizer=None) -> JobRunner:
        return JobRunner(self.db, self.registry, self.context, finalizer)

    async def create(self, runner: JobRunner):
        return await runner.create(
            Capability.SITE_CRAWL, {"url": "https://example.org/"},
            self.provider.name, self.account, "saved-upstream-reference", self.owner,
        )


@pytest.fixture
def state(tmp_path):
    provider = FakeProvider()
    db = Database(tmp_path / "jobs.sqlite3")
    db.initialize({provider.name: provider})
    with db.session() as session:
        account = session.scalar(select(Account.id).where(Account.provider == provider.name))
    owner, _ = create_client_token(db, "owner")
    other, _ = create_client_token(db, "other")
    fixture = State(db, provider, account, owner, other, {provider.name: provider})
    yield fixture
    fixture.db.close()


async def eventually(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def test_completed_result_is_finalized_and_persisted(state):
    async def scenario():
        finalized = []

        async def finalize(cap, result):
            finalized.append((cap, result))
            return {
                "status": "complete", "coverage": {"ok": [state.provider.name], "failed": [], "skipped": []},
                "request_id": "r_fixture", "items": [hit.model_dump(mode="json") for hit in result.hits],
            }

        state.provider.updates.extend([
            JobUpdate("running", poll_after_s=0.01),
            JobUpdate("completed", Result(hits=[Hit(provider=state.provider.name, url="https://example.org/")])),
        ])
        runner = state.runner(finalize)
        await runner.start()
        await runner.start()
        try:
            initial = await state.create(runner)
            assert initial.status == "running"
            output = await runner.get(initial.job_id, state.owner, wait_s=0.5)
            assert output.status == "completed"
            assert output.result["items"][0]["url"] == "https://example.org/"
            assert finalized[0][0] == Capability.SITE_CRAWL
            assert isinstance(finalized[0][1], Result)
            with state.db.session() as session:
                row = session.get(Job, initial.job_id)
                assert row.args == {"url": "https://example.org/"}
                assert row.provider == state.provider.name
                assert row.account_id == state.account
                assert row.upstream_job_ref == "saved-upstream-reference"
                assert row.result == output.result
            assert len(state.provider.polls) == 2
            assert state.provider.starts == 0
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_actual_restart_resumes_saved_reference_without_launching(state):
    async def scenario():
        runner = state.runner()
        state.provider.gates[1] = asyncio.Event()
        await runner.start()
        job = await state.create(runner)
        await eventually(lambda: len(state.provider.polls) == 1)
        await runner.stop()
        assert state.provider.cancelled_polls == 1
        assert state.provider.cancellations == []
        with state.db.session() as session:
            assert session.get(Job, job.job_id).status == "running"
        path = state.db.path
        state.db.close()
        state.db = Database(path)
        state.db.initialize({state.provider.name: state.provider})
        resumed_provider = FakeProvider()
        resumed_provider.updates.append(JobUpdate("completed", Result(urls=["https://example.org/resumed"])))
        state.registry = {resumed_provider.name: resumed_provider}
        restarted = state.runner()
        await restarted.start()
        try:
            output = await restarted.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "completed"
            assert output.result["urls"] == ["https://example.org/resumed"]
            assert resumed_provider.polls == [("saved-upstream-reference", state.account)]
            assert resumed_provider.starts == state.provider.starts == 0
        finally:
            await restarted.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [
    ProviderError(ErrorKind.TRANSIENT, "retry this saved job", retry_after=0.01),
    ProviderError(ErrorKind.RATE_LIMITED, "poll quota", retry_after=0.01),
    ProviderError(ErrorKind.EXHAUSTED, "poll quota exhausted", retry_after=0.01),
    httpx.ReadTimeout("url may contain credentials"),
    TimeoutError("temporary timeout"),
])
def test_transient_poll_errors_retry_same_provider_and_account(state, failure):
    async def scenario():
        unused = FakeProvider()
        state.registry["fallback"] = unused
        state.provider.updates.extend([failure, JobUpdate("completed", Result(urls=["https://example.org/done"]))])
        state.provider.gates[2] = asyncio.Event()
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            await eventually(lambda: len(state.provider.polls) == 2)
            pending = await runner.get(job.job_id, state.owner)
            assert pending.status == "running"
            assert pending.last_error
            assert "url may contain credentials" not in pending.last_error
            state.provider.gates[2].set()
            completed = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert completed.status == "completed"
            assert completed.last_error is None
            assert state.provider.polls == [("saved-upstream-reference", state.account)] * 2
            assert unused.polls == []
            assert unused.starts == state.provider.starts == 0
        finally:
            await runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [ErrorKind.AUTH, ErrorKind.PLAN, ErrorKind.BAD_REQUEST, ErrorKind.TARGET])
def test_permanent_poll_error_is_terminal(state, kind):
    async def scenario():
        state.provider.updates.append(ProviderError(kind, "permanent failure"))
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "failed"
            assert output.last_error == f"{kind.value}: permanent failure"
            assert output.result is None
            assert len(state.provider.polls) == 1
        finally:
            await runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_upstream_terminal_status_is_saved(state, status):
    async def scenario():
        state.provider.updates.append(JobUpdate(status, error="upstream terminal state"))
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == status
            assert output.last_error == "upstream terminal state"
            with state.db.session() as session:
                row = session.get(Job, job.job_id)
                assert row.status == status
                assert row.cancelled_upstream is (True if status == "cancelled" else None)
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_ownership_and_revocation_are_rechecked_after_wait_without_cancelling(state):
    async def scenario():
        state.provider.gates[1] = asyncio.Event()
        state.provider.updates.append(JobUpdate("completed", Result(urls=["https://example.org/private"])))
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            with pytest.raises(PermissionError):
                await runner.get(job.job_id, state.other)
            with pytest.raises(PermissionError):
                await runner.get(job.job_id, None)
            with pytest.raises(KeyError):
                await runner.get("missing", state.owner)
            waiter = asyncio.create_task(runner.get(job.job_id, state.owner, wait_s=0.5))
            await eventually(lambda: len(state.provider.polls) == 1)
            with state.db.session() as session:
                session.get(ClientToken, state.owner).revoked = True
            with pytest.raises(PermissionError):
                await runner.get(job.job_id, state.owner)
            state.provider.gates[1].set()
            with pytest.raises(PermissionError):
                await waiter
            inspected = await runner.get(job.job_id, None, admin=True)
            assert inspected.status == "completed"
            assert inspected.result["urls"] == ["https://example.org/private"]
            assert state.provider.cancellations == []
        finally:
            await runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("response,expected", [
    (True, True), (False, False), (None, None),
    (ProviderError(ErrorKind.TRANSIENT, "cancel unavailable"), None),
])
def test_admin_cancel_reports_upstream_state_and_retries_only_unknown(state, response, expected):
    async def scenario():
        state.provider.cancel_response = response
        state.provider.gates[1] = asyncio.Event()
        # Even an adapter that swallows task cancellation cannot overwrite local cancellation.
        state.provider.complete_when_cancelled = True
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            await eventually(lambda: len(state.provider.polls) == 1)
            cancelled = await runner.cancel(job.job_id)
            assert cancelled["status"] == "cancelled"
            assert cancelled["cancelled_upstream"] is expected
            assert state.provider.cancellations == ["saved-upstream-reference"]
            assert state.provider.cancelled_polls == 1
            assert (await runner.get(job.job_id, state.owner)).status == "cancelled"
            assert await runner.cancel(job.job_id) == cancelled
            assert state.provider.cancellations == ["saved-upstream-reference"] * (2 if expected is None else 1)
            with state.db.session() as session:
                row = session.get(Job, job.job_id)
                assert row.result is None
                assert row.cancelled_upstream is expected
            with pytest.raises(KeyError):
                await runner.cancel("missing")
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_unsupported_upstream_cancel_is_false(state):
    async def scenario():
        state.registry[state.provider.name] = Provider()
        runner = state.runner()
        job = await state.create(runner)
        result = await runner.cancel(job.job_id)
        assert result["cancelled_upstream"] is False
        assert result["status"] == "cancelled"
        assert state.provider.cancellations == []

    asyncio.run(scenario())


def test_missing_upstream_provider_cancel_is_unknown(state):
    async def scenario():
        runner = state.runner()
        job = await state.create(runner)
        state.registry.clear()
        result = await runner.cancel(job.job_id)
        assert result["cancelled_upstream"] is None
        assert result["status"] == "cancelled"

    asyncio.run(scenario())


def test_wait_is_bounded_and_token_revocation_is_checked_after_timeout(state):
    async def scenario():
        runner = state.runner()
        job = await state.create(runner)
        before = time.monotonic()
        pending = await runner.get(job.job_id, state.owner, wait_s=0.03)
        assert pending.status == "running"
        assert 0.02 <= time.monotonic() - before < 0.5
        waiter = asyncio.create_task(runner.get(job.job_id, state.owner, wait_s=0.03))
        await asyncio.sleep(0.01)
        with state.db.session() as session:
            session.get(ClientToken, state.owner).revoked = True
        with pytest.raises(PermissionError):
            await waiter
        for invalid in [float("inf"), float("nan"), -1]:
            with pytest.raises(ValueError):
                await runner.get(job.job_id, state.owner, wait_s=invalid)
        await runner.stop()

    asyncio.run(scenario())


def test_poll_timeout_retries_saved_reference(state):
    async def scenario():
        state.provider.gates[1] = asyncio.Event()
        state.provider.updates.append(JobUpdate("completed", Result(urls=["https://example.org/retried"])))
        runner = state.runner()
        runner.poll_timeout_s = 0.02
        await runner.start()
        try:
            job = await state.create(runner)
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "completed"
            assert state.provider.cancelled_polls == 1
            assert state.provider.polls == [("saved-upstream-reference", state.account)] * 2
        finally:
            await runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("update", [
    JobUpdate("unknown"), JobUpdate("completed"), "malformed", JobUpdate({}),
    JobUpdate("failed", error={"malformed": "error"}),
])
def test_malformed_upstream_completion_is_terminal(state, update):
    async def scenario():
        state.provider.updates.append(update)
        runner = state.runner()
        await runner.start()
        try:
            job = await state.create(runner)
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "failed"
            assert output.last_error
            assert len(state.provider.polls) == 1
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_finalizer_failure_is_terminal(state):
    async def scenario():
        async def broken(cap, result):
            raise RuntimeError("secret value should not be reported")

        state.provider.updates.append(JobUpdate("completed", Result()))
        runner = state.runner(broken)
        await runner.start()
        try:
            job = await state.create(runner)
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "failed"
            assert output.last_error == "Job result finalization failed (RuntimeError)"
            assert "secret" not in output.last_error
        finally:
            await runner.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_terminal_rows_are_not_resumed_or_cancelled_again(state, terminal):
    async def scenario():
        runner = state.runner()
        job = await state.create(runner)
        with state.db.session() as session:
            row = session.get(Job, job.job_id)
            row.status = terminal
            row.result = {"urls": []} if terminal == "completed" else None
            row.cancelled_upstream = True if terminal == "cancelled" else None
        await runner.start()
        try:
            output = await runner.get(job.job_id, state.owner)
            assert output.status == terminal
            response = await runner.cancel(job.job_id)
            assert response["status"] == terminal
            assert state.provider.polls == state.provider.cancellations == []
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_ordinary_work_is_not_convertible_to_a_job(state):
    async def scenario():
        with pytest.raises(ValueError, match="inherently async"):
            await state.runner().create(
                Capability.WEB_SEARCH, {}, state.provider.name, state.account, "ref", state.owner,
            )

    asyncio.run(scenario())


def test_recovery_without_upstream_reference_requires_reconciliation(state):
    async def scenario():
        runner = state.runner()
        job = await state.create(runner)
        with state.db.session() as session:
            session.get(Job, job.job_id).upstream_job_ref = ""
        await runner.start()
        try:
            output = await runner.get(job.job_id, state.owner, wait_s=0.5)
            assert output.status == "failed"
            assert "manual reconciliation" in output.last_error
            assert state.provider.starts == 0
            assert state.provider.polls == []
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_cancel_survives_admin_disconnect_and_coalesces_concurrent_attempts(state):
    async def scenario():
        runner = state.runner()
        state.provider.cancel_gate = asyncio.Event()
        job = await state.create(runner)
        first = asyncio.create_task(runner.cancel(job.job_id))
        second = asyncio.create_task(runner.cancel(job.job_id))
        try:
            await eventually(lambda: len(state.provider.cancellations) == 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with state.db.session() as session:
                row = session.get(Job, job.job_id)
                assert row.status == "cancelled"
                assert row.cancelled_upstream is None
            assert not second.done()
            state.provider.cancel_gate.set()
            result = await second
            assert result["cancelled_upstream"] is True
            assert await runner.cancel(job.job_id) == result
            assert state.provider.cancellations == ["saved-upstream-reference"]
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_unknown_cancellation_can_be_retried_after_runner_restart(state):
    async def scenario():
        runner = state.runner()
        job = await state.create(runner)
        state.provider.cancel_response = ProviderError(ErrorKind.TRANSIENT, "temporarily unavailable")
        assert (await runner.cancel(job.job_id))["cancelled_upstream"] is None
        await runner.stop()
        restarted = state.runner()
        state.provider.cancel_response = True
        await restarted.start()
        try:
            retried = await restarted.cancel(job.job_id)
            assert retried["status"] == "cancelled"
            assert retried["cancelled_upstream"] is True
            assert state.provider.cancellations == ["saved-upstream-reference"] * 2
            assert state.provider.polls == []
        finally:
            await restarted.stop()

    asyncio.run(scenario())
