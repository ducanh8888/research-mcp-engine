"""OpenAlex CC0 REST metadata, graph topology, and open access discovery."""

from __future__ import annotations

from collections import deque
from typing import Any
from urllib.parse import quote

from research_engine.providers.base import Capability, ErrorKind, Hit, Provider, ProviderError, Result
from .common import (abstract_document, bibliographic, doi, graph_edge, graph_node, input_ids,
                     json_request, limit, metadata_result, pdf_document)

BASE = "https://api.openalex.org"


def inverted_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positions = {position: word for word, places in index.items() for position in places}
    return " ".join(positions[position] for position in sorted(positions))


def normalize(work: dict[str, Any], rank: int = 1) -> Hit:
    identifier = str(work.get("id", "")).rsplit("/", 1)[-1]
    ids = {"openalex": identifier} if identifier else {}
    for field in ("doi", "pmid", "pmcid", "mag"):
        value = (work.get("ids") or {}).get(field) or (work.get("doi") if field == "doi" else None)
        if value:
            ids[field] = doi(value) if field == "doi" else str(value).rsplit("/", 1)[-1]
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return Hit(provider="openalex", id="openalex:" + identifier, rank=rank,
        title=work.get("display_name") or work.get("title") or "",
        url=ids.get("doi") and "https://doi.org/" + ids["doi"] or work.get("id") or "",
        ids=ids, snippet=inverted_abstract(work.get("abstract_inverted_index")),
        authors=[entry.get("author", {}).get("display_name", "") for entry in work.get("authorships", [])],
        year=work.get("publication_year"), published=work.get("publication_date"),
        venue=source.get("display_name"), raw={key: work[key] for key in
            ("is_retracted", "open_access", "best_oa_location", "referenced_works", "related_works",
             "cited_by_count", "type", "updated_date") if key in work})


def target_id(target: str) -> str | None:
    identifier = doi(target)
    if identifier:
        return "https://doi.org/" + identifier
    target = target.removeprefix("openalex:")
    if target.startswith("https://openalex.org/"):
        target = target.rsplit("/", 1)[-1]
    if target.startswith("W") and target[1:].isdigit():
        return target
    if target.lower().startswith(("pmid:", "pmcid:", "mag:")):
        return target
    if "pubmed.ncbi.nlm.nih.gov/" in target:
        return "pmid:" + target.rstrip("/").rsplit("/", 1)[-1]
    return None


