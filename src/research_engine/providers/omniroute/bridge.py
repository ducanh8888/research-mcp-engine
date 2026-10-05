"""Minimal explicit-provider OmniRoute HTTP bridge.

OmniRoute source reference: diegosouzapw/OmniRoute @fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b,
open-sse/handlers/{search,webFetch,rerank}.ts. The installed 3.8.51 OpenAPI
exposes the same three operations under /api/v1; public proxies may use /v1.
No upstream account selection, policy, or generic tool passthrough lives here.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

import httpx

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Document, Hit, Result


class BridgeError(ProviderError):
    """A typed failure with the scope required for bridge vs virtual-provider health."""

    def __init__(self, kind: ErrorKind, message: str, *, scope: str = "provider",
                 retry_after: float | None = None, status_code: int | None = None):
        super().__init__(kind, message, retry_after=retry_after, status_code=status_code,
                         block_capability=False if kind in {ErrorKind.BAD_REQUEST, ErrorKind.TARGET} else True)
        self.scope = scope  # connection failures must not poison other virtual providers


def _bad(message: str) -> BridgeError:
    return BridgeError(ErrorKind.BAD_REQUEST, message)


def _endpoint(ctx: CallContext, operation: str) -> str:
    value = ctx.options.get("base_url")
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(ErrorKind.AUTH, "OmniRoute connection URL is not configured", scope="connection")
    parsed = urlsplit(value.strip())
    local_http = parsed.scheme == "http" and (parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                                                  or ctx.options.get("allow_private_http") is True)
    if (not (parsed.scheme == "https" or local_http) or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"/v1", "/api/v1"}):
        raise BridgeError(ErrorKind.BAD_REQUEST, "OmniRoute API root must end in /v1 or /api/v1",
                          scope="connection")
    return value.strip().rstrip("/") + "/" + operation


def _headers(ctx: CallContext) -> dict[str, str]:
    key = ctx.credentials.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise BridgeError(ErrorKind.AUTH, "OmniRoute service credential is not configured", scope="connection")
    # The installed gateway occasionally advertises gzip for uncompressed
    # search bodies; request identity encoding to keep stream decoding reliable.
    return {"Authorization": f"Bearer {key.strip()}", "Accept": "application/json",
            "Accept-Encoding": "identity"}


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _failure(response: httpx.Response, data: Any) -> BridgeError:
    """Never include gateway bodies: they may reflect tokens, URLs or query text."""
    status = response.status_code
    detail = data.get("error", "") if isinstance(data, dict) else ""
    if isinstance(detail, dict):
        detail = " ".join(str(detail.get(field, "")) for field in ("code", "type", "message"))
    detail = detail if isinstance(detail, str) else ""
    quota = any(marker in detail.lower() for marker in ("quota", "credit", "budget", "exhausted"))
    provider_identified = bool(response.headers.get("x-omniroute-provider"))
    if isinstance(data, dict):
        provider_identified |= bool(data.get("provider"))
    if status == 401 and not provider_identified:
        return BridgeError(ErrorKind.AUTH, "OmniRoute connection authentication failed", scope="connection",
                           status_code=status, retry_after=_retry_after(response))
    if status == 402 or (status == 429 and quota):
        kind = ErrorKind.EXHAUSTED
    elif status == 429:
        kind = ErrorKind.RATE_LIMITED
    elif status == 401:
        kind = ErrorKind.AUTH
    elif status == 403:
        kind = ErrorKind.PLAN
    elif status in {404, 410}:
        kind = ErrorKind.TARGET
    elif 400 <= status < 500:
        kind = ErrorKind.BAD_REQUEST
    else:
        kind = ErrorKind.TRANSIENT
    return BridgeError(kind, f"OmniRoute {kind.value} (HTTP {status})", status_code=status,
                       retry_after=_retry_after(response))


def _provider(actual: Any, expected: str) -> None:
    if not isinstance(actual, str) or actual != expected:
        raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute returned a different or unidentified provider")


def _strict_request(req: dict[str, Any], allowed: set[str], *, operation: str) -> None:
    # The gateway accepts filters that some handlers simply ignore. Rejecting
    # unused public arguments avoids claiming a filtered search or a fresh read.
    unsupported = sorted(key for key, value in req.items() if key not in allowed and value not in (None, [], {}))
    if unsupported:
        raise _bad(f"OmniRoute {operation} does not support: {', '.join(unsupported)}")
    if req.get("fresh"):
        raise _bad("OmniRoute cannot guarantee upstream cache bypass for fresh requests")


class OmniRouteBridge:
    """One transport for mapped search, source fetch and optional API reranking."""

    async def _post(self, ctx: CallContext, operation: str, payload: dict[str, Any]) -> tuple[Any, httpx.Response]:
        endpoint, headers = _endpoint(ctx, operation), _headers(ctx)
        try:
            response = await ctx.request("POST", endpoint, json=payload, headers=headers,
                                         retries=0, max_bytes=4 * 1024 * 1024)
        except ProviderError as exc:
            if exc.kind != ErrorKind.TRANSIENT:
                raise
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute connection unavailable", scope="connection") from exc
        except (httpx.RequestError, TimeoutError) as exc:
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute connection unavailable", scope="connection") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.is_error or (isinstance(data, dict) and data.get("success") is False):
            raise _failure(response, data)
        if not isinstance(data, dict):
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute returned an invalid JSON object")
        return data, response

    async def search(self, provider_id: str, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}:
            raise _bad("OmniRoute search does not implement this capability")
        _strict_request(req, {"query", "limit", "fresh", "deadline_s"}, operation="search")
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise _bad("Search query must be nonempty")
        limit = req.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise _bad("Search limit must be an integer from 1 to 25")
        body: dict[str, Any] = {"query": query.strip(), "provider": provider_id,
                                "max_results": limit, "search_type": "news" if cap == Capability.NEWS_SEARCH else "web",
                                "strict_filters": True}
        data, _ = await self._post(ctx, "search", body)
        _provider(data.get("provider"), provider_id)
        if data.get("errors") not in (None, []):
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute reported incomplete search coverage")
        rows = data.get("results")
        if not isinstance(rows, list):
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute search results are missing or invalid")
        hits: list[Hit] = []
        for position, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute returned an invalid search hit")
            citation = row.get("citation")
            if not isinstance(citation, dict):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute search hit lacks provider provenance")
            _provider(citation.get("provider"), provider_id)
            url = row.get("url")
            if not isinstance(url, str):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute search hit lacks a source URL")
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute search hit lacks a safe source URL")
            rank = row.get("position") or citation.get("rank") or position
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute search hit rank is invalid")
            published = row.get("published_at")
            hits.append(Hit(provider=provider_id, rank=rank, title=str(row.get("title") or ""), url=url,
                            snippet=str(row["snippet"]) if isinstance(row.get("snippet"), str) else None,
                            published=published if isinstance(published, str) else None,
                            raw={"transport": "omniroute", "citation": citation}))
        return Result(hits=hits[:limit], usage={"transport": "omniroute", "cached": data.get("cached") is True})

    async def fetch(self, provider_id: str, req: dict[str, Any], ctx: CallContext) -> Result:
        _strict_request(req, {"target", "url", "fresh", "deadline_s"}, operation="fetch")
        target = req.get("target", req.get("url"))
        if not isinstance(target, str) or not target:
            raise _bad("Fetch requires a source URL")
        await ctx.validate_url(target)
        data, _ = await self._post(ctx, "web/fetch", {"url": target, "provider": provider_id,
                                                      "format": "markdown", "include_metadata": True})
        # Installed 3.8.51 returns the executor's data directly, not a wrapper.
        _provider(data.get("provider"), provider_id)
        returned_url = data.get("url")
        if not isinstance(returned_url, str):
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute fetch result has no source URL")
        await ctx.validate_url(returned_url)
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            raise BridgeError(ErrorKind.TARGET, "OmniRoute returned no readable source text")
        return Result(document=Document(url=returned_url, text=content.strip(), source=provider_id, kind="page"),
                      raw={"transport": "omniroute", "provider": provider_id})

    async def rerank(self, model: str, query: str, documents: list[str], ctx: CallContext,
                     *, top_n: int | None = None) -> list[dict[str, Any]]:
        """Return validated rankings; caller keeps original order if this raises.

        Only a configured explicit ``provider/model`` is allowed. This helper is
        deliberately not wired into search: optional rerank integration is P5.
        """
        provider_id, separator, model_id = model.partition("/")
        if not separator or not provider_id or not model_id:
            raise _bad("Rerank model must be an explicit provider/model pair")
        if not isinstance(query, str) or not query.strip() or not documents or any(
            not isinstance(doc, str) or not doc.strip() for doc in documents
        ):
            raise _bad("Rerank needs a query and nonempty text documents")
        count = len(documents) if top_n is None else top_n
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= len(documents):
            raise _bad("Rerank top_n must be in the document range")
        data, response = await self._post(ctx, "rerank", {"model": model, "query": query,
                                                          "documents": documents, "top_n": count,
                                                          "return_documents": False})
        # Unlike search/fetch, rerank has no returned provider in the JSON body.
        # The installed gateway emits X-OmniRoute-Provider from the selected config.
        _provider(response.headers.get("x-omniroute-provider"), provider_id)
        returned_model = response.headers.get("x-omniroute-model")
        if returned_model is not None and returned_model != model_id:
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute rerank substituted the model")
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute rerank response lacks rankings")
        ranking: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute rerank item is invalid")
            index, score = row.get("index"), row.get("relevance_score")
            if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(documents)
                    or index in seen or isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score)):
                raise BridgeError(ErrorKind.TRANSIENT, "OmniRoute rerank index or score is invalid")
            seen.add(index)
            ranking.append({"index": index, "relevance_score": score})
        return ranking


class OmniRouteProvider(Provider):
    """One fixed upstream provider, never an OmniRoute automatic-selection route."""

    bridge_connection = "omniroute"
    keyless = True  # virtual routing marker; service secret lives ONLY on the connection row

    def __init__(self, name: str, provider_id: str, capabilities: set[Capability] | frozenset[Capability],
                 bridge: OmniRouteBridge | None = None):
        if name != f"omni:{provider_id}" or not provider_id or not capabilities:
            raise ValueError("OmniRoute virtual provider name must match its upstream ID and capabilities")
        self.name, self.provider_id = name, provider_id
        self.capabilities = frozenset(capabilities)
        self.bridge = bridge or OmniRouteBridge()

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in self.capabilities:
            raise _bad("Unsupported OmniRoute virtual provider capability")
        if cap == Capability.WEB_READ:
            return await self.bridge.fetch(self.provider_id, req, ctx)
        return await self.bridge.search(self.provider_id, cap, req, ctx)
