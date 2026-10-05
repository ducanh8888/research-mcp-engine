"""Exact evidence deduplication and plain reciprocal rank fusion (design v3)."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from .canonical import (
    ID_PRIORITY, STRONG_IDS, _digest, id_handles,
    normalize_ids, normalize_url, url_handle,
)

_PRIORITY = ("crossref", "openalex", "semantic_scholar", "semanticscholar", "s2", "pubmed", "arxiv")
_FIELDS = ("title", "url", "snippet", "authors", "year", "venue", "published")


@dataclass
class MergeOutput:
    items: list[dict[str, Any]]
    identities: list[dict[str, Any]]
    ambiguous_aliases: list[str]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError("merge_hits expects Hit objects, mappings, or Results containing hits")


def _flatten(values: Iterable[Any]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for value in values:
        mapped = _mapping(value)
        if "hits" in mapped:
            hits.extend(_mapping(hit) for hit in mapped["hits"])
        else:
            hits.append(mapped)
    return hits


def _normalize_hit(hit: dict[str, Any]) -> dict[str, Any]:
    out = dict(hit)
    out["provider"] = str(hit.get("provider") or hit.get("p") or "unknown")
    out["url"] = normalize_url(hit.get("url"))
    out["source_ids"] = dict(hit.get("ids") or {})
    ids = normalize_ids(hit.get("ids"), hit.get("url"))
    for namespace, value in normalize_ids({}, hit.get("pdf_url")).items():
        ids.setdefault(namespace, value)
    out["ids"] = {
        name if name in ID_PRIORITY or name.startswith("provider:") else f"provider:{out['provider']}:{name}": value
        for name, value in ids.items()
    }
    rank = hit.get("rank", 1)
    out["rank"] = max(1, rank) if isinstance(rank, int) and not isinstance(rank, bool) else 1
    out["_keys"] = set(id_handles(out["ids"]))
    if key := url_handle(out["url"]):
        out["_keys"].add(key)
    return out


def _conflicting(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    return any(left.get(name) and right.get(name) and left[name] != right[name] for name in STRONG_IDS)


def _precheck_ambiguity(hits: list[dict[str, Any]]) -> set[str]:
    """Do not arbitrarily attach ID-less evidence to a shared conflicting key."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        for key in hit["_keys"]:
            buckets[key].append(hit)
    ambiguous = set()
    for key, candidates in buckets.items():
        if any(len({hit["ids"][name] for hit in candidates if hit["ids"].get(name)}) > 1 for name in STRONG_IDS):
            ambiguous.add(key)
    return ambiguous


def _source_order(hit: dict[str, Any]) -> tuple[Any, ...]:
    provider = hit["provider"].lower().replace("-", "_")
    priority = _PRIORITY.index(provider) if provider in _PRIORITY else len(_PRIORITY)
    return priority, provider, hit["rank"], str(hit.get("account")), hit.get("url", "")


def _cluster_hits(hits: list[dict[str, Any]], ambiguous: set[str]) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # ID-rich records first makes the result deterministic for thin ID-less hits.
    for hit in sorted(hits, key=lambda h: (-len(h["ids"]), _source_order(h))):
        keys = hit["_keys"] - ambiguous
        candidates: list[dict[str, Any]] = []
        for key in sorted(keys):
            for candidate in index[key]:
                if all(candidate is not current for current in candidates) and not _conflicting(candidate["ids"], hit["ids"]):
                    candidates.append(candidate)
        target = candidates[0] if candidates else {"hits": [], "ids": {}, "keys": set()}
        if not candidates:
            clusters.append(target)
        # A bridging hit can join compatible exact clusters, never ID conflicts.
        for candidate in candidates[1:]:
            if _conflicting(target["ids"], candidate["ids"]):
                continue
            target["hits"].extend(candidate["hits"])
            for name, value in candidate["ids"].items():
                target["ids"].setdefault(name, value)
            target["keys"].update(candidate["keys"])
            clusters.remove(candidate)
            for key in candidate["keys"]:
                index[key] = [target if item is candidate else item for item in index[key]]
        target["hits"].append(hit)
        for name, value in hit["ids"].items():
            target["ids"].setdefault(name, value)
        target["keys"].update(hit["_keys"])
        for key in keys:
            if all(item is not target for item in index[key]):
                index[key].append(target)
    return clusters


def _weights(options: Mapping[str, Any]) -> dict[str, float]:
    if not options.get("weighted_rrf", False):
        return {}
    weights = {str(name): float(weight) for name, weight in options.get("rrf_weights", {}).items()}
    if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
        raise ValueError("RRF weights must be finite and nonnegative")
    return weights


