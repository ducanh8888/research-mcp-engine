"""Validated public request fields and explicit adapter translations."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from research_engine.providers.base import SEARCH_CAPABILITIES, Capability

DEFAULT_DEADLINE_S = 40.0
MIN_DEADLINE_S = 5.0
MAX_DEADLINE_S = 50.0

# Only these shipped adapters consume the indicated publication-year filters.
YEAR_FILTERS = {
    "openalex": ("from_publication_date", "to_publication_date"),
    "crossref": ("from-pub-date", "until-pub-date"),
    "semantic_scholar": ("year", "year"),
    "consensus_api": ("year_min", "year_max"),
    "elicit_mcp": ("minYear", "maxYear"),
    "scite_mcp": ("date_from", "date_to"),
}


class InvalidRequest(ValueError):
    pass


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value: Any, label: str, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, list) or not 1 <= len(value) <= 100 or any(not _nonempty(item) for item in value):
        raise InvalidRequest(f"{label} must contain 1–100 nonempty strings")


def validate(cap: Capability, args: dict[str, Any]) -> dict[str, Any]:
    """Validate before routing; return an owned copy with the effective deadline."""
    args = dict(args)
    if cap in SEARCH_CAPABILITIES - {Capability.PAPER_RELATED}:
        if not _nonempty(args.get("query")) or len(args["query"].strip()) > 4096:
            raise InvalidRequest("query must contain 1–4096 characters")
    if "limit" in args and (isinstance(args["limit"], bool) or not isinstance(args["limit"], int)
                            or not 1 <= args["limit"] <= 25):
        raise InvalidRequest("limit must be an integer from 1 to 25")
    if cap in {Capability.WEB_READ, Capability.PAPER_READ} and not _nonempty(args.get("target")):
        raise InvalidRequest("target is required")
    for name in ("ids", "seeds"):
        _strings(args.get(name), name, required=name == "seeds" and cap in {
            Capability.PAPER_RELATED, Capability.CITATION_GRAPH})
    if cap in {Capability.PAPER_METADATA, Capability.EDITORIAL_CHECK}:
        if cap == Capability.EDITORIAL_CHECK and "ids" not in args:
            raise InvalidRequest("ids are required")
        if cap == Capability.PAPER_METADATA and not (args.get("ids") or _nonempty(args.get("citation"))):
            raise InvalidRequest("ids or citation is required")
    if cap == Capability.CITATION_VERIFY and not _nonempty(args.get("citation")):
        raise InvalidRequest("citation is required")
    if cap == Capability.CITATION_VERIFY and args.get("claim") is not None and not _nonempty(args["claim"]):
        raise InvalidRequest("claim must contain text")
    for name in ("url", "question", "goal"):
        required = {"url": {Capability.SITE_MAP, Capability.SITE_CRAWL},
                    "question": {Capability.SYSTEMATIC_REVIEW},
                    "goal": {Capability.DEEP_LITERATURE_SEARCH}}[name]
        if cap in required and not _nonempty(args.get(name)):
            raise InvalidRequest(f"{name} is required")
    if cap == Capability.SITE_CRAWL:
        depth = args.get("max_depth", 2)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 10:
            raise InvalidRequest("max_depth must be an integer from 0 to 10")
        for name in ("include_paths", "exclude_paths"):
            _strings(args.get(name), name)
    if cap == Capability.PAPER_RELATED and args.get("mode", "similar") not in {"similar", "citing", "cited"}:
        raise InvalidRequest("mode must be similar, citing or cited")
    if cap == Capability.REPO_SEARCH and args.get("mode", "repos") not in {"repos", "code", "issues"}:
        raise InvalidRequest("mode must be repos, code or issues")
    if cap == Capability.NEWS_SEARCH and args.get("recency") not in {None, "day", "week", "month", "year"}:
        raise InvalidRequest("recency must be day, week, month or year")
    if cap in {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}:
        _strings(args.get("domains"), "domains")
        for field in ("date_from", "date_to"):
            if args.get(field) is not None:
                try:
                    date.fromisoformat(args[field])
                except (TypeError, ValueError) as error:
                    raise InvalidRequest(f"{field} must be an ISO date") from error
        if args.get("date_from") and args.get("date_to") and args["date_from"] > args["date_to"]:
            raise InvalidRequest("date_from must not exceed date_to")
    if cap == Capability.CITATION_GRAPH:
        if args.get("direction", "both") not in {"in", "out", "both"}:
            raise InvalidRequest("direction must be in, out or both")
        if isinstance(args.get("depth", 1), bool) or args.get("depth", 1) not in {1, 2}:
            raise InvalidRequest("Graph depth is limited to 1 or 2")
    if cap == Capability.DEVELOPER_SEARCH:
        _strings(args.get("repos"), "repos")
    if cap == Capability.REPO_SEARCH and args.get("min_stars") is not None:
        stars = args["min_stars"]
        if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
            raise InvalidRequest("min_stars must be a nonnegative integer")
    if cap == Capability.PAPER_SEARCH:
        for name in ("year_from", "year_to"):
            year = args.get(name)
            if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999):
                raise InvalidRequest(f"{name} must be an integer year from 1 to 9999")
        if args.get("year_from") and args.get("year_to") and args["year_from"] > args["year_to"]:
            raise InvalidRequest("year_from must not exceed year_to")
        if args.get("filters") is not None and (not isinstance(args["filters"], dict) or args["filters"]):
            # Provider-specific filter bags have no public contract and were silently dropped.
            raise InvalidRequest("Provider-specific filters are not supported; use year_from/year_to")
    deadline = args.get("deadline_s", DEFAULT_DEADLINE_S)
    if isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or not math.isfinite(deadline) or deadline <= 0:
        raise InvalidRequest("deadline_s must be a positive finite number")
    args["deadline_s"] = max(MIN_DEADLINE_S, min(MAX_DEADLINE_S, deadline))
    return args


def for_provider(cap: Capability, args: dict[str, Any], name: str) -> dict[str, Any]:
    """Return adapter input or an explicit unsupported-filter failure."""
    request = {k: v for k, v in args.items() if v is not None and k not in {"cursor", "deadline_s"}}
    if not name.startswith("omni:"):
        request.pop("fresh", None)
    if cap in {Capability.WEB_READ, Capability.PAPER_READ} and not name.startswith("omni:"):
        request["url"] = request.get("target")
    if cap == Capability.SITE_CRAWL:
        request["depth"] = request.pop("max_depth", 2)
    if cap in {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}:
        domains = request.pop("domains", None)
        start, end = request.pop("date_from", None), request.pop("date_to", None)
        recency = request.pop("recency", None)
        if domains:
            if name not in {"tavily", "exa", "firecrawl", "brave", "serper", "duckduckgo"}:
                raise InvalidRequest("Provider does not support domain filters")
            request["include_domains"] = domains
        if start or end:
            if name not in {"tavily", "exa"}:
                raise InvalidRequest("Provider does not support publication-date filters")
            request.update({**({"published_after": start} if start else {}),
                            **({"published_before": end} if end else {})})
        if recency:
            if name not in {"tavily", "brave", "serper", "firecrawl"}:
                raise InvalidRequest("Provider does not support recency filters")
            request["freshness"] = recency
    if cap == Capability.DEVELOPER_SEARCH and request.get("repos") and name not in {"github", "firecrawl"}:
        raise InvalidRequest("Provider does not support repository filters")
    if cap == Capability.REPO_SEARCH and request.get("min_stars") is not None and name not in {"github", "firecrawl"}:
        raise InvalidRequest("Provider does not support min_stars")
    if cap == Capability.REPO_SEARCH and request.get("mode", "repos") != "repos" and name != "github":
        raise InvalidRequest("Provider does not support this repository search mode")
    if cap == Capability.CITATION_VERIFY:
        claim = request.pop("claim", None)
        if claim and name == "semantic_scholar":
            request["statement"] = claim
    if cap == Capability.PAPER_SEARCH:
        start, end = request.pop("year_from", None), request.pop("year_to", None)
        request.pop("filters", None)
        if start is not None or end is not None:
            if name not in YEAR_FILTERS:
                raise InvalidRequest("Provider does not support publication-year filters")
            first, last = YEAR_FILTERS[name]
            if name == "semantic_scholar":
                year = f"{start or ''}-{end or ''}" if start != end else str(start)
                request["filters"] = {"year": year}
            elif name in {"openalex", "crossref"}:
                request["filters"] = {**({first: f"{start:04d}-01-01"} if start else {}),
                                      **({last: f"{end:04d}-12-31"} if end else {})}
            else:
                request.update({**({"year_from": start} if start else {}),
                                **({"year_to": end} if end else {})})
    return request
