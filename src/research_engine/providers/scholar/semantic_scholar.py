"""Semantic Scholar Graph and Recommendations APIs with attributable contexts."""

from __future__ import annotations

import re
from collections import deque
from typing import Any
from urllib.parse import quote

from research_engine.providers.base import Capability, ErrorKind, Hit, Provider, ProviderError, Result
from .common import (abstract_document, arxiv_id, bibliographic, doi, graph_edge, graph_node,
                     input_ids, json_request, limit, metadata_result, pdf_document)

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "paperId,title,externalIds,abstract,authors,year,venue,url,openAccessPdf,citationCount,referenceCount"
GRAPH_FIELDS = "paperId,title,externalIds,year,contexts,intents,isInfluential"


def target_id(target: str) -> str | None:
    identifier = doi(target)
    if identifier:
        return "DOI:" + identifier
    arxiv = arxiv_id(target)
    if arxiv:
        # S2 identifies the preprint family; it cannot confirm a requested arXiv version.
        return None if re.search(r"v\d+$", arxiv) else "ARXIV:" + arxiv
    target = target.removeprefix("s2:").removeprefix("semantic_scholar:")
    if re.fullmatch(r"[a-fA-F0-9]{40}", target):
        return target
    if target.lower().startswith(("corpusid:", "pmid:", "pmcid:", "url:")):
        return target
    if "semanticscholar.org/paper/" in target:
        suffix = target.rstrip("/").rsplit("/", 1)[-1]
        return suffix if re.fullmatch(r"[a-fA-F0-9]{40}", suffix) else None
    return None


def normalize(work: dict[str, Any], rank: int = 1) -> Hit:
    identifier = work.get("paperId") or ""
    ids = {"s2": identifier} if identifier else {}
    external = work.get("externalIds") or {}
    for name, value in external.items():
        if value is not None:
            key = {"DOI": "doi", "ArXiv": "arxiv", "PubMed": "pmid", "PubMedCentral": "pmcid",
                   "CorpusId": "s2_corpus"}.get(name, name.lower())
            ids[key] = doi(str(value)) if key == "doi" else str(value)
    return Hit(provider="semantic_scholar", id="s2:" + identifier, rank=rank,
        title=work.get("title") or "", url=work.get("url") or "https://www.semanticscholar.org/paper/" + identifier,
        ids=ids, snippet=work.get("abstract"), authors=[author.get("name", "") for author in work.get("authors", [])],
        year=work.get("year"), venue=work.get("venue"), raw={key: work[key] for key in
            ("openAccessPdf", "citationCount", "referenceCount", "isOpenAccess") if key in work})


