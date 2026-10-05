"""Consensus REST search, using its current official GET /v1/search contract.

Reference: Consensus-NLP/consensus-api README at
840b2058d2d783a6c724af3ab27bfa0de52a7329. Older research notes describe POST;
the current API documents GET. Generated takeaways are retained only as raw data.
"""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Hit, Provider, ProviderError, Result
from research_engine.providers.scholar.common import doi, json_request, limit, strip_tags, year_range


def api_key(ctx: CallContext) -> str:
    key = ctx.credentials.get("api_key") or ctx.credentials.get("key") or ctx.credentials.get("token")
    if not isinstance(key, str) or not key.strip():
        raise ProviderError(ErrorKind.AUTH, f"{ctx.provider} requires an API key")
    return key.strip()


def paper_hit(paper: dict[str, Any], ctx: CallContext, rank: int) -> Hit:
    ids = {}
    if identifier := doi(paper.get("doi")):
        ids["doi"] = identifier
    if paper.get("paper_id") or paper.get("id"):
        ids["consensus"] = str(paper.get("paper_id") or paper["id"])
    authors = paper.get("authors") or []
    authors = [str(author.get("name", "")) if isinstance(author, dict) else str(author) for author in authors]
    year = paper.get("publish_year") or paper.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None
    return Hit(provider=ctx.provider, account=ctx.account_id, rank=rank,
               id=ids.get("doi") or ids.get("consensus") or paper.get("url") or "",
               ids=ids, title=strip_tags(paper.get("title")) or "",
               url=paper.get("url") or (f"https://doi.org/{ids['doi']}" if "doi" in ids else ""),
               snippet=strip_tags(paper.get("abstract")), authors=[author for author in authors if author],
               year=year, venue=paper.get("journal_name") or paper.get("venue"),
               raw={**paper, "evidence_kind": "paper_metadata", "snippet_kind": "abstract"})


class ConsensusAPIProvider(Provider):
    name = "consensus_api"
    capabilities = frozenset({Capability.PAPER_SEARCH})
    endpoint = "https://api.consensus.app/v1/search"

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap not in self.capabilities:
            raise ProviderError(ErrorKind.PLAN, "Consensus REST implements paper_search only")
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ProviderError(ErrorKind.BAD_REQUEST, "Consensus requires a nonempty query")
        params: dict[str, Any] = {"query": query.strip(), "page_size": limit(req)}
        if req.get("cursor") is not None:
            try:
                page = int(req["cursor"])
            except (ValueError, TypeError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Consensus cursor must be a page number") from exc
            if page < 0:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Consensus page must be nonnegative")
            params["page"] = page
        filters = req.get("filters") or {}
        if not isinstance(filters, dict):
            raise ProviderError(ErrorKind.BAD_REQUEST, "Consensus filters must be an object")
        start, end = year_range(req)
        if (start is not None and "year_min" in filters) or (end is not None and "year_max" in filters):
            raise ProviderError(ErrorKind.BAD_REQUEST, "Use either public year bounds or Consensus year filters")
        supported = {"year_min", "year_max", "month_min", "month_max", "study_types", "human", "controlled",
                     "sample_size_min", "exclude_preprints", "sjr_min", "sjr_max", "citation_min", "medical_mode",
                     "clinical_guideline", "domain", "country", "journal_name", "publisher_name", "open_access"}
        for key in supported & filters.keys():
            value = filters[key]
            params[key] = ",".join(map(str, value)) if isinstance(value, list) else value
        if start is not None:
            params["year_min"] = start
        if end is not None:
            params["year_max"] = end
        data = await json_request(ctx, "GET", self.endpoint, headers={"x-api-key": api_key(ctx)}, params=params)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ProviderError(ErrorKind.TRANSIENT, "Consensus response omitted results")
        papers = data["results"][:limit(req)]
        if any(not isinstance(paper, dict) for paper in papers):
            raise ProviderError(ErrorKind.TRANSIENT, "Consensus returned invalid paper records")
        hits = [paper_hit(paper, ctx, rank) for rank, paper in enumerate(papers, 1)]
        next_page = data.get("next_page")
        return Result(hits=hits, cursor=str(next_page) if next_page is not None and not data.get("is_end") else None,
                      raw={"page": data.get("page"), "is_end": data.get("is_end")})


ConsensusProvider = ConsensusAPIProvider
