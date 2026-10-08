"""Regression fixtures for the 2026-10-08 external benchmark findings.

FIND-01/09 news recency, FIND-07 duplicate provenance, FIND-08 paper_explore
limit contract, FIND-04/05 scholarly identity and citation comparison, FIND-06
claim evidence. Dates are fixed; nothing depends on the wall clock.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from research_engine.merge import merge_hits
from research_engine.merge.recency import enforce_recency, parse_published
from research_engine.providers.base import Capability, Hit
from research_engine.providers.omniroute import OmniRouteProvider
from research_engine.providers.scholar import openalex
from research_engine.providers.scholar.common import bibliographic
from research_engine.router import execute
from research_engine.router.requests import for_provider
from test_evidence_coverage import assert_coverage, call, mcp_fixture

TODAY = date(2026, 10, 8)


# --- FIND-01/09: publication dates and the recency window -------------------

@pytest.mark.parametrize("value,expected", [
    ("2026-10-07", date(2026, 10, 7)),
    ("2026-10-07T23:10:00Z", date(2026, 10, 7)),
    ("2026-10-07T23:10:00+07:00", date(2026, 10, 7)),
    ("Sep 14, 2026", date(2026, 9, 14)),
    ("September 14, 2026", date(2026, 9, 14)),
    ("Sept. 14, 2026", date(2026, 9, 14)),
    ("2 weeks ago", None),        # relative: would need retrieval time
    ("2026", None),               # a bare year is not a date
    ("2026-02-30", None),         # impossible date
    ("Smarch 3, 2026", None),
    ("published recently", None),
    ("", None),
    (None, None),
    (20261007, None),
])
def test_only_absolute_reported_dates_are_publication_evidence(value, expected):
    assert parse_published(value) == expected


def item(url: str, *dates: str | None, provider: str = "omni:firecrawl") -> dict:
    sources = [{"p": provider if index == 0 else f"other-{index}", "rank": 1, "url": url,
                **({"published": value} if value is not None else {})} for index, value in enumerate(dates)]
    return {"url": url, "handle": url, "providers": sources or [{"p": provider, "rank": 1, "url": url}]}


def test_recency_keeps_in_window_drops_known_out_of_window_and_labels_the_rest():
    items = [
        item("https://a.example/in", "2026-10-06"),
        item("https://a.example/out", "Sep 14, 2026"),
        item("https://a.example/missing", None),
        item("https://a.example/malformed", "2 weeks ago"),
        item("https://a.example/conflict", "2026-10-07", "2026-09-18"),
        item("https://a.example/start", "2026-10-01"),     # TODAY - 7 days: inside
        item("https://a.example/before", "2026-09-30"),    # one day earlier: outside
    ]
    kept, report = enforce_recency(items, "week", TODAY)
    assert [(entry["url"].rsplit("/", 1)[-1], entry["recency_check"]) for entry in kept] == [
        ("in", "verified"), ("missing", "unverified"), ("malformed", "unverified"),
        ("conflict", "conflicting"), ("start", "verified")]
    assert {key: report[key] for key in ("verified", "unverified", "conflicting", "excluded_out_of_window")} == {
        "verified": 2, "unverified": 2, "conflicting": 1, "excluded_out_of_window": 2}
    assert report["window_start"] == "2026-10-01" and report["window_end"] == "2026-10-08"
    assert report["by_provider"]["omni:firecrawl"] == {"in_window": 3, "out_of_window": 2, "no_date": 2}
    # Inputs are not mutated and fused order is preserved.
    assert "recency_check" not in items[0]


def test_recency_with_no_compliant_results_returns_an_empty_list():
    kept, report = enforce_recency([item("https://a.example/old", "2026-01-01")], "day", TODAY)
    assert kept == [] and report["excluded_out_of_window"] == 1 and report["verified"] == 0


def news(provider_id: str, rows: list[tuple[str, str | None]]) -> dict:
    return {"provider": provider_id, "errors": [], "cached": False, "results": [
        {"title": url.rsplit("/", 1)[-1], "url": url, "snippet": "s", "position": rank,
         "published_at": published, "citation": {"provider": provider_id, "rank": rank}}
        for rank, (url, published) in enumerate(rows, 1)]}


async def test_news_recency_enforces_reported_dates_end_to_end(tmp_path: Path, monkeypatch):
    """Reproduces FIND-01: Nimble supplies no dates, Firecrawl reports an old item."""
    monkeypatch.setattr(execute, "_today", lambda: TODAY)
    firecrawl = OmniRouteProvider("omni:firecrawl", "firecrawl", {Capability.NEWS_SEARCH})
    nimble = OmniRouteProvider("omni:nimble-search", "nimble-search", {Capability.NEWS_SEARCH})
    serper = OmniRouteProvider("omni:serper-search", "serper-search", {Capability.NEWS_SEARCH})
    sent: list[dict] = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        if body["provider"] == "firecrawl":
            return httpx.Response(200, json=news("firecrawl", [
                ("https://news.example/fresh", "Oct 6, 2026"),
                ("https://news.example/stale", "Sep 14, 2026"),
                ("https://news.example/shared", "2026-10-07")]))
        return httpx.Response(200, json=news("nimble-search", [
            ("https://news.example/undated", None), ("https://news.example/shared", None)]))

    routes = {"news_search": [firecrawl.name, nimble.name, serper.name]}
    async with mcp_fixture(tmp_path, [firecrawl, nimble, serper], routes, upstream=upstream, bridge=True) as (
            runtime, _):
        payload, error = await call(runtime, "news_search",
                                    {"query": "AI chips", "recency": "week", "limit": 8, "fresh": True})
    assert not error
    assert_coverage(payload, {firecrawl.name, nimble.name}, skipped={serper.name})
    checks = {entry["url"].rsplit("/", 1)[-1]: entry["recency_check"] for entry in payload["items"]}
    assert checks == {"fresh": "verified", "shared": "verified", "undated": "unverified"}
    shared = next(entry for entry in payload["items"] if entry["url"].endswith("/shared"))
    assert [(source["p"], source.get("published")) for source in shared["providers"]] == [
        ("firecrawl", "2026-10-07"), ("nimble-search", None)]
    assert payload["recency"]["excluded_out_of_window"] == 1 and payload["recency"]["unverified"] == 1
    assert payload["recency"]["by_provider"]["nimble-search"] == {"in_window": 0, "out_of_window": 0, "no_date": 2}
    assert payload["status"] == "partial"
    assert all("fresh" not in body and body["time_range"] == "week" for body in sent)


async def test_news_recency_failed_peer_and_zero_hit_provider_keep_coverage(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(execute, "_today", lambda: TODAY)
    firecrawl = OmniRouteProvider("omni:firecrawl", "firecrawl", {Capability.NEWS_SEARCH})
    nimble = OmniRouteProvider("omni:nimble-search", "nimble-search", {Capability.NEWS_SEARCH})

    def failing(request):
        if json.loads(request.content)["provider"] == "nimble-search":
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=news("firecrawl", [("https://news.example/fresh", "2026-10-08")]))

    def empty(request):
        provider = json.loads(request.content)["provider"]
        return httpx.Response(200, json=news(provider, [("https://news.example/old", "2025-12-01")]
                                             if provider == "firecrawl" else []))

    routes = {"news_search": [firecrawl.name, nimble.name]}
    async with mcp_fixture(tmp_path, [firecrawl, nimble], routes, upstream=failing, bridge=True) as (runtime, _):
        payload, error = await call(runtime, "news_search", {"query": "q", "recency": "day"})
    assert not error and payload["status"] == "partial"
    assert_coverage(payload, {firecrawl.name}, failed={nimble.name})
    assert [entry["recency_check"] for entry in payload["items"]] == ["verified"]

    async with mcp_fixture(tmp_path / "zero", [firecrawl, nimble], routes, upstream=empty, bridge=True) as (
            runtime, _):
        payload, error = await call(runtime, "news_search", {"query": "q", "recency": "day"})
    assert not error and payload["items"] == []
    assert_coverage(payload, {firecrawl.name, nimble.name})
    assert payload["recency"]["excluded_out_of_window"] == 1 and payload["status"] == "complete"


async def test_search_without_recency_is_unchanged(tmp_path: Path):
    firecrawl = OmniRouteProvider("omni:firecrawl", "firecrawl", {Capability.NEWS_SEARCH})

    def upstream(request):
        return httpx.Response(200, json=news("firecrawl", [("https://news.example/old", "2020-01-01")]))

    async with mcp_fixture(tmp_path, [firecrawl], {"news_search": [firecrawl.name]}, upstream=upstream,
                           bridge=True) as (runtime, _):
        payload, error = await call(runtime, "news_search", {"query": "q"})
    assert not error and payload["status"] == "complete" and "recency" not in payload
    assert [entry["published"] for entry in payload["items"]] == ["2020-01-01"]
    assert "recency_check" not in payload["items"][0]


# --- FIND-07: identical same-provider sightings --------------------------------

def sighting(provider: str, rank: int, url: str, **extra) -> dict:
    return {"provider": provider, "rank": rank, "url": url, "title": "MCP authorization", **extra}


def test_repeated_identical_sightings_collapse_to_best_rank_without_changing_rrf():
    url = "https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization"
    repeated = [sighting("tavily-search", rank, url) for rank in range(8, 0, -1)]
    merged = merge_hits(repeated + [sighting("exa-search", 3, url)]).items
    assert len(merged) == 1
    assert [(source["p"], source["rank"]) for source in merged[0]["providers"]] == [
        ("exa-search", 3), ("tavily-search", 1)]
    assert merged[0]["score"] == pytest.approx(1 / 61 + 1 / 63)
    assert merged[0]["fields"]["title"]["rank"] in {1, 3}


def test_url_variants_collapse_but_distinct_evidence_is_retained():
    variants = [sighting("tavily-search", 2, "https://Example.org/a/"),
                sighting("tavily-search", 1, "https://example.org/a")]
    merged = merge_hits(variants).items
    assert len(merged) == 1 and [source["rank"] for source in merged[0]["providers"]] == [1]
    # Conflicting strong identifiers from one provider stay separate items.
    conflicting = merge_hits([sighting("openalex", 1, "https://example.org/p", ids={"doi": "10.1234/a"}),
                              sighting("openalex", 2, "https://example.org/p", ids={"doi": "10.1234/b"})]).items
    assert len(conflicting) == 2
    # Different reported dates are different evidence and remain visible.
    dated = merge_hits([sighting("serper-search", 1, "https://example.org/n", published="2026-10-01"),
                        sighting("serper-search", 2, "https://example.org/n", published="2026-09-01")]).items
    assert [source.get("published") for source in dated[0]["providers"]] == ["2026-10-01", "2026-09-01"]


def test_related_paper_seed_and_mode_assertions_are_not_collapsed():
    hits = [sighting("openalex", 1, "https://doi.org/10.1234/x", ids={"doi": "10.1234/x"},
                     raw={"related_seed": seed, "related_mode": "similar"}) for seed in ("10.1234/s1", "10.1234/s2")]
    hits.append(sighting("openalex", 4, "https://doi.org/10.1234/x", ids={"doi": "10.1234/x"},
                         raw={"related_seed": "10.1234/s1", "related_mode": "similar"}))
    providers = merge_hits(hits).items[0]["providers"]
    assert [(source["related_seed"], source["rank"]) for source in providers] == [("10.1234/s1", 1), ("10.1234/s2", 1)]


# --- FIND-08: paper_explore limit contract ---------------------------------------

async def test_paper_explore_limit_is_documented_and_rejected_for_citations(tmp_path: Path):
    async with mcp_fixture(tmp_path, [], {}) as (runtime, _):
        async with runtime.client() as client:
            tool = next(tool for tool in await client.list_tools() if tool.name == "paper_explore")
        payload, error = await call(runtime, "paper_explore", {
            "operation": "citations", "ids": ["doi:10.18653/v1/2025.findings-naacl.157"],
            "direction": "both", "depth": 1, "limit": 20})
    schema = tool.inputSchema["properties"]["limit"]
    assert "operation=related only" in schema["description"]
    assert "takes no" in tool.description and "limit" in tool.description
    assert error and payload["error"]["code"] == "INVALID_INPUT"


# --- FIND-04/05: preprint identity and bibliographic comparison -------------------

def test_openalex_resolves_arxiv_ids_through_the_arxiv_datacite_doi():
    assert openalex.target_id("arxiv:2504.17137v2") == "https://doi.org/10.48550/arxiv.2504.17137"
    assert openalex.target_id("https://arxiv.org/abs/2504.17137") == "https://doi.org/10.48550/arxiv.2504.17137"
    preprint = openalex.normalize({"id": "https://openalex.org/W4415306897",
                                   "doi": "https://doi.org/10.48550/arxiv.2504.17137", "display_name": "MIRAGE"})
    published = openalex.normalize({"id": "https://openalex.org/W4411113072",
                                    "doi": "https://doi.org/10.18653/v1/2025.findings-naacl.157",
                                    "display_name": "MIRAGE"})
    assert preprint.ids["arxiv"] == "2504.17137" and "arxiv" not in published.ids
    # Without an authoritative crosswalk the two versions stay separate records.
    assert len(merge_hits([preprint, published]).items) == 2


MIRAGE = Hit(provider="crossref", id="crossref:10.18653/v1/2025.findings-naacl.157",
             title="MIRAGE: A Metric-Intensive Benchmark for Retrieval-Augmented Generation Evaluation",
             ids={"doi": "10.18653/v1/2025.findings-naacl.157"}, year=2025)


@pytest.mark.parametrize("citation,verdict,matched,mismatched", [
    ("Nguyen, T. et al. MIRAGE: A Benchmark for Multilingual Retrieval. NeurIPS 2024. "
     "DOI 10.18653/v1/2025.findings-naacl.157", "mismatch", ["doi"], ["year"]),
    ("Wrong title. 2025. doi:10.18653/v1/2025.findings-naacl.157", "unknown", ["doi", "year"], []),
    ("10.18653/v1/2025.findings-naacl.157", "match", ["doi"], []),
    ("Park et al. MIRAGE: A Metric-Intensive Benchmark for Retrieval-Augmented Generation Evaluation. "
     "NAACL Findings 2025.", "match", ["title", "year"], []),
    ("Some paper, 2024 or 2025, doi:10.18653/v1/2025.findings-naacl.157", "unknown", ["doi"], []),
])
def test_citation_text_compares_only_reliably_parsed_fields(citation, verdict, matched, mismatched):
    result = bibliographic({"citation": citation}, MIRAGE, "crossref").verification
    source = result["sources"][0]
    assert (result["bibliographic"], source["matched_fields"], source["mismatched_fields"]) == (
        verdict, matched, mismatched)


def test_arxiv_identifier_digits_are_not_read_as_a_year():
    hit = Hit(provider="arxiv", id="arxiv:2004.12345", title="T", ids={"arxiv": "2004.12345"}, year=2020)
    result = bibliographic({"citation": "arXiv:2004.12345"}, hit, "arxiv").verification
    assert result["bibliographic"] == "match" and result["sources"][0]["mismatched_fields"] == []


# --- FIND-06: claim evidence states its scope, never a verdict ----------------------

async def test_openalex_claim_returns_the_identified_works_abstract_only(monkeypatch):
    work = {"id": "https://openalex.org/W4411113072", "doi": "https://doi.org/10.18653/v1/2025.findings-naacl.157",
            "display_name": "MIRAGE", "publication_year": 2025,
            "abstract_inverted_index": {"MIRAGE": [0], "consists": [1], "of": [2], "7,560": [3],
                                        "curated": [4], "instances": [5]}}
    provider = openalex.OpenAlexProvider()

    async def fetch(target, ctx):
        return work if target.startswith("10.18653") else None

    async def search(query, req, ctx):
        from research_engine.providers.base import Result
        return Result(hits=[openalex.normalize(work)])

    monkeypatch.setattr(provider, "fetch", fetch)
    monkeypatch.setattr(provider, "search", search)
    claim = "The MIRAGE benchmark contains 7,560 curated instances."
    request = for_provider(Capability.CITATION_VERIFY,
                           {"citation": "10.18653/v1/2025.findings-naacl.157", "claim": claim}, "openalex")
    assert request["claim"] == claim
    result = await provider.call(Capability.CITATION_VERIFY, request, None)
    assert result.claim_evidence == [{"provider": "openalex", "source_id": "openalex:W4411113072",
                                      "scope": "cited_work_abstract", "supports_claim": "unknown",
                                      "passage": "MIRAGE consists of 7,560 curated instances"}]
    # A search-matched record is not an established identity: no passage.
    unresolved = await provider.call(Capability.CITATION_VERIFY, {"citation": "MIRAGE paper", "claim": claim}, None)
    assert unresolved.claim_evidence == []
    assert "claim" not in for_provider(Capability.CITATION_VERIFY, {"citation": "x", "claim": claim}, "crossref")
