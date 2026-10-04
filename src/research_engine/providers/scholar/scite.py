"""Scite's public REST surface: metadata and tallies, never Smart Citation passages."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from research_engine.providers.base import Capability, ErrorKind, Hit, Provider, ProviderError, Result
from .common import bibliographic, doi, input_ids, json_request, metadata_result, strip_tags

BASE = "https://api.scite.ai"


def normalize(work: dict[str, Any]) -> Hit:
    identifier = doi(work.get("doi"))
    authors = []
    for author in work.get("authors", []):
        if isinstance(author, str):
            authors.append(author)
        elif isinstance(author, dict):
            authors.append(author.get("name") or " ".join(str(part) for part in
                (author.get("given") or author.get("firstName"), author.get("family") or author.get("lastName")) if part))
    return Hit(provider="scite_rest", id="doi:" + (identifier or ""),
        title=strip_tags(work.get("title")) or "", url="https://doi.org/" + (identifier or ""),
        ids={"doi": identifier} if identifier else {}, authors=authors, year=work.get("year"),
        venue=work.get("journal") or work.get("shortJournal"), snippet=strip_tags(work.get("abstract")),
        raw={key: work[key] for key in ("editorialNotices", "retracted", "preprintLinks", "publicationLinks") if key in work})


class SciteRestProvider(Provider):
    name = "scite_rest"
    keyless = True
    capabilities = frozenset({Capability.PAPER_METADATA, Capability.CITATION_VERIFY, Capability.EDITORIAL_CHECK})
    options = {"rate_limit_rps": 1, "concurrency": 1}

    async def fetch(self, target: str, ctx: Any, endpoint: str = "papers") -> dict[str, Any] | None:
        identifier = doi(target)
        if not identifier:
            return None
        return await json_request(ctx, "GET", BASE + "/" + endpoint + "/" + quote(identifier, safe="/"),
                                  not_found=True)

    async def call(self, cap: Capability, req: dict[str, Any], ctx: Any) -> Result:
        if cap == Capability.PAPER_METADATA:
            return metadata_result([(target, normalize(work) if (work := await self.fetch(target, ctx)) else None)
                                    for target in input_ids(req)], self.name)
        if cap == Capability.CITATION_VERIFY:
            citation = req.get("citation", "")
            target = (citation.get("doi") or citation.get("id", "")) if isinstance(citation, dict) else citation
            work = await self.fetch(target, ctx)
            result = bibliographic(req, normalize(work) if work else None, self.name)
            tally = await self.fetch(target, ctx, "tallies") if doi(target) else None
            if tally:
                result.citation_tallies.append({"provider": self.name, "doi": doi(target),
                    "source": BASE + "/tallies/" + quote(doi(target), safe="/"),
                    "scope": "all_citations_to_paper", **{key: tally[key] for key in
                        ("total", "supporting", "contradicting", "mentioning", "unclassified", "citingPublications") if key in tally}})
            return result
        if cap == Capability.EDITORIAL_CHECK:
            checks = []
            for target in input_ids(req):
                work = await self.fetch(target, ctx)
                notices = []
                for notice in work.get("editorialNotices", []) if work else []:
                    notices.append({"type": notice.get("type"), "notice_doi": doi(notice.get("noticeDoi")),
                        "date": notice.get("date"), "provider": self.name, "raw": notice})
                types = " ".join(str(notice["type"]).lower() for notice in notices)
                status = ("retracted" if work and work.get("retracted") or "retract" in types else
                          "expression_of_concern" if "concern" in types else "correction" if
                          "correction" in types or "erratum" in types else "unknown")
                checks.append({"requested_id": target, "id": target, "provider": self.name,
                    "status": status, "notices": notices,
                    "source": BASE + "/papers/" + quote(doi(target) or target, safe="/"),
                    "coverage": "editorial_notices" if work else "not_found"})
            return Result(checks=checks)
        raise ProviderError(ErrorKind.PLAN, "Unsupported Scite public REST capability")
