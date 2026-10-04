"""Bind upstream OAuth to the authenticated administrator session."""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import anyio
from starlette.requests import Request

from research_engine.storage.db import Account, Database, ProviderRow


class OAuthConnect:
    def __init__(self, manager: Any, db: Database, secret_store: Any):
        self.manager = manager
        self.db = db
        self.secret_store = secret_store

    async def start(self, request: Request, account_id: int, url: str = "") -> str:
        def account_config() -> tuple[str, dict[str, Any]]:
            with self.db.session() as session:
                account = session.get(Account, account_id)
                if account is None:
                    raise ValueError("Account does not exist")
                provider = session.get(ProviderRow, account.provider)
                options = provider.options if provider is not None else {}
                credentials = self.secret_store.get(account_id)
                configured_url = url or credentials.get("mcp_url") or options.get("mcp_url", "")
                return configured_url, credentials

        configured_url, credentials = await anyio.to_thread.run_sync(account_config)
        if not configured_url:
            raise ValueError("Set the account mcp_url or enter the provider MCP URL")
        parsed = urlsplit(configured_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("The provider MCP URL must be an HTTP or HTTPS URL")
        result = await self.manager.start_oauth(
            account_id=account_id, url=configured_url, credentials=credentials, force=True,
        )
        state = result.get("state")
        authorization_url = result.get("authorization_url")
        if not state or not authorization_url:
            raise ValueError("Provider did not return an OAuth authorization URL and state")
        authorization = urlsplit(authorization_url)
        if authorization.scheme not in {"http", "https"} or not authorization.hostname:
            raise ValueError("Provider returned an invalid OAuth authorization URL")
        now = time.time()
        pending = {
            key: value for key, value in request.session.get("oauth_states", {}).items()
            if now - value.get("started", 0) < 600
        }
        if len(pending) >= 5:
            pending.pop(next(iter(pending)))
        pending[state] = {"account_id": account_id, "started": now}
        request.session["oauth_states"] = pending
        return authorization_url

    async def callback(self, request: Request) -> dict[str, Any]:
        state = request.query_params.get("state", "")
        pending = request.session.get("oauth_states", {})
        matching = next((issued for issued in pending if secrets.compare_digest(issued, state)), None)
        if matching is None:
            raise ValueError("OAuth state does not belong to this admin session")
        flow = pending.pop(matching)
        request.session["oauth_states"] = pending
        if time.time() - flow["started"] >= 600:
            raise ValueError("OAuth connect expired; start again")
        result = await self.manager.oauth_callback(
            state=state,
            code=request.query_params.get("code"),
            error=request.query_params.get("error"),
            issuer=request.query_params.get("iss"),
        )
        if result.get("account_id") != flow["account_id"]:
            raise ValueError("OAuth callback account does not match its session")
        return {"account_id": result["account_id"], "status": result.get("status", "connected")}
