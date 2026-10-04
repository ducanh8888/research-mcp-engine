"""The arXiv Atom API; published-paper relations are not identifier aliases."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from research_engine.providers.base import Capability, ErrorKind, Hit, Provider, ProviderError, Result
from .common import (RequestSpacing, abstract_document, arxiv_id, bibliographic, input_ids,
                     limit, metadata_result, pdf_document)

BASE = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
SPACING = RequestSpacing(3.0)


def normalize(entry: ET.Element, rank: int = 1) -> Hit:
    url = entry.findtext(ATOM + "id", "")
    identifier = arxiv_id(url) or ""
    abstract = " ".join(entry.findtext(ATOM + "summary", "").split())
    published = entry.findtext(ATOM + "published", "")
    pdf = next((link.get("href") for link in entry.findall(ATOM + "link")
                if link.get("title") == "pdf" or link.get("type") == "application/pdf"), None)
    publisher_doi = entry.findtext(ARXIV + "doi")
    raw = {"categories": [category.get("term") for category in entry.findall(ATOM + "category")],
           "updated": entry.findtext(ATOM + "updated")}
    if pdf:
        raw["pdf_url"] = pdf.replace("http://arxiv.org/", "https://arxiv.org/")
    if publisher_doi:
        raw["publication_doi"] = publisher_doi
        raw["relations"] = [{"type": "is_preprint_of", "doi": publisher_doi}]
    return Hit(provider="arxiv", id="arxiv:" + identifier, rank=rank,
        title=" ".join(entry.findtext(ATOM + "title", "").split()),
        url=url.replace("http://arxiv.org/", "https://arxiv.org/"),
        ids={"arxiv": identifier} if identifier else {}, snippet=abstract or None,
        authors=[author.findtext(ATOM + "name", "") for author in entry.findall(ATOM + "author")],
        year=int(published[:4]) if published[:4].isdigit() else None, published=published or None,
        venue=entry.findtext(ARXIV + "journal_ref"), raw=raw)


class ArxivProvider(Provider):
    name = "arxiv"
    keyless = True
    capabilities = frozenset({Capability.PAPER_SEARCH, Capability.PAPER_METADATA, Capability.PAPER_READ,
                             Capability.CITATION_VERIFY})
    options = {"rate_limit_rps": 0.333333, "concurrency": 1}

    async def query(self, params: dict[str, Any], ctx: Any) -> list[Hit]:
        headers = {"User-Agent": "research-engine/0.1"}
        if ctx.options.get("mailto"):
            headers["User-Agent"] += " (mailto:" + str(ctx.options["mailto"]) + ")"
        response = await SPACING.request(ctx, "GET", BASE, params=params, headers=headers)
        if response.status_code >= 400:
            kind = ErrorKind.RATE_LIMITED if response.status_code == 429 else ErrorKind.TRANSIENT
            raise ProviderError(kind, f"arXiv returned HTTP {response.status_code}", status_code=response.status_code)
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "arXiv returned malformed Atom XML") from exc
        records = []
        for entry in root.findall(ATOM + "entry"):
            if "/api/errors" in entry.findtext(ATOM + "id", ""):
                raise ProviderError(ErrorKind.BAD_REQUEST, "arXiv rejected the query")
            hit = normalize(entry, len(records) + 1)
            if hit.ids:
                records.append(hit)
        return records

    async def fetch_many(self, ids: list[str], ctx: Any) -> list[tuple[str, Hit | None]]:
        requested = [(target, arxiv_id(target)) for target in ids]
        valid = [identifier for _, identifier in requested if identifier]
        records = await self.query({"id_list": ",".join(valid), "max_results": len(valid)}, ctx) if valid else []
        out = []
        for target, identifier in requested:
            hit = next((record for record in records if identifier and
                (record.ids["arxiv"] == identifier or "v" not in identifier and
                 record.ids["arxiv"].split("v", 1)[0] == identifier)), None)
            out.append((target, hit))
        return out

    async def search(self, query: str, req: dict[str, Any], ctx: Any) -> Result:
        search = query if ":" in query else "all:" + query
        filters = req.get("filters") or {}
        if isinstance(filters, dict) and filters.get("category"):
            search += " AND cat:" + str(filters["category"])
        return Result(hits=await self.query({"search_query": search, "start": 0,
                      "max_results": limit(req), "sortBy": "relevance", "sortOrder": "descending"}, ctx))

    async def call(self, cap: Capability, req: dict[str, Any], ctx: Any) -> Result:
        if cap == Capability.PAPER_SEARCH:
            return await self.search(req["query"], req, ctx)
        ids = input_ids(req)
        if cap == Capability.PAPER_METADATA:
            return metadata_result(await self.fetch_many(ids, ctx), self.name)
        target = ids[0] if ids else ""
        found = await self.fetch_many([target], ctx)
        hit = found[0][1]
        if cap == Capability.CITATION_VERIFY:
            if not hit and not arxiv_id(target):
                search = await self.search(str(target), {"limit": 1}, ctx)
                hit = search.hits[0] if search.hits else None
            return bibliographic(req, hit, self.name)
        if cap == Capability.PAPER_READ:
            if not hit:
                raise ProviderError(ErrorKind.TARGET, "Paper not found in arXiv")
            if hit.raw.get("pdf_url"):
                try:
                    return await pdf_document(ctx, hit.raw["pdf_url"], hit, self.name)
                except ProviderError as exc:
                    if exc.kind not in {ErrorKind.TARGET, ErrorKind.BAD_REQUEST}:
                        raise
            return abstract_document(hit, self.name)
        raise ProviderError(ErrorKind.PLAN, "Unsupported arXiv capability")
