"""Persist and poll upstream jobs in one process without launching work again.

Routing owns upstream start and fallback. This module accepts a recovered reference,
or records an unknown start for explicit reconciliation without polling or restarting it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from research_engine.providers.base import (
    ASYNC_CAPABILITIES,
    CallContext,
    Capability,
    ErrorKind,
    JobUpdate,
    Provider,
    ProviderError,
)
from research_engine.server.schemas import JobOutput, Result
from research_engine.storage.db import Account, ClientToken, Database, Job, utcnow

logger = logging.getLogger(__name__)

ContextFactory = Callable[[str, int, float], Awaitable[CallContext]]
ResultFinalizer = Callable[[Capability, Result], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class _JobState:
    """Copy scalar fields before closing a worker-thread SQLAlchemy session."""

    id: str
    capability: str
    provider: str
    account_id: int
    upstream_job_ref: str
    client_token_id: int | None
    status: str
    updated_at: Any
    poll_after_s: float
    result: dict[str, Any] | None
    last_error: str | None
    cancelled_upstream: bool | None

    @classmethod
    def from_row(cls, row: Job) -> _JobState:
        return cls(
            row.id, row.capability, row.provider, row.account_id, row.upstream_job_ref,
            row.client_token_id, row.status, row.updated_at, row.poll_after_s,
            deepcopy(row.result), row.last_error, row.cancelled_upstream,
        )


def _interval(value: Any, default: float = 15) -> float:
    """Keep malformed upstream retry hints from spinning or wedging a task."""
    try:
        interval = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(interval) or interval < 0:
        return default
    return max(0.01, min(interval, 86400))


# Provider messages, including errors returned by poll/cancel, are untrusted. Only
# locally authored text is stored; never interpolate exception messages, URLs or
# account credentials. Normalize old persisted errors on read as well.
_SAFE_ERRORS = frozenset({
    "Upstream start outcome is unknown; manual reconciliation required",
    "Missing upstream job reference; manual reconciliation required",
    "Persisted job provider is unavailable",
    "Invalid upstream job update",
    "Invalid upstream job error",
    "Invalid upstream job status",
    "Upstream job failed",
    "Upstream job reported an error",
    "Provider unavailable for upstream cancellation",
    "Invalid upstream cancellation response",
})
_ERROR_KINDS = frozenset(kind.value for kind in ErrorKind)
_LOCAL_ERROR = re.compile(
    r"^(?:Upstream polling failed|Upstream polling temporarily unavailable|"
    r"Upstream cancellation failed|Job result finalization failed) \([A-Za-z_][A-Za-z_0-9]*\)$"
)


def _public_error(error: str | None) -> str | None:
    if error is None:
        return None
    if error in _SAFE_ERRORS or _LOCAL_ERROR.fullmatch(error):
        return error
    kind, separator, detail = error.partition(": ")
    if separator and kind in _ERROR_KINDS:
        if detail in {"Upstream polling failed", "Upstream cancellation failed"}:
            return error
        return f"{kind}: Upstream job error"
    return "Upstream job error"


class JobRunner:
    """One process owns these polling tasks; no leases or worker queue are needed.

    Stopping cancels local coroutines and leaves running rows resumable. It never
    invokes upstream cancellation. Only the admin ``cancel`` method does that.
    """

    poll_timeout_s = 30.0
    max_wait_s = 60.0

    def __init__(
        self,
        db: Database,
        registry: dict[str, Provider],
        context_factory: ContextFactory,
        finalize_result: ResultFinalizer | None = None,
    ):
        self.db = db
        self.registry = registry
        self.context_factory = context_factory
        self.finalize_result = finalize_result
        self._running = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        if self._running:
            return
        job_ids = await asyncio.to_thread(self._running_job_ids)
        self._running = True
        for job_id in job_ids:
            self._spawn(job_id)

    async def stop(self) -> None:
        self._running = False
        tasks = [*self._tasks.values(), *self._cancellations.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancellations.clear()
        for event in self._events.values():
            event.set()
        self._events.clear()

    async def create(
        self,
        cap: Capability,
        args: dict[str, Any],
        provider: str,
        account_id: int,
        upstream_ref: str,
        client_token_id: int | None,
    ) -> JobOutput:
        """Persist a recovered upstream reference before scheduling the first poll."""
        adapter = self._validate_start(cap, provider)
        if not isinstance(upstream_ref, str) or not upstream_ref.strip():
            raise ValueError("An upstream job reference is required")
        state = await asyncio.to_thread(
            self._insert_job, Capability(cap), args, provider, account_id,
            upstream_ref, client_token_id, "running", _interval(adapter.poll_interval_s),
        )
        if self._running:
            self._spawn(state.id)
        return self._output(state)

    async def record_unknown_start(
        self,
        cap: Capability,
        args: dict[str, Any],
        provider: str,
        account_id: int,
        client_token_id: int | None,
    ) -> JobOutput:
        """Record an ambiguous upstream submission; never retry it automatically.

        The caller must invoke this when start may have succeeded but yielded no ref.
        The empty reference is internal only: the row remains failed until an
        operator explicitly attaches a recovered reference. It is never polled
        or cancelled while its upstream state is unknown.
        """
        self._validate_start(cap, provider)
        state = await asyncio.to_thread(
            self._insert_job, Capability(cap), args, provider, account_id,
            "", client_token_id, "failed", 15.0,
        )
        return self._output(state)

    async def reconcile_unknown_start(self, job_id: str, upstream_ref: str) -> JobOutput:
        """Explicitly attach an independently recovered reference; do not start work."""
        if not isinstance(upstream_ref, str) or not upstream_ref.strip():
            raise ValueError("A recovered upstream job reference is required")
        state = await asyncio.to_thread(self._reconcile_unknown_start, job_id, upstream_ref)
        self._notify(job_id)
        if self._running and state.status == "running":
            self._spawn(job_id)
        return self._output(state)

    def _validate_start(self, cap: Capability, provider: str) -> Provider:
        if Capability(cap) not in ASYNC_CAPABILITIES:
            raise ValueError("Jobs are only supported for inherently async capabilities")
        adapter = self.registry.get(provider)
        if adapter is None:
            raise ValueError("Unknown job provider")
        return adapter

    def _insert_job(
        self, cap: Capability, args: dict[str, Any], provider: str, account_id: int,
        upstream_ref: str, client_token_id: int | None, status: str, poll_after_s: float,
    ) -> _JobState:
        with self.db.session() as session:
            account = session.get(Account, account_id)
            if account is None or account.provider != provider:
                raise ValueError("The job account must belong to its provider")
            job = Job(
                capability=cap.value,
                args=deepcopy(args),
                provider=provider,
                account_id=account_id,
                upstream_job_ref=upstream_ref,
                client_token_id=client_token_id,
                status=status,
                poll_after_s=poll_after_s,
                last_error=("Upstream start outcome is unknown; manual reconciliation required"
                            if status == "failed" and not upstream_ref else None),
            )
            session.add(job)
            session.flush()
            return _JobState.from_row(job)

    def _reconcile_unknown_start(self, job_id: str, upstream_ref: str) -> _JobState:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if (job.status != "failed" or job.upstream_job_ref
                    or job.last_error != "Upstream start outcome is unknown; manual reconciliation required"):
                raise ValueError("Only an unknown start can be reconciled")
            job.upstream_job_ref = upstream_ref
            job.status = "running"
            job.last_error = None
            job.updated_at = utcnow() - timedelta(seconds=_interval(job.poll_after_s))
            session.flush()
            return _JobState.from_row(job)

    async def get(
        self,
        job_id: str,
        client_token_id: int | None,
        wait_s: float = 0,
        admin: bool = False,
    ) -> JobOutput:
        if not math.isfinite(wait_s) or wait_s < 0:
            raise ValueError("wait_s must be a finite nonnegative number")
        deadline = time.monotonic() + min(wait_s, self.max_wait_s)
        while True:
            state = await asyncio.to_thread(self._load_authorized, job_id, client_token_id, admin)
            remaining = deadline - time.monotonic()
            if state.status != "running" or remaining <= 0:
                return self._output(state)
            event = self._events.setdefault(job_id, asyncio.Event())
            # A state change during the DB read / event registration must not
            # leave a long-poll waiting indefinitely for an already-finished job.
            state = await asyncio.to_thread(self._load_authorized, job_id, client_token_id, admin)
            if state.status != "running":
                return self._output(state)
            try:
                await asyncio.wait_for(event.wait(), max(0, deadline - time.monotonic()))
            except TimeoutError:
                state = await asyncio.to_thread(self._load_authorized, job_id, client_token_id, admin)
                return self._output(state)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Admin-only caller; coalesce concurrent calls and preserve unknown outcomes."""
        operation = self._cancellations.get(job_id)
        if operation is None:
            operation = asyncio.create_task(self._cancel_upstream(job_id), name=f"research-job-cancel:{job_id}")
            self._cancellations[job_id] = operation
            operation.add_done_callback(lambda finished: self._cancel_finished(job_id, finished))
        return await asyncio.shield(operation)

    def _prepare_cancel(self, job_id: str) -> tuple[_JobState, bool]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if (job.status == "failed" and not job.upstream_job_ref
                    and job.last_error == "Upstream start outcome is unknown; manual reconciliation required"):
                return _JobState.from_row(job), False
            if job.status != "running" and (job.status != "cancelled" or job.cancelled_upstream is not None):
                return _JobState.from_row(job), False
            if job.status == "running":
                job.status = "cancelled"
                job.updated_at = utcnow()
                job.cancelled_upstream = None
                job.last_error = None
            session.flush()
            return _JobState.from_row(job), True

    async def _cancel_upstream(self, job_id: str) -> dict[str, Any]:
        state, should_cancel = await asyncio.to_thread(self._prepare_cancel, job_id)
        if not should_cancel:
            return self._cancel_output(state)
        self._notify(job_id)
        polling = self._tasks.get(job_id)
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)

        stopped: bool | None = False
        error: str | None = None
        adapter = self.registry.get(state.provider)
        cancel_method = getattr(adapter, "cancel", None)
        if adapter is None:
            stopped, error = None, "Provider unavailable for upstream cancellation"
        elif callable(cancel_method) and getattr(cancel_method, "__func__", None) is not Provider.cancel:
            try:
                async with asyncio.timeout(self.poll_timeout_s):
                    ctx = await self.context_factory(state.provider, state.account_id, self.poll_timeout_s)
                    response = await cancel_method(ctx, state.upstream_job_ref)
                if response is True or response is False:
                    stopped = response
                else:
                    stopped, error = None, "Invalid upstream cancellation response"
            except Exception as exc:
                stopped, error = None, self._error_text(exc, "Upstream cancellation failed")
        finished = await asyncio.to_thread(self._finish_cancel, job_id, stopped, error)
        return self._cancel_output(finished)

    def _finish_cancel(self, job_id: str, stopped: bool | None, error: str | None) -> _JobState:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status == "cancelled":
                job.cancelled_upstream = stopped
                job.last_error = error
                job.updated_at = utcnow()
            session.flush()
            return _JobState.from_row(job)

    def _cancel_finished(self, job_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._cancellations.get(job_id) is task:
            self._cancellations.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Job cancellation could not update storage: %s", job_id)

    def _spawn(self, job_id: str) -> None:
        if job_id in self._tasks:
            return
        task = asyncio.create_task(self._poll_job(job_id), name=f"research-job:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda finished: self._finished(job_id, finished))

    def _finished(self, job_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Job polling task stopped unexpectedly: %s", job_id)

    async def _poll_job(self, job_id: str) -> None:
        while self._running:
            try:
                job = await asyncio.to_thread(self._load, job_id)
                if job is None or job.status != "running":
                    return
                due = job.updated_at + timedelta(seconds=_interval(job.poll_after_s))
                delay = max(0.0, (due - utcnow()).total_seconds())
                if delay:
                    await asyncio.sleep(delay)
                job = await asyncio.to_thread(self._load, job_id)
                if job is None or job.status != "running":
                    return
                await self._poll_once(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Temporary DB failure must not strand a running row. Never log
                # exception values: transport URLs and SQLite parameters may leak.
                logger.error("Job polling could not update storage: %s", job_id)
                await asyncio.sleep(1)

    async def _poll_once(self, job: _JobState) -> None:
        if not isinstance(job.upstream_job_ref, str) or not job.upstream_job_ref.strip():
            await self._persist(job.id, "failed", error="Missing upstream job reference; manual reconciliation required")
            return
        adapter = self.registry.get(job.provider)
        if adapter is None:
            await self._persist(job.id, "failed", error="Persisted job provider is unavailable")
            return
        try:
            async with asyncio.timeout(self.poll_timeout_s):
                ctx = await self.context_factory(job.provider, job.account_id, self.poll_timeout_s)
                update = await adapter.poll(ctx, job.upstream_job_ref)
        except ProviderError as exc:
            retryable = exc.kind in {ErrorKind.TRANSIENT, ErrorKind.RATE_LIMITED, ErrorKind.EXHAUSTED}
            delay = exc.retry_after
            if delay is None and exc.reset_at is not None:
                reset_at = exc.reset_at
                if reset_at.tzinfo is None:
                    reset_at = reset_at.replace(tzinfo=timezone.utc)
                delay = (reset_at - utcnow()).total_seconds()
            await self._persist(
                job.id,
                "running" if retryable else "failed",
                error=self._error_text(exc, "Upstream polling failed"),
                poll_after_s=_interval(delay, _interval(adapter.poll_interval_s)),
            )
            return
        except (httpx.TransportError, TimeoutError, ConnectionError, OSError) as exc:
            await self._persist(
                job.id,
                "running",
                error=self._error_text(exc, "Upstream polling temporarily unavailable"),
                poll_after_s=_interval(adapter.poll_interval_s),
            )
            return
        except Exception as exc:
            await self._persist(job.id, "failed", error=self._error_text(exc, "Upstream polling failed"))
            return

        if not isinstance(update, JobUpdate) or not isinstance(update.status, str):
            await self._persist(job.id, "failed", error="Invalid upstream job update")
            return
        if update.error is not None and not isinstance(update.error, str):
            await self._persist(job.id, "failed", error="Invalid upstream job error")
            return
        status = {
            "pending": "running", "queued": "running", "processing": "running",
            "complete": "completed", "done": "completed", "succeeded": "completed",
            "error": "failed", "canceled": "cancelled",
        }.get(update.status, update.status)
        if status not in {"running", "completed", "failed", "cancelled"}:
            await self._persist(job.id, "failed", error="Invalid upstream job status")
            return
        result = None
        if status == "completed":
            try:
                if update.result is None:
                    raise ValueError("No result in completed upstream job")
                if self.finalize_result is not None:
                    normalized = Result.model_validate(update.result)
                    async with asyncio.timeout(self.poll_timeout_s):
                        result = await self.finalize_result(Capability(job.capability), normalized)
                elif isinstance(update.result, Result):
                    result = update.result.model_dump(mode="json")
                else:
                    result = deepcopy(update.result)
                if not isinstance(result, dict):
                    raise ValueError("The final job result must be an object")
                json.dumps(result, allow_nan=False)
            except Exception as exc:
                await self._persist(job.id, "failed", error=self._error_text(exc, "Job result finalization failed"))
                return
        error = None if status == "completed" else ("Upstream job failed" if status == "failed" else None)
        if update.error and status == "running":
            error = "Upstream job reported an error"
        await self._persist(
            job.id, status, result=result, error=error,
            poll_after_s=_interval(update.poll_after_s, _interval(adapter.poll_interval_s)),
        )

    async def _persist(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        poll_after_s: float | None = None,
    ) -> None:
        changed = await asyncio.to_thread(
            self._write_update, job_id, status, result, error, poll_after_s,
        )
        if changed:
            self._notify(job_id)

    def _write_update(
        self, job_id: str, status: str, result: dict[str, Any] | None,
        error: str | None, poll_after_s: float | None,
    ) -> bool:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "running":
                return False
            # An admin cancellation wins even if a provider ignores task cancellation.
            job.status = status
            job.result = result
            job.last_error = error
            job.updated_at = utcnow()
            if poll_after_s is not None:
                job.poll_after_s = poll_after_s
            if status == "cancelled":
                job.cancelled_upstream = True
            return True

    def _running_job_ids(self) -> list[str]:
        with self.db.session() as session:
            return list(session.scalars(select(Job.id).where(Job.status == "running")))

    def _load(self, job_id: str) -> _JobState | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            return _JobState.from_row(job) if job is not None else None

    def _load_authorized(self, job_id: str, client_token_id: int | None, admin: bool) -> _JobState:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if not admin:
                if client_token_id is None or job.client_token_id != client_token_id:
                    raise PermissionError("This job belongs to another client token")
                token = session.get(ClientToken, client_token_id)
                if token is None or token.revoked:
                    raise PermissionError("The client token has been revoked")
            return _JobState.from_row(job)

    def _notify(self, job_id: str) -> None:
        event = self._events.pop(job_id, None)
        if event is not None:
            event.set()

    @staticmethod
    def _output(job: _JobState) -> JobOutput:
        return JobOutput(
            job_id=job.id,
            status=job.status,
            poll_after_s=job.poll_after_s,
            result=deepcopy(job.result),
            last_error=_public_error(job.last_error),
        )

    @staticmethod
    def _cancel_output(job: _JobState) -> dict[str, Any]:
        return {
            "job_id": job.id,
            "status": job.status,
            "cancelled_upstream": job.cancelled_upstream,
            "last_error": _public_error(job.last_error),
        }

    @staticmethod
    def _error_text(exc: Exception, prefix: str) -> str:
        if isinstance(exc, ProviderError):
            return f"{exc.kind.value}: {prefix}"
        return f"{prefix} ({type(exc).__name__})"
