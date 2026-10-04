"""Tavily search and extraction, retaining credit errors and source content only."""

from __future__ import annotations

from typing import Any

import httpx

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import api_key, checked_json, document, error_text, hits, limit, options, query, retry_after


_COUNTRIES = {"US": "united states", "GB": "united kingdom", "UK": "united kingdom", "VN": "vietnam",
              "TH": "thailand", "AU": "australia", "CA": "canada", "NZ": "new zealand", "DE": "germany",
              "FR": "france", "ES": "spain", "IT": "italy", "JP": "japan", "CN": "china",
              "TW": "taiwan", "KR": "south korea", "IN": "india", "SG": "singapore", "ID": "indonesia",
              "MY": "malaysia", "PH": "philippines", "BR": "brazil", "MX": "mexico", "NL": "netherlands"}


class TavilyProvider(Provider):
    name = "tavily"
    capabilities = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH, Capability.WEB_READ})

    @staticmethod
    def _checked(response: httpx.Response, ctx: CallContext) -> dict[str, Any]:
        if response.status_code == 429:
            try:
                text = error_text(response.json()).lower()
            except ValueError:
                text = ""
            if any(value in text for value in ("usage limit", "monthly limit", "credit limit", "credit balance")):
                raise ProviderError(ErrorKind.EXHAUSTED, "Tavily usage budget exhausted",
                                    retry_after=retry_after(response), status_code=429)
        return checked_json(response, ctx, exhausted_statuses=(402, 432, 433))

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in self.capabilities:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Tavily capability", block_capability=False)
        opts = options(ctx, req)
        base = str(ctx.options.get("base_url", "https://api.tavily.com")).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key(ctx)}", "Content-Type": "application/json"}
        if cap == Capability.WEB_READ:
            url = str(req.get("url", ""))
            await ctx.validate_url(url)
            depth = opts.get("extract_depth", "basic")
            if depth not in {"basic", "advanced"}:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid Tavily extraction depth",
                                    block_capability=False)
            body = {"urls": [url], "extract_depth": depth, "format": "markdown", "include_usage": True}
            data = self._checked(await ctx.request("POST", base + "/extract", json=body, headers=headers), ctx)
            rows = data.get("results", [])
            row = next((item for item in rows if isinstance(item, dict) and item.get("raw_content")), None)
            if row is None:
                raise ProviderError(ErrorKind.TARGET, "Tavily could not extract this target",
                                    block_capability=False)
            final_url = str(row.get("url") or url)
            if final_url != url:
                await ctx.validate_url(final_url)
            result = document(row["raw_content"], final_url, ctx)
            result.usage = data.get("usage") or {}
            result.raw = {"failed_results": data.get("failed_results", [])}
            return result
        count = limit(req, 20)
        depth = opts.get("search_depth", "basic")
        if depth not in {"basic", "advanced", "fast", "ultra-fast"}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid Tavily search depth", block_capability=False)
        body: dict[str, Any] = {"query": query(req), "max_results": count, "search_depth": depth,
                               "topic": "news" if cap == Capability.NEWS_SEARCH else "general",
                               "include_answer": False, "include_usage": True}
        for local, remote in (("include_domains", "include_domains"), ("exclude_domains", "exclude_domains"),
                              ("country", "country"), ("published_after", "start_date"),
                              ("published_before", "end_date")):
            value = req.get(local, opts.get(remote))
            if value:
                if local == "country" and cap == Capability.NEWS_SEARCH:
                    raise ProviderError(ErrorKind.BAD_REQUEST, "Tavily country applies to general search only",
                                        block_capability=False)
                if local == "country":
                    value = str(value).strip()
                    if len(value) == 2:
                        if value.upper() not in _COUNTRIES:
                            raise ProviderError(ErrorKind.BAD_REQUEST,
                                                "Use a country name for this Tavily country code",
                                                block_capability=False)
                        value = _COUNTRIES[value.upper()]
                    else:
                        value = value.lower()
                body[remote] = value
        language = req.get("language", opts.get("language"))
        if language:
            body["language"] = language
        if opts.get("filter_by_language"):
            if not language:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Tavily language filter requires language",
                                    block_capability=False)
            body["filter_by_language"] = True
        freshness = req.get("freshness", opts.get("time_range"))
        if freshness:
            body["time_range"] = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}.get(
                freshness, freshness)
        data = self._checked(await ctx.request("POST", base + "/search", json=body, headers=headers), ctx)
        return Result(hits=hits(data.get("results"), ctx, maximum=count), usage=data.get("usage") or {},
                      raw={"request_id": data.get("request_id")})
