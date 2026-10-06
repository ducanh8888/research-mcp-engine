"""Controlled-clock direct provider operation limiter fixtures; no network calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import pytest

from research_engine.providers.base import CallContext
from research_engine.router.limiter import RequestLimiter


@dataclass
class FakeClock:
    now: float = 1.0
    delays: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def test_spacing_applies_to_each_subrequest_and_shared_account_group():
    clock = FakeClock()
    limiter = RequestLimiter(clock=clock.time, sleep=clock.sleep)
    started = []
    for account in [11, 12, 11]:
        async with limiter.admit("openalex", account, quota_group="common", rate_limit_rps=2):
            started.append(clock.now)
    assert started == [1, 1.5, 2]
    assert clock.delays == [0.5, 0.5]
    async with limiter.admit("other", 11, quota_group="common", rate_limit_rps=2):
        assert clock.now == 2  # a different provider does not share a quota group
    async with limiter.admit("openalex", 22, quota_group="independent", rate_limit_rps=2):
        assert clock.now == 2


async def test_concurrency_bound_covers_inflight_operations_and_releases_after_cancel():
    limiter = RequestLimiter()
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with limiter.admit("s2", 1, quota_group="shared", concurrency=1):
            entered.set()
            await release.wait()

    async def second():
        async with limiter.admit("s2", 2, quota_group="shared", concurrency=1):
            second_entered.set()

    task = asyncio.create_task(first())
    await entered.wait()
    next_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(next_task, timeout=1)
    assert second_entered.is_set()
    release.set()


async def test_deadline_during_rate_wait_does_not_reserve_later_slot():
    clock = FakeClock()
    limiter = RequestLimiter(clock=clock.time, sleep=clock.sleep)
    async with limiter.admit("arxiv", 1, rate_limit_rps=1, deadline=5):
        pass
    with pytest.raises(TimeoutError, match="deadline"):
        async with limiter.admit("arxiv", 1, rate_limit_rps=1, deadline=1.5):
            pytest.fail("no provider call after deadline")
    assert clock.delays == []
    async with limiter.admit("arxiv", 1, rate_limit_rps=1, deadline=5):
        assert clock.now == 2


async def test_each_http_retry_acquires_new_rate_slot():
    clock = FakeClock(now=time.monotonic())
    limiter = RequestLimiter(clock=clock.time, sleep=clock.sleep)
    calls = []

    def respond(request):
        calls.append(clock.now)
        return httpx.Response(503 if len(calls) == 1 else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ctx = CallContext(client, {}, 1, "arxiv", clock.now + 10, {"rate_limit_rps": 1}, limiter=limiter)
        response = await ctx.request("GET", "https://export.arxiv.org/api/query")
    assert response.status_code == 200
    assert calls == [clock.now - 1, clock.now]


async def test_operator_concurrency_edit_applies_after_inflight_call():
    limiter = RequestLimiter()
    async with limiter.admit("scholar", 1, concurrency=2):
        pass
    async with limiter.admit("scholar", 1, concurrency=1):
        assert limiter._buckets[("scholar", "account:1")].concurrency_limit == 1
    async with limiter.admit("scholar", 1, concurrency=None):
        assert limiter._buckets[("scholar", "account:1")].concurrency_limit is None


async def test_group_rate_never_relaxes_when_second_account_has_higher_rps():
    clock = FakeClock()
    limiter = RequestLimiter(clock=clock.time, sleep=clock.sleep)
    starts = []
    for account, rate in ((1, 1), (2, 10), (1, 10)):
        async with limiter.admit("scholar", account, quota_group="shared", rate_limit_rps=rate):
            starts.append(clock.now)
    assert starts == [1, 2, 3]


async def test_limit_options_reject_invalid_values_without_calling_provider():
    limiter = RequestLimiter()
    for invalid in [-1, float("nan"), True]:
        with pytest.raises(ValueError, match="rate_limit_rps"):
            async with limiter.admit("provider", 1, rate_limit_rps=invalid):
                pytest.fail("invalid rate admitted")
    for invalid in [0, True, 1.5]:
        with pytest.raises(ValueError, match="concurrency"):
            async with limiter.admit("provider", 1, concurrency=invalid):
                pytest.fail("invalid concurrency admitted")
