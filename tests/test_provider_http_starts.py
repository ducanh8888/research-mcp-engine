"""Transport retries for read operations cannot duplicate side-effecting starts."""

from __future__ import annotations

import time

import httpx
import pytest

from research_engine.providers.base import CallContext, Capability, ErrorKind, ProviderError
from research_engine.providers.http import request
from research_engine.providers.scholar.elicit_api import ElicitAPIProvider


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_non_idempotent_post_is_sent_once_on_server_error(status: int):
    calls = []

    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await request(client, "POST", "https://elicit.com/api/v2/sessions/reports",
                                 deadline=time.monotonic() + 2)
    assert response.status_code == status
    assert len(calls) == 1


async def test_post_lost_response_is_sent_once():
    calls = []

    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ReadTimeout("response lost", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError) as error:
            await request(client, "POST", "https://elicit.com/api/v2/sessions/reports",
                          deadline=time.monotonic() + 2)
    assert error.value.kind == ErrorKind.TRANSIENT
    assert len(calls) == 1


async def test_read_retries_remain_available():
    calls = []

    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(503 if len(calls) == 1 else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        response = await request(client, "GET", "https://elicit.com/api/v2/usage",
                                 deadline=time.monotonic() + 2)
    assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_elicit_start_server_error_is_ambiguous_and_never_resubmits(status: int):
    calls = []

    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status, json={"error": "upstream failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ctx = CallContext(client, {"api_key": "fixture-key"}, 1, "elicit_api", time.monotonic() + 2)
        with pytest.raises(ProviderError) as error:
            await ElicitAPIProvider().start(Capability.DEEP_LITERATURE_SEARCH, {"question": "research?"}, ctx)
    assert error.value.kind == ErrorKind.TRANSIENT
    assert error.value.ambiguous_start
    assert len(calls) == 1


async def test_elicit_start_lost_response_is_ambiguous_and_never_resubmits():
    calls = []

    def respond(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ReadTimeout("response lost", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ctx = CallContext(client, {"api_key": "fixture-key"}, 1, "elicit_api", time.monotonic() + 2)
        with pytest.raises(ProviderError) as error:
            await ElicitAPIProvider().start(Capability.DEEP_LITERATURE_SEARCH, {"question": "research?"}, ctx)
    assert error.value.ambiguous_start
    assert len(calls) == 1
