"""Private MCP request authentication with immediate bearer-token revocation."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from research_engine.storage.db import lookup_token


client_token_id: ContextVar[int | None] = ContextVar("research_client_token_id", default=None)


class MCPAuthentication:
    def __init__(self, app, *, db, settings):
        self.app, self.db, self.settings = app, db, settings
        self.origins = set(settings.trusted_origins) | {settings.public_base_url}
        self.hosts = {urlsplit(origin).hostname for origin in self.origins} | {"localhost", "127.0.0.1", "::1"}
        self.hosts.update(host.strip() for host in settings.host.split(",") if host.strip() != "0.0.0.0")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value.decode("latin-1") for key, value in scope["headers"]}
        try:
            host = urlsplit("//" + headers.get(b"host", "")).hostname
        except ValueError:
            host = None
        if host not in self.hosts:
            await JSONResponse({"error": "Untrusted Host"}, status_code=403)(scope, receive, send)
            return
        origin = headers.get(b"origin")
        if origin is not None and origin.rstrip("/") not in self.origins:
            await JSONResponse({"error": "Untrusted Origin"}, status_code=403)(scope, receive, send)
            return
        scheme, _, raw = headers.get(b"authorization", "").partition(" ")
        token = None
        if scheme.lower() == "bearer" and raw and len(raw) <= 512:
            token = await asyncio.to_thread(lookup_token, self.db, raw)
        if token is None:
            response = JSONResponse({"error": "A valid client bearer token is required"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Bearer realm="research-engine"'})
            await response(scope, receive, send)
            return
        context = client_token_id.set(token.id)
        try:
            await self.app(scope, receive, send)
        finally:
            client_token_id.reset(context)
