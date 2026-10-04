"""Account-scoped upstream OAuth, adapted from R0Wi/mcp-gateway @59c1efd.

Only upstream client authentication is included. This module does not issue
tokens to engine clients or expose an authorization server.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

try:
    from mcp.shared.auth import AuthorizationCodeResult
except ImportError:  # MCP SDK 1.x returns a (code, state) tuple.
    AuthorizationCodeResult = None  # type: ignore[assignment,misc]


class SecretStorage(Protocol):
    def get(self, account_id: int, kind: str = "credentials") -> dict: ...

    def set(self, account_id: int, value: dict, kind: str = "credentials") -> None: ...


class NotConnectedError(RuntimeError):
    """An upstream account requires an administrator to authorize it."""


class OAuthStateError(ValueError):
    """An authorization callback does not match an active, unused state."""


class AccountTokenStorage:
    """Persist SDK tokens and registration through the encrypted secret store."""

    def __init__(
        self,
        secrets: SecretStorage,
        account_id: int,
        *,
        static_client_info: OAuthClientInformationFull | None = None,
        ignore_tokens: bool = False,
    ) -> None:
        self.secrets = secrets
        self.account_id = account_id
        self.static_client_info = static_client_info
        self.ignore_tokens = ignore_tokens

    async def get_tokens(self) -> OAuthToken | None:
        if self.ignore_tokens:
            return None
        stored = await asyncio.to_thread(self.secrets.get, self.account_id, "oauth_tokens")
        if not stored:
            return None
        data = dict(stored)
        expires_at = data.pop("expires_at", None)
        if expires_at is not None:
            data["expires_in"] = int(float(expires_at) - time.time())
        return OAuthToken.model_validate(data)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = tokens.model_dump(mode="json", exclude_none=True)
        # Refresh-token rotation is optional: omission retains the existing token.
        previous = await asyncio.to_thread(self.secrets.get, self.account_id, "oauth_tokens")
        if not data.get("refresh_token") and previous.get("refresh_token"):
            data["refresh_token"] = previous["refresh_token"]
        if tokens.expires_in is not None:
            data["expires_at"] = time.time() + tokens.expires_in
        await asyncio.to_thread(self.secrets.set, self.account_id, data, "oauth_tokens")

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if self.static_client_info is not None:
            return self.static_client_info
        data = await asyncio.to_thread(self.secrets.get, self.account_id, "oauth_client")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if self.static_client_info is not None:
            return
        await asyncio.to_thread(
            self.secrets.set,
            self.account_id,
            client_info.model_dump(mode="json", exclude_none=True),
            "oauth_client",
        )


class _AuthStateLock:
    """A task-owned mutex which can safely release during transport waiting."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None

    async def acquire(self) -> None:
        await self._lock.acquire()
        self._owner = asyncio.current_task()

    def release_owned(self) -> bool:
        if self._owner is not asyncio.current_task():
            return False
        self._owner = None
        self._lock.release()
        return True

    async def __aenter__(self) -> _AuthStateLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_: Any) -> None:
        # Cancellation while another request owns the mutex must not release it.
        self.release_owned()


