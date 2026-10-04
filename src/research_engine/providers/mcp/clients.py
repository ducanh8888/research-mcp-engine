"""Small account-specific FastMCP client pool for official hosted providers."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from pydantic import AnyUrl

from .oauth import AccountTokenStorage, NonSerializingOAuthClientProvider, OAuthFlowCoordinator, SecretStorage

HOSTED_ENDPOINTS = {
    "scite": "https://api.scite.ai/mcp",
    "elicit": "https://elicit.com/api/mcp",
    "undermind": "https://mcp.undermind.ai/mcp",
    "consensus": "https://mcp.consensus.app/mcp",
}
HOSTED_SCOPES = {
    "scite": ["mcp"],
    "elicit": ["elicit.mcp"],
    "undermind": ["mcp"],
    "consensus": ["search", "profile"],
}


class _NoForwardTransport(StreamableHttpTransport):
    """Static account headers only, adapted from mcp-gateway upstream.py."""

    def _prepare_headers(self) -> dict[str, str]:
        return dict(self.headers or {})


class AccountMCPClientManager:
    """Account identity, credentials and upstream sessions never share a key."""

    def __init__(
        self,
        secrets: SecretStorage,
        base_url: str,
        *,
        callback_path: str = "/admin/oauth/callback",
        oauth_timeout: float = 300,
        client_factory: Any = Client,
    ) -> None:
        self.secrets = secrets
        self.base_url = base_url.rstrip("/")
        self.callback_path = callback_path
        self.client_factory = client_factory
        self.oauth = OAuthFlowCoordinator(oauth_timeout)
        self._clients: dict[int, tuple[str, Any]] = {}

    async def credentials(self, account_id: int) -> dict:
        return await asyncio.to_thread(self.secrets.get, account_id, "credentials")

    def _oauth_provider(self, account_id: int, url: str, options: dict, *, force: bool = False) -> Any:
        redirect_uri = f"{self.base_url}{self.callback_path}"
        provider = next((name for name, endpoint in HOSTED_ENDPOINTS.items() if endpoint == url), None)
        scopes = options.get("scopes", HOSTED_SCOPES.get(provider, []))
        scope = scopes if isinstance(scopes, str) else " ".join(scopes)
        metadata = OAuthClientMetadata(
            client_name="Research Engine",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if options.get("client_secret") else "none",
            scope=scope or None,
        )
        static_info = None
        if options.get("client_id"):
            static_info = OAuthClientInformationFull(
                client_id=options["client_id"],
                client_secret=options.get("client_secret"),
                redirect_uris=[AnyUrl(redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method=metadata.token_endpoint_auth_method,
                scope=scope or None,
            )
        redirect, callback = self.oauth.handlers(account_id)
        kwargs = {
            "server_url": url,
            "client_metadata": metadata,
            "storage": AccountTokenStorage(self.secrets, account_id, static_client_info=static_info, ignore_tokens=force),
            "redirect_handler": redirect,
            "callback_handler": callback,
        }
        # CIMD URLs must be supplied explicitly; private engine URLs are not
        # assumed to be reachable by a provider's authorization server.
        if options.get("client_metadata_url"):
            if "client_metadata_url" not in inspect.signature(NonSerializingOAuthClientProvider.__mro__[1]).parameters:
                raise ValueError("The installed MCP SDK does not support client metadata documents")
            kwargs["client_metadata_url"] = options["client_metadata_url"]
        return NonSerializingOAuthClientProvider(**kwargs)

    def _build_client(self, account_id: int, url: str, options: dict, *, force: bool = False) -> Any:
        auth_type = options.get("auth_type", options.get("type"))
        token = options.get("api_key") or options.get("token") or options.get("access_token")
        if auth_type is None:
            auth_type = "bearer" if token else ("oauth" if url in HOSTED_ENDPOINTS.values() else "none")
        headers = {str(key): str(value) for key, value in options.get("headers", {}).items()}
        auth = None
        if auth_type in {"bearer", "api_key"}:
            if not token:
                raise ValueError("A bearer account requires a stored API key or token")
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "oauth":
            auth = self._oauth_provider(account_id, url, options, force=force)
        elif auth_type not in {"headers", "none"}:
            raise ValueError(f"Unsupported upstream authentication type: {auth_type}")
        transport = _NoForwardTransport(url, headers=headers or None, auth=auth)
        return self.client_factory(transport)

    async def get_client(self, account_id: int, url: str, credentials: dict | None = None) -> Any:
        options = await self.credentials(account_id) if credentials is None else credentials
        fingerprint = hashlib.sha256(json.dumps([url, options], sort_keys=True, default=str).encode()).hexdigest()
        cached = self._clients.get(account_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        client = self._build_client(account_id, url, options)
        self._clients[account_id] = fingerprint, client
        return client

    async def list_tools(self, account_id: int, url: str, *, timeout: float = 20, credentials: dict | None = None) -> list:
        client = await self.get_client(account_id, url, credentials)
        async with asyncio.timeout(timeout), client:
            return await client.list_tools()

    async def call_tool(
        self,
        account_id: int,
        url: str,
        name: str,
        arguments: dict,
        *,
        timeout: float = 60,
        credentials: dict | None = None,
    ) -> Any:
        client = await self.get_client(account_id, url, credentials)
        async with asyncio.timeout(timeout), client:
            return await client.call_tool(name, arguments)

    async def start_oauth(self, account_id: int, url: str, credentials: dict | None = None, *, force: bool = True) -> dict:
        options = dict(await self.credentials(account_id) if credentials is None else credentials)
        options["auth_type"] = "oauth"

        async def connector() -> None:
            client = self._build_client(account_id, url, options, force=force)
            async with client:
                await client.list_tools()
            await self.invalidate(account_id)

        return await self.oauth.begin(account_id, connector)

    def oauth_callback(self, state: str, code: str | None = None, error: str | None = None, issuer: str | None = None) -> dict:
        return self.oauth.callback(state, code=code, error=error, issuer=issuer)

    def oauth_status(self, account_id: int) -> dict:
        return self.oauth.status(account_id)

    async def invalidate(self, account_id: int) -> None:
        # Clients use scoped contexts per call, so idle cached clients have no
        # open sessions. Active calls finish with their original credentials.
        self._clients.pop(account_id, None)

    async def close(self) -> None:
        await self.oauth.close()
        self._clients.clear()


MCPClientManager = AccountMCPClientManager
