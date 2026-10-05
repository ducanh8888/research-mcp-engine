"""Crossref deposited bibliographic metadata and explicit editorial notices."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from research_engine.providers.base import Capability, ErrorKind, Hit, Provider, ProviderError, Result
from .common import bibliographic, citation_result, doi, input_ids, json_request, limit, metadata_result, strip_tags, year_range

BASE = "https://api.crossref.org"


def normalize(work: dict[str, Any], rank: int = 1) -> Hit:
    identifier = doi(work.get("DOI"))
    title = work.get("title") or []
    venue = work.get("container-title") or []
    date = next((work.get(key, {}).get("date-parts", [[]])[0] for key in
                 ("published", "published-print", "published-online", "issued")
                 if work.get(key, {}).get("date-parts")), [])
    return Hit(provider="crossref", id="doi:" + (identifier or ""), rank=rank,
        title=strip_tags(title[0] if isinstance(title, list) and title else title) or "",
        url=work.get("URL") or "https://doi.org/" + (identifier or ""),
        ids={"doi": identifier} if identifier else {}, snippet=strip_tags(work.get("abstract")),
        authors=[" ".join(part for part in (author.get("given"), author.get("family")) if part)
                 or author.get("name", "") for author in work.get("author", [])],
        year=date[0] if date else None, venue=venue[0] if venue else None,
        raw={key: work[key] for key in ("type", "publisher", "volume", "issue", "page", "relation",
             "updated-by", "update-to", "link", "license", "reference", "is-referenced-by-count") if key in work})


class CrossrefProvider(Provider):
    name = "crossref"
    keyless = True
    capabilities = frozenset({Capability.PAPER_SEARCH, Capability.PAPER_METADATA,
                              Capability.CITATION_VERIFY, Capability.EDITORIAL_CHECK})
    options = {"rate_limit_rps": 1, "concurrency": 1}

    def options_for(self, ctx: Any) -> dict[str, Any]:
        email = ctx.options.get("mailto") or ctx.credentials.get("mailto")
        params = {"mailto": email} if email else {}
        headers = {"User-Agent": "research-engine/0.1" + (" (mailto:" + email + ")" if email else "")}
        key = ctx.credentials.get("api_key") or ctx.credentials.get("key")
        if key:
            headers["Crossref-Plus-API-Token"] = "Bearer " + key
        return {"params": params, "headers": headers}

    async def fetch(self, target: str, ctx: Any) -> dict[str, Any] | None:
        identifier = doi(target)
        if not identifier:
            return None
        data = await json_request(ctx, "GET", BASE + "/works/" + quote(identifier, safe="/"),
                                  not_found=True, **self.options_for(ctx))
        return data.get("message") if data else None

    async def search(self, query: str, req: dict[str, Any], ctx: Any) -> Result:
        options = self.options_for(ctx)
        options["params"].update({"query.bibliographic": query, "rows": limit(req)})
        start, end = year_range(req)
        filters = req.get("filters") or {}
        if not isinstance(filters, dict):
            raise ProviderError(ErrorKind.BAD_REQUEST, "Crossref filters must be an object")
        allowed = {"from-pub-date", "until-pub-date", "type", "has-abstract", "has-full-text"}
        values = [key + ":" + str(value).lower() for key, value in filters.items() if key in allowed]
        if (start is not None and "from-pub-date" in filters or
                end is not None and "until-pub-date" in filters):
            raise ProviderError(ErrorKind.BAD_REQUEST, "Use either public year bounds or Crossref date filters")
        if start is not None:
            values.append(f"from-pub-date:{start}-01-01")
        if end is not None:
            values.append(f"until-pub-date:{end}-12-31")
        if values:
            options["params"]["filter"] = ",".join(values)
        data = await json_request(ctx, "GET", BASE + "/works", **options)
        message = data.get("message") or {}
        return Result(hits=[normalize(work, i) for i, work in enumerate(message.get("items", []), 1)],
                      raw={"total": message.get("total-results")})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: Any) -> Result:
        if cap == Capability.PAPER_SEARCH:
            return await self.search(req["query"], req, ctx)
        if cap == Capability.PAPER_METADATA:
            if req.get("citation") and not req.get("ids"):
                citation = str(req["citation"])
                if doi(citation):
                    work = await self.fetch(citation, ctx)
                    return metadata_result([(citation, normalize(work) if work else None)], self.name)
                results = await self.search(citation, {"limit": 5}, ctx)
                return citation_result(citation, results.hits, self.name)
            return metadata_result([(target, normalize(work) if (work := await self.fetch(target, ctx)) else None)
                                    for target in input_ids(req)], self.name)
        if cap == Capability.CITATION_VERIFY:
            citation = req.get("citation", "")
            target = (citation.get("doi") or citation.get("id") or citation.get("title", "")) if isinstance(citation, dict) else citation
            work = await self.fetch(target, ctx)
            if work:
                return bibliographic(req, normalize(work), self.name)
            # A DOI miss is not substituted by a different work returned by text search.
            if doi(target):
                return bibliographic(req, None, self.name)
            results = await self.search(str(target), {"limit": 1}, ctx)
            return bibliographic(req, results.hits[0] if results.hits else None, self.name)
        if cap == Capability.EDITORIAL_CHECK:
            checks = []
            for target in input_ids(req):
                work = await self.fetch(target, ctx)
                notices = []
                for update in work.get("updated-by", []) if work else []:
                    notices.append({"type": update.get("type"), "notice_doi": doi(update.get("DOI")),
                        "date": update.get("updated"), "source": update.get("source"), "provider": self.name})
                types = {str(notice["type"]).lower().replace("-", "_") for notice in notices}
                status = ("retracted" if "retraction" in types else "expression_of_concern" if
                          "expression_of_concern" in types else "correction" if "correction" in types else "unknown")
                checks.append({"requested_id": target, "id": target, "provider": self.name,
                    "status": status, "notices": notices,
                    "source": "https://api.crossref.org/works/" + quote(doi(target) or target, safe="/"),
                    "coverage": "deposited_notices" if work else "not_found"})
            return Result(checks=checks)
        raise ProviderError(ErrorKind.PLAN, "Unsupported Crossref capability")
