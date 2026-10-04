"""Exa HTTP search and source contents, adapted from research-mcp @11f297d."""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import api_key, checked_json, document, hits, limit, options, query


class ExaProvider(Provider):
    name = "exa"
    capabilities = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH,
                              Capability.DEVELOPER_SEARCH, Capability.WEB_READ})
    options = {"search_type": "auto"}

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        opts = options(ctx, req)
        base = str(ctx.options.get("base_url", "https://api.exa.ai")).rstrip("/")
        headers = {"x-api-key": api_key(ctx), "Content-Type": "application/json"}
        if cap == Capability.WEB_READ:
            url = str(req.get("url", ""))
            await ctx.validate_url(url)
            age = opts.get("max_age_hours", opts.get("maxAgeHours", 24))
            try:
                age = float(age)
            except (ValueError, TypeError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Exa maxAgeHours must be numeric",
                                    block_capability=False) from exc
            if not -1 <= age <= 720:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Exa maxAgeHours must be between -1 and 720",
                                    block_capability=False)
            body = {"urls": [url], "text": True, "maxAgeHours": age}
            data = checked_json(await ctx.request("POST", base + "/contents", json=body, headers=headers), ctx)
            rows = data.get("results", [])
            row = next((item for item in rows if isinstance(item, dict) and item.get("text")), None)
            if row is None:
                raise ProviderError(ErrorKind.TARGET, "Exa could not retrieve this target",
                                    block_capability=False)
            final_url = str(row.get("url") or url)
            if final_url != url:
                await ctx.validate_url(final_url)
            result = document(row["text"], final_url, ctx)
            result.usage = data.get("costDollars") or {}
            result.raw = {"requestId": data.get("requestId"), "statuses": data.get("statuses", [])}
            return result
        if cap not in self.capabilities:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Exa capability", block_capability=False)
        search_type = opts.get("type", opts.get("search_type", "auto"))
        if search_type not in {"instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning"}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Exa search type", block_capability=False)
        count = limit(req, 100)
        body: dict[str, Any] = {
            "query": query(req), "type": search_type, "numResults": count,
            "contents": {"highlights": True},
        }
        category = "news" if cap == Capability.NEWS_SEARCH else opts.get("category")
        if category in {"github", "pdf"}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Exa removed this category; use include_domains",
                                block_capability=False)
        if category:
            body["category"] = category
        include = req.get("include_domains", opts.get("includeDomains", opts.get("include_domains")))
        if cap == Capability.DEVELOPER_SEARCH and not include:
            include = ["github.com", "stackoverflow.com", "readthedocs.io", "developer.mozilla.org"]
        if include:
            body["includeDomains"] = include
        exclude = req.get("exclude_domains", opts.get("excludeDomains", opts.get("exclude_domains")))
        if exclude:
            body["excludeDomains"] = exclude
        for local, remote in (("published_after", "startPublishedDate"), ("published_before", "endPublishedDate"),
                              ("crawl_after", "startCrawlDate"), ("crawl_before", "endCrawlDate")):
            value = req.get(local, opts.get(remote))
            if value:
                body[remote] = value
        if req.get("country") or opts.get("userLocation"):
            body["userLocation"] = req.get("country") or opts["userLocation"]
        data = checked_json(await ctx.request("POST", base + "/search", json=body, headers=headers), ctx)
        return Result(hits=hits(data.get("results"), ctx, maximum=count),
                      usage=data.get("costDollars") or {}, raw={"requestId": data.get("requestId")})
