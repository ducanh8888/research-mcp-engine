"""The nine public workflow tools, end to end over authenticated MCP transport.

Each test calls a public tool and checks that it dispatches to the intended
internal capability, keeps provenance/coverage, and rejects invalid operation
combinations before any upstream call. Upstreams are local fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research_engine.providers.base import Capability, JobUpdate, Provider
from research_engine.providers.omniroute import OmniRouteProvider
from research_engine.server.schemas import Document, Result
from research_engine.storage.db import create_client_token
from test_evidence_coverage import assert_coverage, call, hit, mcp_fixture
from test_mcp_e2e import Runtime


class Recording(Provider):
    """A routed source that records the adapter request it received."""

    def __init__(self, name: str, caps: set[Capability], result: Result):
        self.name, self.capabilities, self.result = name, frozenset(caps), result
        self.requests: list[tuple[Capability, dict]] = []

    async def call(self, cap, req, ctx):
        self.requests.append((cap, dict(req)))
        return self.result


class Untouchable(Provider):
    def __init__(self, name: str, caps: set[Capability]):
        self.name, self.capabilities = name, frozenset(caps)

    async def call(self, cap, req, ctx):
        pytest.fail(f"{self.name} must not be called for an invalid request")


def omni_search(provider_id: str, title: str) -> dict:
    return {"provider": provider_id, "errors": [], "cached": False, "results": [{
        "title": title, "url": f"https://example.org/{title}", "snippet": title, "position": 1,
        "citation": {"provider": provider_id, "rank": 1}}]}


async def test_search_focus_selects_web_or_news_and_keeps_provenance(tmp_path: Path):
    web = Recording("web-source", {Capability.WEB_SEARCH}, Result(hits=[hit("web-source", "web-item")]))
    news = Recording("news-source", {Capability.NEWS_SEARCH}, Result(hits=[hit("news-source", "news-item")]))
    async with mcp_fixture(tmp_path, [web, news], {"web_search": [web.name], "news_search": [news.name]}) as (
            runtime, _):
        general, error = await call(runtime, "search", {"query": "everyday question"})
        assert not error and [i["title"] for i in general["items"]] == ["web-item"]
        recent, error = await call(runtime, "search", {"query": "today", "focus": "news", "recency": None})
        assert not error and [i["title"] for i in recent["items"]] == ["news-item"]
    assert general["status"] == recent["status"] == "complete"
    assert general["items"][0]["providers"][0]["p"] == "web-source" and general["items"][0]["handle"]
    assert [cap for cap, _ in web.requests] == [Capability.WEB_SEARCH]
    assert [cap for cap, _ in news.requests] == [Capability.NEWS_SEARCH]


async def test_search_fresh_bypasses_only_engine_cache_through_omniroute(tmp_path: Path):
    bridge = OmniRouteProvider("omni:exa-search", "exa-search", {Capability.WEB_SEARCH})
    sent: list[dict] = []

    def upstream(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=omni_search("exa-search", "fresh-item"))

    async with mcp_fixture(tmp_path, [bridge], {"web_search": [bridge.name]}, upstream=upstream, bridge=True) as (
            runtime, _):
        first, _ = await call(runtime, "search", {"query": "cache me"})
        cached, _ = await call(runtime, "search", {"query": "cache me"})
        fresh, error = await call(runtime, "search", {"query": "cache me", "fresh": True})
    assert not error and fresh["status"] == "complete"
    assert_coverage(fresh, {bridge.name})
    assert [i["title"] for i in fresh["items"]] == ["fresh-item"]
    # Second plain call is served from the engine cache; fresh=true goes upstream again.
    assert first["request_id"] != cached["request_id"] and len(sent) == 2
    assert all("fresh" not in body for body in sent)


async def test_read_paginates_and_fresh_bypasses_document_cache(tmp_path: Path):
    text = "a" * 20_000 + "b" * 5_000
    reader = Recording("reader", {Capability.WEB_READ},
                       Result(document=Document(url="https://example.org/long", text=text, source="reader")))
    async with mcp_fixture(tmp_path, [reader], {"web_read": [reader.name]}, modes={"web_read": "sequential"}) as (
            runtime, _):
        page, error = await call(runtime, "read", {"target": "https://example.org/long"})
        assert not error and page["status"] == "complete" and set(page["document"]["text"]) == {"a"}
        cursor = page["document"]["next_cursor"]
        rest, error = await call(runtime, "read", {"target": "https://example.org/long", "cursor": cursor})
        assert not error and rest["document"]["text"] == "b" * 5_000 and rest["document"]["next_cursor"] is None
        handle, _ = await call(runtime, "read", {"target": page["document"]["handle"]})
        assert handle["document"]["text"] == page["document"]["text"]
        assert len(reader.requests) == 1  # cursor and handle reads come from the document cache
        fresh, error = await call(runtime, "read", {"target": "https://example.org/long", "fresh": True})
    assert not error and fresh["status"] == "complete" and len(reader.requests) == 2


@pytest.mark.parametrize("target,expected", [
    ("10.1234/example", "paper"), ("doi:10.1234/example", "paper"), ("https://doi.org/10.1234/example", "paper"),
    ("2401.12345", "paper"), ("https://example.org/article", "web"),
])
async def test_read_auto_selects_paper_or_web_reader(tmp_path: Path, target: str, expected: str):
    paper = Recording("paper-reader", {Capability.PAPER_READ}, Result(document=Document(
        url="https://doi.org/10.1234/example", text="Abstract only", source="paper-reader", kind="abstract")))
    web = Recording("web-reader", {Capability.WEB_READ}, Result(document=Document(
        url="https://example.org/article", text="Article text", source="web-reader")))
    async with mcp_fixture(tmp_path, [paper, web], {"paper_read": [paper.name], "web_read": [web.name]},
                           modes={"paper_read": "sequential", "web_read": "sequential"}) as (runtime, _):
        payload, error = await call(runtime, "read", {"target": target})
    assert not error
    assert (len(paper.requests), len(web.requests)) == ((1, 0) if expected == "paper" else (0, 1))
    assert payload["document"]["kind"] == ("abstract" if expected == "paper" else "page")


async def test_search_filters_skip_sources_that_cannot_apply_them(tmp_path: Path):
    exa = OmniRouteProvider("omni:exa-search", "exa-search", {Capability.WEB_SEARCH})
    serper = OmniRouteProvider("omni:serper-search", "serper-search", {Capability.WEB_SEARCH})
    sent: list[dict] = []

    def upstream(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json=omni_search(body["provider"], "filtered"))

    async with mcp_fixture(tmp_path, [exa, serper], {"web_search": [exa.name, serper.name]}, upstream=upstream,
                           bridge=True) as (runtime, engine):
        payload, error = await call(runtime, "search", {"query": "q", "domains": ["example.org"]})
        assert not error and payload["status"] == "partial"
        assert_coverage(payload, {exa.name}, skipped={serper.name})
        assert payload["coverage"]["skipped"][0]["reason"].startswith("unsupported filter")
        assert [body["provider"] for body in sent] == ["exa-search"]
        assert sent[0]["filters"] == {"include_domains": ["example.org"]}
        # No routed source can apply recency: explicit validation error, no upstream request.
        rejected, error = await call(runtime, "search", {"query": "q", "recency": "week"})
    assert error and rejected["error"]["code"] == "INVALID_INPUT" and "recency" in rejected["error"]["message"]
    assert len(sent) == 1


@pytest.mark.parametrize("tool,args", [
    ("paper_explore", {"operation": "metadata"}),
    ("paper_explore", {"operation": "metadata", "ids": ["10.1/a"], "citation": "A paper"}),
    ("paper_explore", {"operation": "metadata", "ids": ["10.1/a"], "relation": "citing"}),
    ("paper_explore", {"operation": "related", "citation": "A paper"}),
    ("paper_explore", {"operation": "related", "ids": ["10.1/a"], "direction": "in"}),
    ("paper_explore", {"operation": "citations", "ids": ["10.1/a"], "relation": "cited"}),
    ("paper_explore", {"operation": "citations", "ids": ["10.1/a"], "depth": 3}),
    ("verify", {"operation": "citation"}),
    ("verify", {"operation": "citation", "citation": "x", "claim": "y"}),
    ("verify", {"operation": "claim", "citation": "x"}),
    ("verify", {"operation": "editorial"}),
    ("verify", {"operation": "editorial", "ids": ["10.1/a"], "citation": "x"}),
    ("code_search", {"query": "q", "scope": "repositories", "repos": ["a/b"]}),
    ("code_search", {"query": "q", "scope": "code", "min_stars": 5}),
    ("site_research", {"url": "https://example.org", "operation": "map", "max_depth": 3}),
    ("deep_research", {"operation": "literature", "question": "q", "criteria": ["x"]}),
    ("search", {"query": "q", "focus": "images"}),
    ("search", {"query": "q", "provider": "exa"}),
    ("read", {"target": "https://example.org", "source_type": "pdf"}),
])
async def test_invalid_workflow_arguments_fail_before_upstream(tmp_path: Path, tool: str, args: dict):
    caps = set(Capability)
    guard = Untouchable("guard", caps)
    routes = {cap.value: [guard.name] for cap in caps}
    async with mcp_fixture(tmp_path, [guard], routes) as (runtime, _):
        async with runtime.client() as client:
            result = await client.call_tool(tool, args, raise_on_error=False)
    # Schema violations (unknown enum or parameter) and semantic combinations both fail.
    assert result.is_error


async def test_code_search_scopes_dispatch_to_developer_and_repository_capabilities(tmp_path: Path):
    docs = Recording("firecrawl", {Capability.DEVELOPER_SEARCH}, Result(hits=[hit("firecrawl", "doc-page")]))
    github = Recording("github", {Capability.DEVELOPER_SEARCH, Capability.REPO_SEARCH},
                       Result(hits=[hit("github", "repo")]))
    routes = {"developer_search": [github.name, docs.name], "repo_search": [github.name]}
    async with mcp_fixture(tmp_path, [docs, github], routes) as (runtime, _):
        documentation, error = await call(runtime, "code_search", {"query": "httpx timeout", "scope": "docs"})
        assert not error and documentation["status"] == "partial"
        assert_coverage(documentation, {"firecrawl"}, skipped={"github"})
        for scope in ("repositories", "code", "issues"):
            payload, error = await call(runtime, "code_search", {"query": "httpx", "scope": scope})
            assert not error, payload
    assert docs.requests == [(Capability.DEVELOPER_SEARCH, docs.requests[0][1])]
    assert docs.requests[0][1]["kind"] == "docs"
    assert [(cap, req.get("mode")) for cap, req in github.requests] == [
        (Capability.REPO_SEARCH, "repos"), (Capability.REPO_SEARCH, "code"), (Capability.REPO_SEARCH, "issues")]


class ReviewService(Provider):
    name = "review-service"
    capabilities = frozenset({Capability.SYSTEMATIC_REVIEW})
    poll_interval_s = 0.05

    def __init__(self):
        self.starts: list[dict] = []

    async def start(self, cap, req, ctx):
        self.starts.append(dict(req))
        return "review-ref"

    async def poll(self, ctx, ref):
        return JobUpdate("completed", result=Result(hits=[hit(self.name, "screened-paper")]))


async def test_deep_research_starts_one_job_polled_by_owner_only(tmp_path: Path):
    service = ReviewService()
    async with mcp_fixture(tmp_path, [service], {"systematic_review": [service.name]},
                           modes={"systematic_review": "sequential"}) as (runtime, engine):
        unavailable, error = await call(runtime, "deep_research", {"operation": "literature", "question": "q"})
        assert error and unavailable["error"]["code"] == "NO_PROVIDER_AVAILABLE"
        assert "no provider is configured" in unavailable["error"]["message"].lower()
        started, error = await call(runtime, "deep_research", {
            "operation": "systematic_review", "question": "Does X reduce Y?", "criteria": ["RCTs only"]})
        assert not error and started["status"] == "running" and started["job_id"]
        done, error = await call(runtime, "get_job", {"job_id": started["job_id"], "wait_s": 5})
        assert not error and done["status"] == "completed"
        other_id, other_token = create_client_token(engine.db, "other client")
        other = Runtime(runtime.url, other_token, other_id, runtime.settings, runtime.app)
        stranger, error = await call(other, "get_job", {"job_id": started["job_id"]})
    assert error and stranger["error"]["code"] == "NOT_FOUND"
    assert service.starts == [{"question": "Does X reduce Y?", "criteria": ["RCTs only"]}]


class CrawlService(Provider):
    name = "crawler"
    capabilities = frozenset({Capability.SITE_CRAWL})

    def __init__(self):
        self.starts: list[dict] = []

    async def start(self, cap, req, ctx):
        self.starts.append(dict(req))
        return "crawl-ref"

    async def poll(self, ctx, ref):
        return JobUpdate("running", poll_after_s=60)


async def test_site_research_crawl_returns_a_job_with_bounds(tmp_path: Path):
    crawler = CrawlService()
    async with mcp_fixture(tmp_path, [crawler], {"site_crawl": [crawler.name]},
                           modes={"site_crawl": "sequential"}) as (runtime, _):
        payload, error = await call(runtime, "site_research", {
            "url": "https://example.org", "operation": "crawl", "limit": 10, "max_depth": 1,
            "include_paths": ["/docs"]})
    assert not error and payload["status"] == "running" and payload["job_id"]
    assert crawler.starts == [{"url": "https://example.org", "limit": 10, "depth": 1, "include_paths": ["/docs"]}]


async def test_paper_explore_metadata_related_and_citations_dispatch(tmp_path: Path):
    scholar = Recording("scholar", {Capability.PAPER_METADATA, Capability.PAPER_RELATED, Capability.CITATION_GRAPH},
                        Result(records=[{"requested_id": "10.1/a", "id": "10.1/a"}],
                               per_id_coverage={"10.1/a": {"found": True}},
                               hits=[hit("scholar", "neighbor", doi="10.1234/n")],
                               nodes=[{"id": "doi:10.1/a"}], edges=[]))
    routes = {"paper_metadata": [scholar.name], "paper_related": [scholar.name], "citation_graph": [scholar.name]}
    async with mcp_fixture(tmp_path, [scholar], routes, modes={"paper_metadata": "sequential"}) as (runtime, _):
        metadata, e1 = await call(runtime, "paper_explore", {"operation": "metadata", "ids": ["10.1/a"]})
        related, e2 = await call(runtime, "paper_explore", {"operation": "related", "ids": ["10.1/a", "10.1/b"],
                                                            "relation": "citing", "limit": 3})
        graph, e3 = await call(runtime, "paper_explore", {"operation": "citations", "ids": ["10.1/a"]})
    assert not (e1 or e2 or e3)
    assert metadata["per_id_coverage"]["10.1/a"]["status"] == "found"
    assert related["items"][0]["ids"]["doi"] == "10.1234/n"
    assert graph["nodes"] and graph["truncated"] is False
    assert [(cap, req.get("mode"), req.get("direction"), req.get("seeds")) for cap, req in scholar.requests] == [
        (Capability.PAPER_METADATA, None, None, None),
        (Capability.PAPER_RELATED, "citing", None, ["10.1/a", "10.1/b"]),
        (Capability.CITATION_GRAPH, None, "both", ["10.1/a"])]
