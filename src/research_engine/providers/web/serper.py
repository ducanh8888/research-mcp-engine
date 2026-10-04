"""Serper Google search/news using raw HTTP so exhausted credits remain visible."""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import api_key, checked_json, domain_query, hits, limit, options, query


class SerperProvider(Provider):
    name = "serper"
    capabilities = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in self.capabilities:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Serper capability", block_capability=False)
        opts = options(ctx, req)
        count = limit(req, 100)
        body: dict[str, Any] = {"q": domain_query(query(req), req, opts), "num": count}
        language = req.get("language", opts.get("language", opts.get("hl")))
        country = req.get("country", opts.get("country", opts.get("gl")))
        if language:
            body["hl"] = str(language).strip().replace("_", "-").split("-")[0].lower()
        if country:
            body["gl"] = str(country).lower()
        freshness = req.get("freshness", opts.get("tbs"))
        if freshness:
            body["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y",
                           "pd": "qdr:d", "pw": "qdr:w", "pm": "qdr:m", "py": "qdr:y"}.get(
                freshness, freshness)
        if req.get("page") is not None:
            try:
                page = int(req["page"])
            except (ValueError, TypeError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Serper page must be an integer",
                                    block_capability=False) from exc
            if page < 1:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Serper page must be positive",
                                    block_capability=False)
            body["page"] = page
        path = "/news" if cap == Capability.NEWS_SEARCH else "/search"
        base = str(ctx.options.get("base_url", "https://google.serper.dev")).rstrip("/")
        data = checked_json(await ctx.request("POST", base + path, json=body,
                            headers={"X-API-KEY": api_key(ctx), "Content-Type": "application/json"}), ctx)
        if "searchParameters" not in data and not any(key in data for key in ("organic", "news")):
            raise ProviderError(ErrorKind.TRANSIENT, "Serper returned an unrecognized search response")
        rows = data.get("news" if cap == Capability.NEWS_SEARCH else "organic", [])
        usage = {"credits": data["credits"]} if "credits" in data else {}
        return Result(hits=hits(rows, ctx, maximum=count), usage=usage)
