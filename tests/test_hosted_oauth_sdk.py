"""SDK 2.3.0 OAuth shim regression for restart, concurrency and cancellation."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from research_engine.providers.mcp.oauth import NonSerializingOAuthClientProvider
from test_hosted_mcp import MemorySecrets
from research_engine.providers.mcp.oauth import AccountTokenStorage


async def provider(*, expires_in: int = 600):
    secrets = MemorySecrets()
    storage = AccountTokenStorage(secrets, 1)
    await storage.set_client_info(OAuthClientInformationFull(client_id="sdk-client"))
    await storage.set_tokens(OAuthToken(access_token="first", refresh_token="refresh", expires_in=expires_in))
    auth = NonSerializingOAuthClientProvider(server_url="https://mcp.example.org/mcp", storage=storage,
              client_metadata=OAuthClientMetadata(client_name="Research MCP",
                 redirect_uris=[AnyUrl("https://callback.example.org/oauth/callback")]))
    return auth, secrets


async def test_sdk_valid_token_requests_can_overlap_and_no_interactive_callback():
    auth, _ = await provider()
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        assert request.headers["authorization"] == "Bearer first"
        calls += 1
        if calls == 2:
            both_started.set()
        await release.wait()
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handle)) as client:
        first = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        second = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        await asyncio.wait_for(both_started.wait(), 1)
        release.set()
        assert [response.status_code for response in await asyncio.gather(first, second)] == [200, 200]
    assert calls == 2


async def test_sdk_expired_token_refresh_once_after_restart():
    auth, secrets = await provider(expires_in=1)
    tokens = secrets.get(1, "oauth_tokens")
    tokens["expires_at"] = time.time() - 20
    secrets.set(1, tokens, "oauth_tokens")
    calls = []

    async def handle(request):
        calls.append(request)
        if request.url.path == "/token":
            return httpx2.Response(200, json={"access_token": "rotated", "refresh_token": "next",
                                                "expires_in": 600, "token_type": "Bearer"})
        assert request.headers["authorization"] == "Bearer rotated"
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handle)) as client:
        assert (await client.get("https://mcp.example.org/mcp")).status_code == 200
        assert (await client.get("https://mcp.example.org/mcp")).status_code == 200
    assert [req.url.path for req in calls].count("/token") == 1
    assert secrets.get(1, "oauth_tokens")["refresh_token"] == "next"


async def test_sdk_rotating_refresh_with_two_expired_requests_still_refreshes_once():
    auth, secrets = await provider(expires_in=1)
    tokens = secrets.get(1, "oauth_tokens")
    tokens["expires_at"] = time.time() - 10
    secrets.set(1, tokens, "oauth_tokens")
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()
    seen = []

    async def handle(request):
        seen.append(request)
        if request.url.path == "/token":
            refresh_started.set()
            await allow_refresh.wait()
            assert parse_qs(request.content.decode())["refresh_token"] == ["refresh"]
            return httpx2.Response(200, json={"access_token": "next", "refresh_token": "rotated",
                                                "expires_in": 600, "token_type": "Bearer"})
        assert request.headers["authorization"] == "Bearer next"
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handle)) as client:
        first = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        await asyncio.wait_for(refresh_started.wait(), 1)
        second = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        allow_refresh.set()
        assert [response.status_code for response in await asyncio.gather(first, second)] == [200, 200]
    assert [request.url.path for request in seen].count("/token") == 1
    assert secrets.get(1, "oauth_tokens")["refresh_token"] == "rotated"


async def test_cancel_queued_refresh_waiter_does_not_unlock_owner():
    auth, secrets = await provider(expires_in=1)
    tokens = secrets.get(1, "oauth_tokens")
    tokens["expires_at"] = time.time() - 10
    secrets.set(1, tokens, "oauth_tokens")
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refreshes = 0

    async def handle(request):
        nonlocal refreshes
        if request.url.path == "/token":
            refreshes += 1
            refresh_started.set()
            await release_refresh.wait()
            return httpx2.Response(200, json={"access_token": "next", "refresh_token": "rotated",
                                                "expires_in": 600, "token_type": "Bearer"})
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handle)) as client:
        owner = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        await asyncio.wait_for(refresh_started.wait(), 1)
        waiter = asyncio.create_task(client.get("https://mcp.example.org/mcp"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert refreshes == 1
        release_refresh.set()
        assert (await asyncio.wait_for(owner, 1)).status_code == 200
        assert (await asyncio.wait_for(client.get("https://mcp.example.org/mcp"), 1)).status_code == 200
    assert refreshes == 1


async def test_cancel_resource_request_releases_sdk_lock_for_next_call():
    auth, _ = await provider()
    blocked = asyncio.Event()
    started = asyncio.Event()

    async def handle(request):
        if request.url.path == "/blocked":
            started.set()
            await blocked.wait()
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(handle)) as client:
        task = asyncio.create_task(client.get("https://mcp.example.org/blocked"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await asyncio.wait_for(client.get("https://mcp.example.org/mcp"), 1)).status_code == 200
