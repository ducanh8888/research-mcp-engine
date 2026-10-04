"""Explicit deferred relationship hints; never collapse source evidence."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from typing import Any

from rapidfuzz.fuzz import ratio

from .canonical import normalize_ids


def normalized_title(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _authors(item: Mapping[str, Any]) -> set[str]:
    authors = item.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    return {
        normalized_title(author.get("name", "") if isinstance(author, Mapping) else str(author))
        for author in authors
        if author
    } - {""}


def _year(item: Mapping[str, Any]) -> int | None:
    try:
        return int(item["year"])
    except (KeyError, TypeError, ValueError):
        return None


def _simhash(text: str, min_tokens: int) -> int | None:
    tokens = re.findall(r"\w+", text.casefold())
    if len(tokens) < min_tokens:
        return None
    values = [0] * 64
    for token, frequency in Counter(tokens).items():
        bits = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        for index in range(64):
            values[index] += frequency if bits & (1 << index) else -frequency
    return sum(1 << index for index, value in enumerate(values) if value > 0)


def _link(left: dict[str, Any], right: dict[str, Any], kind: str, **assertion: Any) -> None:
    for source, target in ((left, right), (right, left)):
        relation = {"type": kind, "handle": target["handle"], **assertion}
        related = source.setdefault("related", [])
        if relation not in related:
            related.append(relation)


def _source_item(source: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in items if any(
        provider["p"] == source["provider"] and provider["rank"] == source["rank"]
        and provider["ids"] == source["ids"] and provider["url"] == source["url"]
        for provider in item["providers"]
    )), None)


def _version_relations(source: dict[str, Any]):
    raw = source.get("raw") or {}
    if isinstance(raw.get("message"), Mapping):
        raw = raw["message"]
    relation = raw.get("relation", {})
    if isinstance(relation, Mapping):
        for kind in ("is-preprint-of", "has-preprint", "is-version-of", "has-version", "is-identical-to"):
            for edge in relation.get(kind, []) or []:
                if isinstance(edge, Mapping) and edge.get("id-type") == "doi":
                    yield kind, edge.get("id")
    for edge in source.get("related", []) or raw.get("related", []) or []:
        if isinstance(edge, Mapping) and edge.get("type") in {
            "is-preprint-of", "has-preprint", "is-version-of", "has-version", "is-identical-to",
        }:
            yield edge["type"], edge.get("doi") or (edge.get("ids") or {}).get("doi")


def add_relationships(
    items: list[dict[str, Any]], sources: list[dict[str, Any]], options: Mapping[str, Any],
) -> None:
    """Add explainable relationship hints; exact merge identities stay intact."""
    if options.get("version_links"):
        for source in sources:
            item = _source_item(source, items)
            if item is None:
                continue
            for kind, value in _version_relations(source):
                if not value:
                    continue
                ids = normalize_ids({"doi": value})
                if not ids.get("doi"):
                    continue
                target = f"doi:{ids['doi']}"
                if target == item["handle"]:
                    continue
                relation = {"type": kind, "handle": target, "asserted_by": source["provider"]}
                if relation not in item.setdefault("related", []):
                    item["related"].append(relation)
    fuzzy = bool(options.get("fuzzy_relationships"))
    near = bool(options.get("near_duplicate_links"))
    threshold = float(options.get("fuzzy_title_threshold", 0.97))
    if not 0 <= threshold <= 1:
        raise ValueError("fuzzy_title_threshold must be between zero and one")
    texts = [normalized_title(str(item.get("title") or "")) for item in items]
    hashes = [_simhash(str(item.get("snippet") or ""), int(options.get("simhash_min_tokens", 50))) for item in items] if near else []
    max_distance = int(options.get("simhash_max_distance", 3))
    if not 0 <= max_distance <= 64:
        raise ValueError("simhash_max_distance must be between zero and 64")
    for left_index, left in enumerate(items):
        for right_index in range(left_index + 1, len(items)):
            right = items[right_index]
            if fuzzy and min(len(texts[left_index]), len(texts[right_index])) >= 20:
                years = _year(left), _year(right)
                years_match = all(year is not None for year in years) and abs(years[0] - years[1]) <= 1
                authors_match = bool(_authors(left) & _authors(right)) or not options.get("fuzzy_require_author", True)
                similarity = ratio(texts[left_index], texts[right_index]) / 100
                if years_match and authors_match and similarity >= threshold:
                    _link(left, right, "possible_duplicate", asserted_by="research-engine:fuzzy-title", similarity=round(similarity, 4))
            if near and hashes[left_index] is not None and hashes[right_index] is not None:
                distance = (hashes[left_index] ^ hashes[right_index]).bit_count()
                if distance <= max_distance:
                    _link(left, right, "near_duplicate", asserted_by="research-engine:simhash", distance=distance)
