"""Authentication and request boundary checks through a real HTTP server."""

from __future__ import annotations

import httpx
import pytest

from research_engine.storage.db import ClientToken, Database
from test_mcp_e2e import Runtime, running_server as running_server


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "auth-boundary-test", "version": "1"},
    },
}
ACCEPT = "application/json, text/event-stream"


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic invalid"])
async def test_every_mcp_method_requires_valid_bearer(
    running_server: Runtime, method: str, authorization: str | None,
):
    headers = {"Accept": ACCEPT}
    if authorization:
        headers["Authorization"] = authorization
    async with httpx.AsyncClient() as http:
        response = await http.request(
            method, running_server.url + "/mcp", headers=headers,
            json=INITIALIZE if method == "POST" else None,
        )
    assert response.status_code == 401
    assert "Bearer" in response.headers.get("WWW-Authenticate", "")


async def test_revoked_token_is_rejected_on_next_transport_request(running_server: Runtime):
    async with running_server.client() as client:
        assert await client.list_tools()
    db = Database(running_server.settings.database_path)
    try:
        with db.session() as session:
            token = session.get(ClientToken, running_server.token_id)
            assert token is not None
            token.revoked = True
        async with httpx.AsyncClient() as http:
            response = await http.post(
                running_server.url + "/mcp", json=INITIALIZE,
                headers={"Accept": ACCEPT, "Authorization": f"Bearer {running_server.token}"},
            )
        assert response.status_code == 401
    finally:
        db.close()


@pytest.mark.parametrize("origin", ["https://evil.invalid", "null", "http://localhost:1"])
async def test_bad_origin_is_rejected_with_valid_token(running_server: Runtime, origin: str):
    async with httpx.AsyncClient() as http:
        response = await http.post(
            running_server.url + "/mcp", json=INITIALIZE,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {running_server.token}",
                     "Origin": origin},
        )
    assert response.status_code == 403


async def test_untrusted_host_is_rejected_with_valid_token(running_server: Runtime):
    async with httpx.AsyncClient() as http:
        response = await http.post(
            running_server.url + "/mcp", json=INITIALIZE,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {running_server.token}",
                     "Host": "evil.invalid:8765"},
        )
    assert response.status_code in {400, 403}


async def test_matching_origin_allows_mcp_initialize(running_server: Runtime):
    async with httpx.AsyncClient() as http:
        response = await http.post(
            running_server.url + "/mcp", json=INITIALIZE,
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {running_server.token}",
                     "Origin": running_server.url},
        )
    assert response.status_code == 200
    assert "serverInfo" in response.text
