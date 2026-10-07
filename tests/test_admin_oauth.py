"""Admin-session OAuth state, expiry, callback and error redaction over HTTP."""

from __future__ import annotations

import re
import socket
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup

from research_engine.router.execute import Engine
from research_engine.providers.mcp.oauth import OAuthFlowCoordinator
from research_engine.server.app import create_app
from research_engine.storage.db import Account, Database, ProviderRow
from test_mcp_e2e import make_settings, run_http_app


async def test_upstream_oauth_state_is_session_bound_single_use_and_expires(tmp_path, monkeypatch):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "runtime", sock.getsockname()[1])
    db = Database(settings.database_path)
    db.initialize()
    with db.session() as session:
        session.add(ProviderRow(name="scite_mcp", capabilities=["paper_search"]))
        session.flush()
        account = Account(provider="scite_mcp", label="oauth-test", credential="ok")
        session.add(account)
        session.flush()
        account_id = account.id

    class OAuthManager:
        def __init__(self):
            self.oauth = OAuthFlowCoordinator(timeout=2)
            self.callbacks = []
            self.state = "state-one"

        async def start_oauth(self, **kwargs):
            return {"state": self.state, "authorization_url":
                    f"https://authorize.example.org/?state={self.state}"}

        def oauth_callback(self, **kwargs):
            self.callbacks.append(kwargs)
            return {"account_id": account_id, "status": "exchanging_token"}

        async def close(self):
            pass

    manager = OAuthManager()
    engine = Engine(settings)
    engine.mcp_manager = manager
    app = create_app(settings, engine=engine)
    # The HTTP fixture tests the same dashboard route as the real manager, but
    # keeps external consent and token exchange inside this synthetic coordinator.
    try:
        with run_http_app(app, sock):
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.port}",
                                         follow_redirects=False) as owner, httpx.AsyncClient(
                                             base_url=f"http://127.0.0.1:{settings.port}",
                                             follow_redirects=False) as outsider:
                login = await owner.get("/admin/login")
                csrf = BeautifulSoup(login.text, "html.parser").find("input", {"name": "csrf_token"})["value"]
                assert (await owner.post("/admin/login", data={"username": "admin",
                    "password": "temporary-admin-password", "csrf_token": csrf})).status_code in {302, 303}
                operations = await owner.get("/admin/operations")
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', operations.text).group(1)
                started = await owner.post("/admin/operations", data={"command": "oauth_connect",
                    "account_id": str(account_id), "mcp_url": "https://api.scite.ai/mcp", "csrf_token": csrf})
                assert started.status_code == 303
                state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
                assert state == "state-one"
                assert (await outsider.get(f"/admin/oauth/callback?state={state}&code=fake")).status_code in {302, 303}
                callback = await owner.get(f"/admin/oauth/callback?state={state}&code=fake")
                assert callback.status_code == 200 and "no-store" in callback.headers["cache-control"]
                assert manager.callbacks == [{"state": state, "code": "fake", "error": None, "issuer": None}]
                reused = await owner.get(f"/admin/oauth/callback?state={state}&code=fake")
                assert reused.status_code == 400 and "no-store" in reused.headers["cache-control"]
                assert "fake" not in reused.text and "state-one" not in reused.text
                manager.state = "state-two"
                started = await owner.post("/admin/operations", data={"command": "oauth_connect",
                    "account_id": str(account_id), "mcp_url": "https://api.scite.ai/mcp", "csrf_token": csrf})
                assert started.status_code == 303
                expired_state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
                from research_engine.admin import oauth_connect
                started_at = time.time()
                monkeypatch.setattr(oauth_connect.time, "time", lambda: started_at + 3)
                expired = await owner.get(f"/admin/oauth/callback?state={expired_state}&code=fake")
                assert expired.status_code == 400 and "no-store" in expired.headers["cache-control"]
                assert len(manager.callbacks) == 1
                assert expired_state not in expired.text
    finally:
        db.close()
