"""Authentication and request boundary checks through a real HTTP server."""

from __future__ import annotations

import re

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


async def test_dashboard_login_session_csrf_logout_and_write_only_credentials(running_server: Runtime):
    async with httpx.AsyncClient(base_url=running_server.url, follow_redirects=False) as http:
        entry = await http.get("/admin")
        assert entry.status_code == 307 and entry.headers["location"] == "/admin/"
        anonymous = await http.get("/admin/")
        assert anonymous.status_code in {302, 303, 307}
        login = await http.get("/admin/login")
        assert login.status_code == 200 and "no-store" in login.headers["cache-control"]
        assert "csrf_token" in login.text
        from bs4 import BeautifulSoup
        csrf = BeautifulSoup(login.text, "html.parser").find("input", {"name": "csrf_token"})["value"]
        wrong = await http.post("/admin/login", data={"username": "admin", "password": "wrong",
                                                       "csrf_token": csrf})
        assert wrong.status_code in {200, 400, 401} and "no-store" in wrong.headers["cache-control"]
        assert (await http.get("/admin/")).status_code in {302, 303, 307}
        login = await http.post("/admin/login", data={"username": "admin", "password": "temporary-admin-password",
                                                       "csrf_token": csrf})
        assert login.status_code in {302, 303} and "research_admin" in login.headers["set-cookie"]
        assert "no-store" in login.headers["cache-control"]
        dashboard = await http.get("/admin/")
        assert dashboard.status_code == 200 and "no-store" in dashboard.headers["cache-control"]
        operations = await http.get("/admin/operations")
        assert operations.status_code == 200 and "no-store" in operations.headers["cache-control"]
        assert "sensitive-access-token" not in operations.text
        accounts = await http.get("/admin/account/list")
        assert accounts.status_code == 200 and "no-store" in accounts.headers["cache-control"]
        assert "api_key" not in accounts.text and "refresh_token" not in accounts.text
        missing_csrf = await http.post("/admin/operations", data={"command": "clear_cache", "cache": "query"})
        assert missing_csrf.status_code == 403
        operations_csrf = re.search(r'name="csrf_token" value="([^"]+)"', operations.text).group(1)
        valid_csrf = await http.post("/admin/operations", data={"command": "clear_cache", "cache": "query",
                                                                 "csrf_token": operations_csrf})
        assert valid_csrf.status_code == 200
        assert '"query"' in valid_csrf.text and "no-store" in valid_csrf.headers["cache-control"]
        logout = await http.get("/admin/logout")
        assert logout.status_code == 200
        logout_csrf = BeautifulSoup(logout.text, "html.parser").find("input", {"name": "csrf_token"})["value"]
        assert (await http.post("/admin/logout", data={"csrf_token": logout_csrf})).status_code in {302, 303}
        assert (await http.get("/admin/")).status_code in {302, 303, 307}
