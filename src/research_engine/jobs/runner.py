"""Persist and poll upstream jobs in one process without launching work again.

Routing owns upstream start and fallback. This module only accepts a reference
that has already been obtained and resumes that same provider/account on restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
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


def _interval(value: Any, default: float = 15) -> float:
    """Keep malformed upstream retry hints from spinning or wedging a task."""
    try:
        interval = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(interval) or interval < 0:
        return default
    return max(0.01, min(interval, 86400))


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
        with self.db.session() as session:
            job_ids = list(session.scalars(select(Job.id).where(Job.status == "running")))
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
        capability = Capability(cap)
        if capability not in ASYNC_CAPABILITIES:
            raise ValueError("Jobs are only supported for inherently async capabilities")
        if not isinstance(upstream_ref, str) or not upstream_ref.strip():
            raise ValueError("An upstream job reference is required")
        adapter = self.registry.get(provider)
        if adapter is None:
            raise ValueError("Unknown job provider")
        with self.db.session() as session:
            account = session.get(Account, account_id)
            if account is None or account.provider != provider:
                raise ValueError("The job account must belong to its provider")
            job = Job(
                capability=capability.value,
                args=deepcopy(args),
                provider=provider,
                account_id=account_id,
                upstream_job_ref=upstream_ref,
                client_token_id=client_token_id,
                status="running",
                poll_after_s=_interval(adapter.poll_interval_s),
            )
            session.add(job)
            session.flush()
            output = self._output(job)
        # The transaction commits before any poll is scheduled or response sent.
        if self._running:
            self._spawn(job.id)
        return output

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
            job = self._load(job_id)
            if job is None:
                raise KeyError(job_id)
            self._authorize(job, client_token_id, admin)
            remaining = deadline - time.monotonic()
            if job.status != "running" or remaining <= 0:
                return self._output(job)
            event = self._events.setdefault(job_id, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                # Recheck both ownership and revocation after every await.
                job = self._load(job_id)
                if job is None:
                    raise KeyError(job_id) from None
                self._authorize(job, client_token_id, admin)
                return self._output(job)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Admin-only caller: stop local work first, then try upstream cancellation.

        cancelled_upstream is True when acknowledged, False when unsupported or
        rejected, and None when an error leaves the upstream state unknown.
        Concurrent attempts share one operation. A disconnected admin request
        does not abort it, and unknown outcomes can be retried after restart.
        """
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "running" and (job.status != "cancelled" or job.cancelled_upstream is not None):
                return self._cancel_output(job)
            if job.status == "running":
                job.status = "cancelled"
                job.updated_at = utcnow()
                job.cancelled_upstream = None
                job.last_error = None
        self._notify(job_id)
        operation = self._cancellations.get(job_id)
        if operation is None:
            operation = asyncio.create_task(self._cancel_upstream(job), name=f"research-job-cancel:{job_id}")
            self._cancellations[job_id] = operation
            operation.add_done_callback(lambda finished: self._cancel_finished(job_id, finished))
        return await asyncio.shield(operation)

    async def _cancel_upstream(self, job: Job) -> dict[str, Any]:
        polling = self._tasks.get(job.id)
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)

        stopped: bool | None = False
        error: str | None = None
        adapter = self.registry.get(job.provider)
        cancel_method = getattr(adapter, "cancel", None)
        if adapter is None:
            stopped, error = None, "Provider unavailable for upstream cancellation"
        elif callable(cancel_method) and getattr(cancel_method, "__func__", None) is not Provider.cancel:
            try:
                async with asyncio.timeout(self.poll_timeout_s):
                    ctx = await self.context_factory(job.provider, job.account_id, self.poll_timeout_s)
                    response = await cancel_method(ctx, job.upstream_job_ref)
                if response is True or response is False:
                    stopped = response
                else:
                    stopped, error = None, "Invalid upstream cancellation response"
            except Exception as exc:
                stopped, error = None, self._error_text(exc, "Upstream cancellation failed")
        with self.db.session() as session:
            persisted = session.get(Job, job.id)
            if persisted is None:
                raise KeyError(job.id)
            persisted.cancelled_upstream = stopped
            persisted.last_error = error
            persisted.updated_at = utcnow()
            return self._cancel_output(persisted)

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
                job = self._load(job_id)
                if job is None or job.status != "running":
                    return
                due = job.updated_at + timedelta(seconds=_interval(job.poll_after_s))
                delay = max(0.0, (due - utcnow()).total_seconds())
                if delay:
                    await asyncio.sleep(delay)
                job = self._load(job_id)
                if job is None or job.status != "running":
                    return
                await self._poll_once(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A temporary database failure must not strand a running row.
                # Do not log exception values: transport URLs may contain keys.
                logger.error("Job polling could not update storage: %s", job_id)
                await asyncio.sleep(1)

    async def _poll_once(self, job: Job) -> None:
        if not isinstance(job.upstream_job_ref, str) or not job.upstream_job_ref.strip():
            self._persist(job.id, "failed", error="Missing upstream job reference; manual reconciliation required")
            return
        adapter = self.registry.get(job.provider)
        if adapter is None:
            self._persist(job.id, "failed", error="Persisted job provider is unavailable")
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
            self._persist(
                job.id,
                "running" if retryable else "failed",
                error=self._error_text(exc, "Upstream polling failed"),
                poll_after_s=_interval(delay, _interval(adapter.poll_interval_s)),
            )
            return
        except (httpx.TransportError, TimeoutError, ConnectionError, OSError) as exc:
            self._persist(
                job.id,
                "running",
                error=self._error_text(exc, "Upstream polling temporarily unavailable"),
                poll_after_s=_interval(adapter.poll_interval_s),
            )
            return
        except Exception as exc:
            self._persist(job.id, "failed", error=self._error_text(exc, "Upstream polling failed"))
            return

        if not isinstance(update, JobUpdate) or not isinstance(update.status, str):
            self._persist(job.id, "failed", error="Invalid upstream job update")
            return
        if update.error is not None and not isinstance(update.error, str):
            self._persist(job.id, "failed", error="Invalid upstream job error")
            return
        status = {
            "pending": "running", "queued": "running", "processing": "running",
            "complete": "completed", "done": "completed", "succeeded": "completed",
            "error": "failed", "canceled": "cancelled",
        }.get(update.status, update.status)
        if status not in {"running", "completed", "failed", "cancelled"}:
            self._persist(job.id, "failed", error="Invalid upstream job status")
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
                self._persist(job.id, "failed", error=self._error_text(exc, "Job result finalization failed"))
                return
        error = update.error
        if status == "completed":
            error = None
        elif status == "failed" and not error:
            error = "Upstream job failed"
        self._persist(
            job.id,
            status,
            result=result,
            error=error,
            poll_after_s=_interval(update.poll_after_s, _interval(adapter.poll_interval_s)),
        )

    def _persist(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        poll_after_s: float | None = None,
    ) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "running":
                return
            # An admin cancellation wins even if a provider ignores task cancellation.
            job.status = status
            job.result = result
            job.last_error = error
            job.updated_at = utcnow()
            if poll_after_s is not None:
                job.poll_after_s = poll_after_s
            if status == "cancelled":
                job.cancelled_upstream = True
        self._notify(job_id)

    def _load(self, job_id: str) -> Job | None:
        with self.db.session() as session:
            return session.get(Job, job_id)

    def _authorize(self, job: Job, client_token_id: int | None, admin: bool) -> None:
        if admin:
            return
        if client_token_id is None or job.client_token_id != client_token_id:
            raise PermissionError("This job belongs to another client token")
        with self.db.session() as session:
            token = session.get(ClientToken, client_token_id)
            if token is None or token.revoked:
                raise PermissionError("The client token has been revoked")

    def _notify(self, job_id: str) -> None:
        event = self._events.pop(job_id, None)
        if event is not None:
            event.set()

    @staticmethod
    def _output(job: Job) -> JobOutput:
        return JobOutput(
            job_id=job.id,
            status=job.status,
            poll_after_s=job.poll_after_s,
            result=deepcopy(job.result),
            last_error=job.last_error,
        )

    @staticmethod
    def _cancel_output(job: Job) -> dict[str, Any]:
        return {
            "job_id": job.id,
            "status": job.status,
            "cancelled_upstream": job.cancelled_upstream,
            "last_error": job.last_error,
        }

    @staticmethod
    def _error_text(exc: Exception, prefix: str) -> str:
        if isinstance(exc, ProviderError):
            return f"{exc.kind.value}: {str(exc)[:1000]}"
        return f"{prefix} ({type(exc).__name__})"