class NonSerializingOAuthClientProvider(OAuthClientProvider):
    """Keep SDK discovery/PKCE/refresh, without locking ordinary MCP requests.

    The SDK's OAuth generator holds its context mutex across each yielded HTTP
    request. Release it only for the original protected-resource request; token
    exchange, refresh and authorization continue under the per-account mutex.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auth_state_lock = _AuthStateLock()
        self.context.lock = self._auth_state_lock

    async def _initialize(self) -> None:
        await super()._initialize()
        if self.context.current_tokens is not None:
            self.context.update_token_expiry(self.context.current_tokens)

    async def _exchange_token_authorization_code(self, *args: Any, **kwargs: Any) -> Any:
        request = await super()._exchange_token_authorization_code(*args, **kwargs)
        request.headers["accept"] = "application/json"
        return request

    async def _refresh_token(self, *args: Any, **kwargs: Any) -> Any:
        request = await super()._refresh_token(*args, **kwargs)
        request.headers["accept"] = "application/json"
        return request

    async def _drive(self, flow: AsyncGenerator, original_request: Any) -> AsyncGenerator:
        try:
            outbound = await anext(flow)
            while True:
                released = outbound is original_request and self._auth_state_lock.release_owned()
                response = yield outbound
                if released:
                    await self._auth_state_lock.acquire()
                outbound = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()

    async def _auth_flow(self, request: Any) -> AsyncGenerator:
        flow = self._drive(super()._auth_flow(request), request)
        try:
            outbound = await anext(flow)
            while True:
                response = yield outbound
                outbound = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()

    async def async_auth_flow(self, request: Any) -> AsyncGenerator:
        if hasattr(OAuthClientProvider, "_auth_flow"):
            flow = super().async_auth_flow(request)
        else:
            flow = self._drive(super().async_auth_flow(request), request)
        try:
            outbound = await anext(flow)
            while True:
                response = yield outbound
                outbound = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()


@dataclass
class _PendingFlow:
    account_id: int
    code: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    state: str | None = None
    authorization_url: str | None = None


class OAuthFlowCoordinator:
    """Short-lived exact-state callback routing; persisted tokens live elsewhere."""

    def __init__(self, timeout: float = 300) -> None:
        self.timeout = timeout
        self.pending: dict[int, _PendingFlow] = {}
        self.states: dict[str, _PendingFlow] = {}
        self.results: dict[int, dict] = {}

    def handlers(self, account_id: int) -> tuple[Callable, Callable]:
        async def redirect(url: str) -> None:
            flow = self.pending.get(account_id)
            if flow is None:
                raise NotConnectedError("Authorize this upstream account from the admin interface")
            values = parse_qs(urlparse(url).query).get("state", [])
            if len(values) != 1 or not values[0] or values[0] in self.states:
                raise OAuthStateError("Upstream authorization did not supply a unique state")
            flow.state = values[0]
            flow.authorization_url = url
            self.states[flow.state] = flow
            flow.ready.set()

        async def callback() -> Any:
            flow = self.pending.get(account_id)
            if flow is None:
                raise NotConnectedError("No interactive authorization is active")
            return await asyncio.wait_for(asyncio.shield(flow.code), self.timeout)

        return redirect, callback

    async def begin(self, account_id: int, connector: Callable[[], Awaitable[None]]) -> dict:
        if account_id in self.pending:
            raise OAuthStateError("An authorization flow is already active for this account")
        flow = _PendingFlow(account_id)
        self.pending[account_id] = flow
        self.results[account_id] = {"account_id": account_id, "status": "connecting"}

        async def run() -> None:
            try:
                await asyncio.wait_for(connector(), self.timeout)
                self.results[account_id] = {"account_id": account_id, "status": "connected"}
            except asyncio.CancelledError:
                self.results[account_id] = {"account_id": account_id, "status": "cancelled"}
                raise
            except Exception as exc:
                self.results[account_id] = {
                    "account_id": account_id,
                    "status": "failed",
                    "error": type(exc).__name__,
                    "detail": "Upstream authorization or connection failed",
                }
            finally:
                if flow.state:
                    self.states.pop(flow.state, None)
                self.pending.pop(account_id, None)
                flow.ready.set()

        flow.task = asyncio.create_task(run(), name=f"oauth-connect-{account_id}")
        try:
            await asyncio.wait_for(flow.ready.wait(), min(self.timeout, 30))
        except TimeoutError as exc:
            flow.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flow.task
            raise NotConnectedError("Upstream did not respond before the connection deadline") from exc
        if flow.authorization_url:
            return {
                "account_id": account_id,
                "status": "authorization_pending",
                "authorization_url": flow.authorization_url,
                "state": flow.state,
            }
        return self.status(account_id)

    def callback(self, state: str, code: str | None = None, error: str | None = None, issuer: str | None = None) -> dict:
        flow = self.states.pop(state, None)
        if flow is None or flow.code.done():
            raise OAuthStateError("Unknown, expired or already used OAuth state")
        if error or not code:
            flow.code.set_exception(NotConnectedError("Upstream authorization was denied"))
            return {"account_id": flow.account_id, "status": "failed"}
        if AuthorizationCodeResult is None:
            response: Any = (code, state)
        else:
            response = AuthorizationCodeResult(code=code, state=state, iss=issuer)
        flow.code.set_result(response)
        return {"account_id": flow.account_id, "status": "exchanging_token"}

    def status(self, account_id: int) -> dict:
        flow = self.pending.get(account_id)
        if flow and flow.state:
            return {"account_id": account_id, "status": "authorization_pending", "state": flow.state}
        return dict(self.results.get(account_id, {"account_id": account_id, "status": "not_connected"}))

    async def close(self) -> None:
        tasks = [flow.task for flow in self.pending.values() if flow.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