class SemanticScholarProvider(Provider):
    name = "semantic_scholar"
    keyless = True
    capabilities = frozenset({Capability.PAPER_SEARCH, Capability.PAPER_METADATA, Capability.PAPER_READ,
                             Capability.PAPER_RELATED, Capability.CITATION_GRAPH, Capability.CITATION_VERIFY})
    options = {"rate_limit_rps": 1, "concurrency": 1}

    def headers(self, ctx: Any) -> dict[str, str]:
        key = ctx.credentials.get("api_key") or ctx.credentials.get("key")
        return {"x-api-key": key} if key else {}

    async def fetch(self, target: str, ctx: Any) -> dict[str, Any] | None:
        identifier = target_id(target)
        if not identifier:
            return None
        return await json_request(ctx, "GET", BASE + "/paper/" + quote(identifier, safe=":/"),
            headers=self.headers(ctx), params={"fields": FIELDS}, not_found=True)

    async def search(self, query: str, req: dict[str, Any], ctx: Any) -> Result:
        params = {"query": query, "limit": limit(req), "fields": FIELDS}
        filters = req.get("filters") or {}
        if isinstance(filters, dict):
            params.update({key: value for key, value in filters.items() if key in
                           {"year", "venue", "fieldsOfStudy", "publicationTypes", "openAccessPdf", "minCitationCount"}})
        data = await json_request(ctx, "GET", BASE + "/paper/search", headers=self.headers(ctx), params=params)
        return Result(hits=[normalize(work, i) for i, work in enumerate(data.get("data", []), 1)],
                      raw={"total": data.get("total")})

    async def neighbors(self, target: str, side: str, count: int, ctx: Any) -> dict[str, Any]:
        return await json_request(ctx, "GET", BASE + "/paper/" + quote(target, safe=":/") + "/" + side,
            headers=self.headers(ctx), params={"fields": GRAPH_FIELDS, "limit": min(100, count)}, not_found=True) or {}

    async def graph(self, req: dict[str, Any], ctx: Any) -> Result:
        budget = max(1, min(int(req.get("max_edges", 100)), 2000))
        depth = max(1, min(int(req.get("depth", 1)), 2))
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        queue = deque((seed, 0) for seed in input_ids(req))
        seen: set[str] = set()
        truncated = False
        while queue:
            target, level = queue.popleft()
            if target in seen:
                continue
            seen.add(target)
            if ctx.remaining() < 1:
                truncated = True
                break
            work = await self.fetch(target, ctx)
            if not work:
                continue
            seed = normalize(work)
            nodes[seed.id] = graph_node(seed, self.name)
            direction = req.get("direction", "both")
            for side in (["references"] if direction == "out" else ["citations"] if direction == "in"
                         else ["references", "citations"]):
                if len(edges) >= budget:
                    truncated = True
                    break
                data = await self.neighbors(work["paperId"], side, budget - len(edges), ctx)
                truncated = truncated or data.get("next") is not None
                for item in data.get("data", []):
                    record = item.get("citedPaper" if side == "references" else "citingPaper")
                    if not record or not record.get("paperId"):
                        continue
                    child = normalize(record)
                    nodes[child.id] = graph_node(child, self.name)
                    source, dest = (seed, child) if side == "references" else (child, seed)
                    attrs = {}
                    if req.get("include_intent", False):
                        attrs.update({key: item[key] for key in ("intents", "isInfluential") if key in item})
                    if req.get("include_snippets", False):
                        attrs["contexts"] = item.get("contexts", [])
                    edges[(source.id, dest.id)] = graph_edge(source, dest, self.name, **attrs)
                    if level + 1 < depth:
                        queue.append((record["paperId"], level + 1))
            if len(edges) >= budget and queue:
                truncated = True
                break
        return Result(nodes=list(nodes.values()), edges=list(edges.values()), truncated=truncated)

    async def call(self, cap: Capability, req: dict[str, Any], ctx: Any) -> Result:
        if cap == Capability.PAPER_SEARCH:
            return await self.search(req["query"], req, ctx)
        if cap == Capability.CITATION_GRAPH:
            return await self.graph(req, ctx)
        if cap == Capability.PAPER_METADATA:
            ids = input_ids(req)
            valid = [(target, identifier) for target in ids if (identifier := target_id(target))]
            found = {}
            if valid:
                records = await json_request(ctx, "POST", BASE + "/paper/batch", headers=self.headers(ctx),
                    params={"fields": FIELDS}, json={"ids": [identifier for _, identifier in valid]})
                if not isinstance(records, list):
                    raise ProviderError(ErrorKind.TRANSIENT, "Semantic Scholar batch returned an unexpected shape")
                for i, (target, _) in enumerate(valid):
                    found[target] = normalize(records[i]) if i < len(records) and records[i] else None
            return metadata_result([(target, found.get(target)) for target in ids], self.name)
        ids = input_ids(req)
        target = ids[0] if ids else ""
        work = await self.fetch(target, ctx)
        if cap == Capability.CITATION_VERIFY:
            if not work and not target_id(target):
                search = await self.search(str(target), {"limit": 1}, ctx)
                return bibliographic(req, search.hits[0] if search.hits else None, self.name)
            result = bibliographic(req, normalize(work) if work else None, self.name)
            if work and req.get("statement"):
                contexts = await self.neighbors(work["paperId"], "citations", 10, ctx)
                for item in contexts.get("data", []):
                    source = item.get("citingPaper") or {}
                    for passage in item.get("contexts", []):
                        result.claim_evidence.append({"provider": self.name, "source_id": source.get("paperId"),
                            "source_url": "https://www.semanticscholar.org/paper/" + source.get("paperId", ""),
                            "cited_id": work["paperId"], "passage": passage,
                            "source_classification": item.get("intents", []), "scope": "citation_context"})
            return result
        if not work:
            raise ProviderError(ErrorKind.TARGET, "Paper not found in Semantic Scholar")
        hit = normalize(work)
        if cap == Capability.PAPER_RELATED:
            data = await json_request(ctx, "GET", "https://api.semanticscholar.org/recommendations/v1/papers/forpaper/"
                + quote(work["paperId"], safe=""), headers=self.headers(ctx),
                params={"fields": FIELDS, "limit": limit(req)})
            return Result(hits=[normalize(entry, i) for i, entry in enumerate(data.get("recommendedPapers", []), 1)])
        if cap == Capability.PAPER_READ:
            pdf = work.get("openAccessPdf") or {}
            if pdf.get("url"):
                try:
                    return await pdf_document(ctx, pdf["url"], hit, self.name)
                except ProviderError as exc:
                    if exc.kind not in {ErrorKind.TARGET, ErrorKind.BAD_REQUEST}:
                        raise
            return abstract_document(hit, self.name)
        raise ProviderError(ErrorKind.PLAN, "Unsupported Semantic Scholar capability")
