"""Brave web/news HTTP search; request mapping adapted from research-mcp @11f297d."""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import api_key, checked_json, domain_query, error_text, hits, limit, options, query, retry_after


def _language(value: str) -> str:
    normalized = value.strip().replace("_", "-").lower()
    if normalized in {"zh-tw", "zh-hk", "zh-hant"}:
        return "zh-hant"
    if normalized.startswith("zh"):
        return "zh-hans"
    if normalized == "pt-br":
        return "pt-br"
    return {"no": "nb"}.get(normalized.split("-")[0], normalized.split("-")[0])


class BraveProvider(Provider):
    name = "brave"
    capabilities = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in self.capabilities:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Brave capability", block_capability=False)
        opts = options(ctx, req)
        count = limit(req, 20)
        params: dict[str, Any] = {"q": domain_query(query(req), req, opts), "count": count}
        language = req.get("language", opts.get("language", opts.get("search_lang")))
        if language:
            params["search_lang"] = _language(str(language))
        if req.get("country", opts.get("country")):
            params["country"] = str(req.get("country", opts.get("country"))).upper()
        freshness = req.get("freshness", opts.get("freshness"))
        if freshness:
            params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}.get(
                freshness, freshness)
        safe = req.get("safe_search", opts.get("safesearch", "moderate"))
        if isinstance(safe, bool):
            safe = "strict" if safe else "off"
        if safe not in {"strict", "moderate", "off"}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid Brave safe search mode", block_capability=False)
        params["safesearch"] = safe
        try:
            offset = int(req.get("page", opts.get("page", 1))) - 1
        except (ValueError, TypeError) as exc:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Brave page must be an integer",
                                block_capability=False) from exc
        if not 0 <= offset <= 9:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Brave supports pages 1 through 10",
                                block_capability=False)
        if offset:
            params["offset"] = offset
        source = "news" if cap == Capability.NEWS_SEARCH else "web"
        base = str(ctx.options.get("base_url", "https://api.search.brave.com/res/v1")).rstrip("/")
        response = await ctx.request("GET", base + f"/{source}/search", params=params,
                                     headers={"X-Subscription-Token": api_key(ctx), "Accept": "application/json"})
        if response.status_code == 429:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            text = error_text(payload).lower()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            meta = error.get("meta", {}) if isinstance(error, dict) else {}
            current, allowed = meta.get("quota_current"), meta.get("quota_limit")
            exhausted = (isinstance(current, (int, float)) and isinstance(allowed, (int, float))
                         and allowed > 0 and current >= allowed)
            if exhausted or any(value in text for value in ("subscription_quota", "subscription_exhausted",
                                                            "monthly quota", "monthly limit")):
                raise ProviderError(ErrorKind.EXHAUSTED, "Brave subscription quota exhausted",
                                    retry_after=retry_after(response), status_code=429)
        data = checked_json(response, ctx)
        if source not in data and "results" not in data and not isinstance(data.get("query"), dict):
            raise ProviderError(ErrorKind.TRANSIENT, "Brave returned an unrecognized search response")
        payload = data.get(source, data if source == "news" else {})
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        return Result(hits=hits(rows, ctx, maximum=count), usage={"rate_limit_remaining":
                      response.headers.get("X-RateLimit-Remaining"), "rate_limit_reset":
                      response.headers.get("X-RateLimit-Reset")})
