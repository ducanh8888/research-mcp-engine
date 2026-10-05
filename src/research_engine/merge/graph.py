"""Conservative aggregation of independent providers' citation graphs.

This utility is independent of routing; callers supply completed graph Results and a
combined node/edge budget. Edges without retained endpoints are never returned.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from research_engine.server.schemas import Result

from .canonical import STRONG_IDS, _digest, id_handles, normalize_ids
from .core import _cluster_hits, _normalize_hit, _precheck_ambiguity


def merge_graphs(results: Iterable[Result], *, max_nodes: int = 2000,
                 max_edges: int = 100) -> dict[str, Any]:
    """Merge exact identities and duplicate edges without discarding source assertions."""
    if min(max_nodes, max_edges) < 1:
        raise ValueError("Graph budgets must be positive")
    results = list(results)
    indexed: list[dict[str, Any]] = []
    for result_index, result in enumerate(results):
        for node in result.nodes:
            provider = str((node.get("providers") or ["unknown"])[0])
            if not node.get("id"):
                continue
            ids = dict(node.get("ids") or {})
            # Node IDs are provider-scoped aliases, not cross-provider identities.
            raw_id = str(node["id"])
            if ":" in raw_id:
                namespace, value = raw_id.split(":", 1)
                if namespace in {"openalex", "s2", "doi", "arxiv", "pmid", "pmcid"}:
                    if ids.get(namespace) and normalize_ids({namespace: ids[namespace]}).get(namespace) != (
                        normalize_ids({namespace: value}).get(namespace)):
                        raise ValueError("Citation graph node ID disagrees with its asserted identifiers")
                    ids.setdefault(namespace, value)
            mapped = _normalize_hit({"provider": provider, "ids": ids,
                                     "url": node.get("url"), "title": node.get("title", "")})
            mapped["_graph_ref"] = (result_index, raw_id)
            mapped["_assertion"] = dict(node)
            indexed.append(mapped)
    refs = [hit["_graph_ref"] for hit in indexed]
    if len(refs) != len(set(refs)):
        raise ValueError("Citation graph contains duplicate provider node IDs")
    ambiguous = _precheck_ambiguity(indexed)
    clusters = _cluster_hits(indexed, ambiguous)
    # A thin alias may have joined a rich identity before another incompatible
    # strong ID was seen. Never issue a shared handle for that contested alias.
    key_clusters: dict[str, int] = {}
    for cluster in clusters:
        for candidate in cluster["keys"]:
            key_clusters[candidate] = key_clusters.get(candidate, 0) + 1
    ambiguous.update(candidate for candidate, count in key_clusters.items() if count > 1)
    aliases: dict[tuple[int, str], str] = {}
    nodes: list[dict[str, Any]] = []
    truncated = any(result.truncated for result in results)
    for cluster in clusters:
        assertions = cluster["hits"]
        identifiers = cluster["ids"]
        if any(len({hit["ids"][name] for hit in assertions if hit["ids"].get(name)}) > 1
               for name in STRONG_IDS):
            raise ValueError("Citation graph contains conflicting paper identities")
        keys = id_handles(identifiers)
        key = next((candidate for candidate in keys if candidate not in ambiguous), None)
        if not key:
            key = next((candidate for candidate in sorted(cluster["keys"])
                        if candidate.startswith("url:") and candidate not in ambiguous), None)
        if not key:
            fingerprint = "|".join(sorted(f"{hit['_graph_ref'][0]}:{hit['_graph_ref'][1]}" for hit in assertions))
            key = "graph:" + _digest(fingerprint)
        for hit in assertions:
            aliases[hit["_graph_ref"]] = key
        if len(nodes) >= max_nodes:
            truncated = True
            continue
        node = {"id": key, "ids": identifiers,
                "title": next((hit["_assertion"].get("title") for hit in assertions
                               if hit["_assertion"].get("title")), ""),
                "providers": list(dict.fromkeys(hit["provider"] for hit in assertions)),
                "sources": [{"provider": hit["provider"], "id": hit["_graph_ref"][1],
                             "assertion": hit["_assertion"]} for hit in assertions]}
        nodes.append(node)
    retained = {node["id"] for node in nodes}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for result_index, result in enumerate(results):
        for edge in result.edges:
            source = aliases.get((result_index, str(edge.get("source"))))
            target = aliases.get((result_index, str(edge.get("target"))))
            if source not in retained or target not in retained or source is None or target is None:
                truncated = True
                continue
            relation = str(edge.get("relation", "cites"))
            key = source, target, relation
            if key not in edges:
                if len(edges) >= max_edges:
                    truncated = True
                    continue
                edges[key] = {"source": source, "target": target, "relation": relation, "sources": []}
            edges[key]["sources"].append({"provider": edge.get("provider"), "assertion": dict(edge)})
    return {"nodes": nodes, "edges": list(edges.values()), "truncated": truncated}
