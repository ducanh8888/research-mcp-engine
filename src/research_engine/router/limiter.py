"""Small, request-bound rate and concurrency limiter for direct providers.

One process shares limiter state for every outgoing HTTP/MCP operation of an
account or its quota group. The caller must wrap each subrequest, Test, poll,
or cancel; a lock around a top-level adapter call is not equivalent.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Bucket:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    concurrency: asyncio.Semaphore | None = None
    concurrency_limit: int | None = None
    next_at: float = 0
    rate_limit_rps: float | None = None


class RequestLimiter:
    """Single-event-loop in-memory quota-group limiter.

    ``rate_limit_rps`` uses fixed spacing of starts rather than bursts; no
    upstream request is sent before ``admit`` yields. Wait and operation time
    are both within the caller's deadline. ``concurrency`` counts in-flight
    operations (including subrequests), not queued waiters.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Any] | None = None):
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    @staticmethod
    def _validate(rate_limit_rps: float | int | None, concurrency: int | None) -> tuple[float, int | None]:
        if rate_limit_rps is None:
            rate_limit_rps = 0
        if (isinstance(rate_limit_rps, bool) or not isinstance(rate_limit_rps, (int, float))
                or not math.isfinite(rate_limit_rps) or rate_limit_rps < 0):
            raise ValueError("rate_limit_rps must be a nonnegative finite number")
        if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int)
                                        or concurrency < 1):
            raise ValueError("concurrency must be a positive integer")
        return float(rate_limit_rps), concurrency

    @asynccontextmanager
    async def admit(self, provider: str, account_id: int, *, quota_group: str | None = None,
                    rate_limit_rps: float | int | None = None, concurrency: int | None = None,
                    deadline: float | None = None) -> AsyncIterator[None]:
        """Acquire a shared request slot; release on success, error or cancel.

        ``deadline`` uses the same monotonic clock as this limiter. Callers
        pass their provider context's deadline, and still wrap the actual
        network operation with its remaining timeout.
        """
        rate, limit = self._validate(rate_limit_rps, concurrency)
        key = (provider, quota_group or str(account_id))
        bucket = self._buckets.setdefault(key, _Bucket())
        # Account and group modes cannot silently change concurrency with
        # requests in flight. Mode/config edits require a new limiter.
        if bucket.concurrency_limit != limit:
            async with bucket.lock:
                if bucket.concurrency_limit != limit:
                    if bucket.concurrency is not None:
                        raise ValueError("concurrency changed while limiter active")
                    if limit is not None:
                        bucket.concurrency = asyncio.Semaphore(limit)
                        bucket.concurrency_limit = limit
        semaphore = bucket.concurrency
        if semaphore is not None:
            await self._within_deadline(semaphore.acquire(), deadline)
        try:
            async with bucket.lock:
                now = self._clock()
                wait = max(0.0, bucket.next_at - now)
                if wait:
                    if deadline is not None and bucket.next_at >= deadline:
                        raise TimeoutError("Request deadline exceeded before provider call")
                    await self._within_deadline(self._sleep(wait), deadline)
                if deadline is not None and self._clock() >= deadline:
                    raise TimeoutError("Request deadline exceeded before provider call")
                # The spacing is per *outgoing* operation; subrequests reserve
                # separate starts. Never relax a shared provider limit when
                # two accounts in one quota group carry different overrides.
                bucket.rate_limit_rps = max(bucket.rate_limit_rps or 0, rate)
                bucket.next_at = (self._clock() + 1 / bucket.rate_limit_rps
                                  if bucket.rate_limit_rps else self._clock())
            yield
        finally:
            if semaphore is not None:
                semaphore.release()

    async def _within_deadline(self, work: Any, deadline: float | None) -> Any:
        if deadline is None:
            return await work
        remaining = deadline - self._clock()
        if remaining <= 0:
            # Coroutine-backed awaitables must be closed if they are never
            # scheduled to avoid unawaited-coroutine warnings.
            if hasattr(work, "close"):
                work.close()
            raise TimeoutError("Request deadline exceeded before provider call")
        async with asyncio.timeout(remaining):
            return await work