def _hierarchical_fusion(items: list[dict[str, Any]], options: Mapping[str, Any]) -> None:
    """Explicit deferred two-stage RRF: provider lists, then independence groups."""
    if not options.get("hierarchical_rrf"):
        return
    group_names = dict(options.get("independence_groups", {}))
    provider_weights = _weights(options)
    group_weights = {str(group): float(weight) for group, weight in options.get("group_weights", {}).items()}
    if any(not math.isfinite(weight) or weight < 0 for weight in group_weights.values()):
        raise ValueError("Group weights must be finite and nonnegative")
    grouped: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for item in items:
        ranks: dict[str, int] = {}
        for source in item["providers"]:
            ranks[source["p"]] = min(ranks.get(source["p"], source["rank"]), source["rank"])
        scores: dict[str, float] = defaultdict(float)
        for provider, rank in ranks.items():
            group = str(group_names.get(provider, provider))
            scores[group] += provider_weights.get(provider, 1) / (60 + rank)
        for group, score in scores.items():
            grouped[group].append((score, item))
        item["score"] = 0.0
        item["fusion"] = {"method": "hierarchical_rrf", "groups": []}
    for group, candidates in sorted(grouped.items()):
        candidates.sort(key=lambda pair: (-pair[0], pair[1]["_best_rank"], pair[1]["handle"]))
        for rank, (_, item) in enumerate(candidates, start=1):
            item["score"] += group_weights.get(group, 1) / (60 + rank)
            item["fusion"]["groups"].append({"name": group, "rank": rank})


def merge_hits(
    hits: Iterable[Any], limit: int | None = 25, options: Mapping[str, Any] | None = None,
) -> MergeOutput:
    """Merge exact compatible evidence; keep conflicting evidence separate.

    ``options`` enables only explicit deferred behavior. Search snippets are
    capped at 120 words; complete provider payloads remain available upstream.
    ``identities`` includes all merged records before the output limit.
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer or None")
    options = dict(options or {})
    weights = _weights(options)
    normalized = [_normalize_hit(hit) for hit in _flatten(hits)]
    ambiguous = _precheck_ambiguity(normalized)
    clusters = _cluster_hits(normalized, ambiguous)
    # A transitive conflict can make a previously unique alias unsafe too.
    key_counts = Counter(key for cluster in clusters for key in cluster["keys"])
    ambiguous.update(key for key, count in key_counts.items() if count > 1)
    items: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for cluster in clusters:
        sources = sorted(cluster["hits"], key=_source_order)
        fields: dict[str, Any] = {}
        item: dict[str, Any] = {"ids": dict(cluster["ids"]), "title": "", "url": "", "snippet": ""}
        for field in _FIELDS:
            source = next((hit for hit in sources if hit.get(field) not in (None, "", [], {})), None)
            if source is not None:
                item[field] = source[field]
                fields[field] = {"p": source["provider"], "account": source.get("account"), "rank": source["rank"]}
        item["snippet"] = " ".join(str(item["snippet"]).split()[:120])
        candidates = id_handles(item["ids"])
        if url_alias := url_handle(item["url"]):
            candidates.append(url_alias)
        handle = next((key for key in candidates if key not in ambiguous), "")
        if not handle:
            # Explicit source handle instead of an arbitrarily chosen ambiguous URL.
            fingerprint = json.dumps([
                {key: hit.get(key) for key in ("provider", "account", "rank", "ids", "url", "title")}
                for hit in sources
            ], sort_keys=True)
            handle = f"provider:{sources[0]['provider']}:{_digest(fingerprint)}"
        item["handle"] = handle
        item["fields"] = fields
        providers = []
        best_ranks: dict[str, int] = {}
        for source in sources:
            provider = source["provider"]
            best_ranks[provider] = min(best_ranks.get(provider, source["rank"]), source["rank"])
            provenance = {
                "p": provider, "account": source.get("account"), "rank": source["rank"],
                "ids": source["ids"], "url": source["url"],
            }
            if source["source_ids"] and source["source_ids"] != source["ids"]:
                provenance["source_ids"] = source["source_ids"]
            transport = (source.get("raw") or {}).get("transport")
            if transport == "omniroute":
                provenance["transport"] = transport
            if options.get("include_raw") and source.get("raw"):
                provenance["raw"] = source["raw"]
            providers.append(provenance)
        item["providers"] = providers
        item["score"] = sum(weights.get(provider, 1.0) / (60 + rank) for provider, rank in best_ranks.items())
        item["_best_rank"] = min(best_ranks.values())
        if any(key in ambiguous for key in cluster["keys"]):
            item["ambiguous_aliases"] = sorted(cluster["keys"] & ambiguous)
        aliases = sorted(cluster["keys"] - ambiguous - {handle})
        identities.append({
            "handle": handle, "ids": item["ids"], "url": item["url"], "title": item["title"], "aliases": aliases,
        })
        items.append(item)
    _hierarchical_fusion(items, options)
    items.sort(key=lambda item: (-item["score"], item["_best_rank"], item["handle"]))
    for item in items:
        item.pop("_best_rank", None)
    if options.get("fuzzy_relationships") or options.get("version_links") or options.get("near_duplicate_links"):
        from .relationships import add_relationships
        add_relationships(items, normalized, options)
    return MergeOutput(items if limit is None else items[:limit], identities, sorted(ambiguous))


merge_results = merge_hits