class OpenAlexProvider(Provider):
    name = "openalex"
    keyless = True
    capabilities = frozenset({Capability.PAPER_SEARCH, Capability.PAPER_METADATA, Capability.PAPER_READ,
        Capability.PAPER_RELATED, Capability.CITATION_GRAPH, Capability.CITATION_VERIFY,
        Capability.EDITORIAL_CHECK})
    options = {"rate_limit_rps": 10, "concurrency": 2}

    def params(self, ctx: Any, **values: Any) -> dict[str, Any]:
        key = ctx.credentials.get("api_key") or ctx.credentials.get("key")
        if key:
            values["api_key"] = key
        email = ctx.options.get("mailto") or ctx.credentials.get("mailto")
        if email:
            values["mailto"] = email
        return values

    async def fetch(self, target: str, ctx: Any) -> dict[str, Any] | None:
        identifier = target_id(target)
        if not identifier:
            return None
        return await json_request(ctx, "GET", BASE + "/works/" + quote(identifier, safe="/:"),
                                  params=self.params(ctx), not_found=True)

    async def search(self, query: str, req: dict[str, Any], ctx: Any) -> Result:
        params = self.params(ctx, search=query, **{"per-page": limit(req)})
        filters = req.get("filters") or {}
        if isinstance(filters, dict):
            choices = []
            for key in ("from_publication_date", "to_publication_date", "publication_year", "is_retracted",
                        "type", "open_access.is_oa"):
                if key in filters:
                    choices.append(key + ":" + str(filters[key]).lower())
            if choices:
                params["filter"] = ",".join(choices)
        data = await json_request(ctx, "GET", BASE + "/works", params=params)
        return Result(hits=[normalize(work, i) for i, work in enumerate(data.get("results", []), 1)],
                      raw={"total": data.get("meta", {}).get("count")})

    async def metadata(self, ids: list[str], ctx: Any) -> Result:
        if len(ids) <= 1:
            return metadata_result([(target, normalize(work) if (work := await self.fetch(target, ctx)) else None)
                                    for target in ids], self.name)
        groups: dict[str, list[tuple[str, str]]] = {}
        for target in ids:
            identifier = target_id(target)
            if not identifier:
                continue
            if identifier.startswith("https://doi.org/"):
                field, value = "doi", identifier.removeprefix("https://doi.org/")
            elif identifier.startswith("W"):
                field, value = "openalex", identifier
            else:
                field, value = identifier.split(":", 1)
                field = field.lower()
            groups.setdefault(field, []).append((target, value))
        found: dict[str, Hit] = {}
        for field, requests in groups.items():
            for offset in range(0, len(requests), 100):
                chunk = requests[offset:offset + 100]
                data = await json_request(ctx, "GET", BASE + "/works", params=self.params(ctx,
                    filter=field + ":" + "|".join(value for _, value in chunk), **{"per-page": 100}))
                for work in data.get("results", []):
                    hit = normalize(work)
                    value = hit.ids.get(field)
                    for target, wanted in chunk:
                        if value and value.lower() == wanted.lower():
                            found[target] = hit
        return metadata_result([(target, found.get(target)) for target in ids], self.name)

    async def graph(self, req: dict[str, Any], ctx: Any) -> Result:
        max_edges = max(1, min(int(req.get("max_edges", 100)), 2000))
        depth = max(1, min(int(req.get("depth", 1)), 2))
        direction = req.get("direction", "both")
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
            neighbors: list[str] = []
            if direction in {"out", "both"}:
                refs = work.get("referenced_works", [])
                for ref in refs:
                    if len(edges) >= max_edges:
                        truncated = True
                        break
                    child_id = "openalex:" + ref.rsplit("/", 1)[-1]
                    child = {"id": child_id, "ids": {"openalex": ref.rsplit("/", 1)[-1]},
                             "providers": [self.name]}
                    nodes.setdefault(child_id, child)
                    edges[(seed.id, child_id)] = graph_edge(seed, child, self.name)
                    neighbors.append(ref)
            if direction in {"in", "both"} and len(edges) >= max_edges and work.get("cited_by_count") != 0:
                truncated = True
            if direction in {"in", "both"} and len(edges) < max_edges:
                count = min(100, max_edges - len(edges))
                data = await json_request(ctx, "GET", BASE + "/works", params=self.params(ctx,
                    filter="cites:" + seed.ids["openalex"], **{"per-page": count}))
                if data.get("meta", {}).get("count", 0) > count:
                    truncated = True
                for entry in data.get("results", []):
                    child = normalize(entry)
                    nodes[child.id] = graph_node(child, self.name)
                    edges[(child.id, seed.id)] = graph_edge(child, seed, self.name)
                    neighbors.append(entry["id"])
            if level + 1 < depth:
                queue.extend((neighbor, level + 1) for neighbor in neighbors)
            if len(edges) >= max_edges and queue:
                truncated = True
                break
        return Result(nodes=list(nodes.values()), edges=list(edges.values()), truncated=truncated)

    async def call(self, cap: Capability, req: dict[str, Any], ctx: Any) -> Result:
        if cap == Capability.PAPER_SEARCH:
            return await self.search(req["query"], req, ctx)
        if cap == Capability.CITATION_GRAPH:
            return await self.graph(req, ctx)
        if cap == Capability.CITATION_VERIFY:
            citation = req.get("citation", "")
            value = (citation.get("doi") or citation.get("id") or citation.get("title", "")) if isinstance(citation, dict) else citation
            work = await self.fetch(value, ctx)
            if not work:
                search = await self.search(str(value), {"limit": 1}, ctx)
                return bibliographic(req, search.hits[0] if search.hits else None, self.name)
            return bibliographic(req, normalize(work), self.name)
        ids = input_ids(req)
        if cap == Capability.PAPER_METADATA:
            return await self.metadata(ids, ctx)
        if cap == Capability.EDITORIAL_CHECK:
            checks = []
            for target in ids:
                work = await self.fetch(target, ctx)
                checks.append({"requested_id": target, "id": target, "provider": self.name,
                    "status": "retracted" if work and work.get("is_retracted") else "unknown",
                    "is_retracted": work.get("is_retracted") if work else None,
                    "notices": [], "source": work.get("id") if work else None,
                    "coverage": "retraction_flag_only" if work else "not_found"})
            return Result(checks=checks)
        target = ids[0] if ids else ""
        work = await self.fetch(target, ctx)
        if not work:
            raise ProviderError(ErrorKind.TARGET, "Paper not found in OpenAlex")
        hit = normalize(work)
        if cap == Capability.PAPER_RELATED:
            related = []
            for identifier in work.get("related_works", [])[:limit(req)]:
                record = await self.fetch(identifier, ctx)
                if record:
                    related.append(normalize(record, len(related) + 1))
            return Result(hits=related)
        if cap == Capability.PAPER_READ:
            location = work.get("best_oa_location") or {}
            url = location.get("pdf_url")
            if url:
                try:
                    return await pdf_document(ctx, url, hit, self.name)
                except ProviderError as exc:
                    if exc.kind not in {ErrorKind.TARGET, ErrorKind.BAD_REQUEST}:
                        raise
            return abstract_document(hit, self.name)
        raise ProviderError(ErrorKind.PLAN, "Unsupported OpenAlex capability")
