"""Real SQLite/HTTP-MCP regression cases for router/server contracts (no live providers)."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from research_engine.providers.base import Capability, ErrorKind, Provider, ProviderError
from research_engine.providers.scholar.semantic_scholar import SemanticScholarProvider
from research_engine.providers.web.firecrawl import FirecrawlProvider
from research_engine.router.execute import Engine, ToolError
from research_engine.router.requests import for_provider, validate
from research_engine.server.app import create_app
from research_engine.server.schemas import Document, Hit, Result
from research_engine.storage.db import Account, Attempt, Database, ProviderRow, RequestRow, Routing, create_client_token
from test_mcp_e2e import Runtime, make_settings, run_http_app


class Stub(Provider):
    keyless = True

    def __init__(self, name, capabilities, result=None, *, error=None, delay=0):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.result = result
        self.error = error
        self.delay = delay
        self.calls = []

    async def call(self, cap, req, ctx):
        self.calls.append((cap, req, ctx.account_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def engine_with(tmp_path: Path, providers, cap: Capability, *, accounts=1, settings=None) -> Engine:
    settings = settings or make_settings(tmp_path / "runtime", 8765)
    registry = {provider.name: provider for provider in providers}
    db = Database(settings.database_path)
    db.initialize(registry)
    with db.session() as session:
        for provider in providers:
            for _ in range(accounts):
                session.add(Account(provider=provider.name, label="fixture", credential="ok"))
        session.merge(Routing(capability=cap.value, mode="fanout" if cap in {
            Capability.CITATION_VERIFY, Capability.PAPER_SEARCH, Capability.WEB_SEARCH} else "sequential",
            providers=[provider.name for provider in providers]))
    db.close()
    return Engine(settings, providers=registry)


async def test_changed_route_identity_bypasses_stale_query_without_deleting_history(tmp_path):
    first = Stub("old_search", {Capability.WEB_SEARCH}, Result(hits=[Hit(
        provider="old_search", title="Old", url="https://example.org/old")]))
    second = Stub("new_search", {Capability.WEB_SEARCH}, Result(hits=[Hit(
        provider="new_search", title="New", url="https://example.org/new")]))
    engine = engine_with(tmp_path, [first, second], Capability.WEB_SEARCH)
    try:
        with engine.db.session() as session:
            session.get(Routing, "web_search").providers = [first.name]
        initial = await engine.execute("web_search", {"query": "same"})
        assert initial["items"][0]["title"] == "Old"
        with engine.db.session() as session:
            session.get(Routing, "web_search").providers = [second.name]
        updated = await engine.execute("web_search", {"query": "same"})
        assert updated["items"][0]["title"] == "New"
        assert len(first.calls) == len(second.calls) == 1
        with engine.db.session() as session:
            session.get(ProviderRow, second.name).enabled = False
        with pytest.raises(ToolError):
            await engine.execute("web_search", {"query": "same"})
    finally:
        await engine.stop()


def test_direct_reader_receives_public_url_and_claim_keeps_independent_sources():
    source = {"target": "https://example.org/source", "fresh": True}
    assert for_provider(Capability.WEB_READ, source, "trafilatura")["url"] == source["target"]
    assert for_provider(Capability.WEB_READ, source, "omni:jina-reader")["target"] == source["target"]
    for name in ("crossref", "openalex", "scite_rest"):
        request = for_provider(Capability.CITATION_VERIFY, {"citation": "10.1234/a", "claim": "statement"}, name)
        assert request["citation"] == "10.1234/a"
        assert "statement" not in request
    assert for_provider(Capability.CITATION_VERIFY,
                        {"citation": "10.1234/a", "claim": "statement"},
                        "semantic_scholar")["statement"] == "statement"


def test_year_filters_reach_supported_specialist_adapters_without_double_translation():
    args = {"query": "evidence", "year_from": 2020, "year_to": 2024}
    for name in ("consensus_api", "scite_mcp", "elicit_mcp"):
        request = for_provider(Capability.PAPER_SEARCH, args, name)
        assert request["year_from"] == 2020 and request["year_to"] == 2024
        assert "filters" not in request


async def test_misattributed_search_hit_does_not_create_uninvoked_rrf_vote(tmp_path):
    fake = Stub("called", {Capability.WEB_SEARCH}, Result(hits=[Hit(
        provider="uncalled", title="False provenance", url="https://example.org/false")]))
    engine = engine_with(tmp_path, [fake], Capability.WEB_SEARCH)
    try:
        with pytest.raises(ToolError) as failure:
            await engine.execute("web_search", {"query": "fixture", "fresh": True})
        assert failure.value.code == "NO_PROVIDER_AVAILABLE"
        assert failure.value.coverage["failed"][0]["p"] == "called"
    finally:
        await engine.stop()


async def test_empty_read_fallback_and_valid_empty_search(tmp_path):
    first = Stub("empty_read", {Capability.WEB_READ}, Result())
    second = Stub("good_read", {Capability.WEB_READ}, Result(document=Document(
        url="https://example.org/a", text="Source text", source="good_read")))
    engine = engine_with(tmp_path, [first, second], Capability.WEB_READ)
    try:
        output = await engine.execute("web_read", {"target": "https://example.org/a", "fresh": True})
        assert output["coverage"]["ok"] == ["good_read"]
        assert output["coverage"]["failed"][0]["p"] == "empty_read"
        assert output["document"]["text"] == "Source text"
        assert len(first.calls) == len(second.calls) == 1
    finally:
        await engine.stop()

    search = Stub("empty_search", {Capability.WEB_SEARCH}, Result(hits=[]))
    engine = engine_with(tmp_path / "search", [search], Capability.WEB_SEARCH)
    try:
        output = await engine.execute("web_search", {"query": "nothing", "fresh": True})
        assert output["status"] == "complete" and output["items"] == []
        assert output["coverage"]["ok"] == ["empty_search"]
    finally:
        await engine.stop()


@pytest.mark.parametrize("kind", [ErrorKind.TARGET, ErrorKind.BAD_REQUEST])
async def test_account_independent_failure_not_retried(tmp_path, kind):
    provider = Stub("bad", {Capability.WEB_SEARCH}, error=ProviderError(kind, "same target"))
    engine = engine_with(tmp_path, [provider], Capability.WEB_SEARCH, accounts=3)
    try:
        with pytest.raises(ToolError) as failure:
            await engine.execute("web_search", {"query": "example", "fresh": True})
        assert failure.value.code == "NO_PROVIDER_AVAILABLE"
        assert len(provider.calls) == 1
        with engine.db.session() as session:
            attempts = list(session.scalars(select(Attempt)))
            assert len(attempts) == 1 and attempts[0].kind == kind
    finally:
        await engine.stop()


async def test_citation_only_metadata_advances_past_unresolved_source(tmp_path):
    citation = "Author. Exact Article Title. (2021). Journal."
    unresolved = Stub("unresolved", {Capability.PAPER_METADATA}, Result(
        per_id_coverage={citation: {"found": False, "reason": "unsupported_citation_text"}}))
    resolved = Stub("resolved", {Capability.PAPER_METADATA}, Result(
        records=[{"requested_id": citation, "id": "doi:10.1234/article", "provider": "resolved"}],
        per_id_coverage={citation: {"found": True, "provider": "resolved"}}))
    engine = engine_with(tmp_path, [unresolved, resolved], Capability.PAPER_METADATA)
    try:
        data = await engine.execute("paper_metadata", {"citation": citation, "fresh": True})
        assert data["coverage"]["ok"] == ["unresolved", "resolved"]
        assert data["per_id_coverage"][citation]["status"] == "found"
        assert data["records"][0]["requested_id"] == citation
        assert data["not_found"] == []
        assert len(unresolved.calls) == len(resolved.calls) == 1
        assert "ids" not in unresolved.calls[0][1]
    finally:
        await engine.stop()


async def test_metadata_preserves_both_providers_and_unknown_after_timeout(tmp_path):
    a = Stub("metadata_a", {Capability.PAPER_METADATA}, Result(
        records=[{"requested_id": "A", "id": "A", "provider": "metadata_a"}],
        not_found=["B"], per_id_coverage={"A": {"found": True, "providers": ["metadata_a"]},
                                            "B": {"found": False, "providers": ["metadata_a"]}}))
    b = Stub("metadata_b", {Capability.PAPER_METADATA}, Result(
        records=[{"requested_id": "B", "id": "B", "provider": "metadata_b"}],
        per_id_coverage={"B": {"found": True, "providers": ["metadata_b"]}}))
    engine = engine_with(tmp_path, [a, b], Capability.PAPER_METADATA)
    try:
        output = await engine.execute("paper_metadata", {"ids": ["A", "B"], "fresh": True})
        assert output["coverage"]["ok"] == ["metadata_a", "metadata_b"]
        assert [r["requested_id"] for r in output["records"]] == ["A", "B"]
        assert output["per_id_coverage"]["B"]["sources"] == [
            {"found": False, "providers": ["metadata_a"]},
            {"found": True, "providers": ["metadata_b"]}]
        assert output["not_found"] == []
    finally:
        await engine.stop()

    b = Stub("slow_metadata", {Capability.PAPER_METADATA}, delay=10)
    engine = engine_with(tmp_path / "slow", [a, b], Capability.PAPER_METADATA)
    try:
        output = await engine.execute("paper_metadata", {"ids": ["A", "B"], "fresh": True,
                                                          "deadline_s": 0.01})
        assert output["status"] == "partial"
        assert output["not_found"] == []
        assert output["per_id_coverage"]["B"]["status"] == "unknown"
        assert output["coverage"]["ok"] == ["metadata_a"]
    finally:
        await engine.stop()


@pytest.mark.parametrize("verdicts,expected", [
    (("match", "mismatch"), "conflict"),
    (("unknown", "match"), "unknown"),
    (("unknown", "unknown"), "unknown"),
])
async def test_verification_conflict_preserves_assertions(tmp_path, verdicts, expected):
    providers = [Stub(f"verify_{i}", {Capability.CITATION_VERIFY}, Result(
        verification={"bibliographic": verdict, "sources": [{"provider": f"verify_{i}", "verdict": verdict}]},
        claim_evidence=[{"provider": f"verify_{i}", "passage": "context"}]))
        for i, verdict in enumerate(verdicts)]
    engine = engine_with(tmp_path, providers, Capability.CITATION_VERIFY)
    try:
        result = await engine.execute("citation_verify", {"citation": "10.1234/a", "fresh": True})
        assert result["bibliographic"] == expected
        assert len(result["sources"]) == len(result["claim_evidence"]) == 2
        assert {source["verdict"] for source in result["sources"]} == set(verdicts)
    finally:
        await engine.stop()


async def test_fault_and_cancellation_finalize_correlated_requests(tmp_path):
    search = Stub("search", {Capability.WEB_SEARCH}, Result(hits=[Hit(
        provider="search", title="Found", url="https://example.org")]))
    engine = engine_with(tmp_path, [search], Capability.WEB_SEARCH)
    async def broken(*args, **kwargs):
        raise RuntimeError("never expose this secret")
    engine._payload = broken
    try:
        with pytest.raises(ToolError) as failure:
            await engine.execute("web_search", {"query": "item", "fresh": True})
        assert failure.value.code == "INTERNAL"
        assert "never expose" not in str(failure.value)
        with engine.db.session() as session:
            row = session.get(RequestRow, failure.value.request_id)
            assert row.status == "error" and row.finished_at is not None
            assert row.result["request_id"] == row.id
            assert "never expose" not in str(row.result)

        search.delay = 30
        task = asyncio.create_task(engine.execute("web_search", {"query": "another", "fresh": True}))
        for _ in range(100):
            if search.calls[-1][1]["query"] == "another":
                break
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with engine.db.session() as session:
            rows = list(session.scalars(select(RequestRow).where(RequestRow.args["query"].as_string() == "another")))
            assert len(rows) == 1 and rows[0].status == "error" and rows[0].finished_at is not None
    finally:
        await engine.stop()


async def test_whole_request_timeout_covers_slow_cache_step(tmp_path):
    provider = Stub("cache_timeout", {Capability.WEB_SEARCH}, Result(hits=[]))
    engine = engine_with(tmp_path, [provider], Capability.WEB_SEARCH)
    original = engine.cache.get_query

    def slow_cache(*args, **kwargs):
        import time
        time.sleep(5.5)
        return original(*args, **kwargs)

    engine.cache.get_query = slow_cache
    try:
        with pytest.raises(ToolError) as failure:
            await engine.execute("web_search", {"query": "delayed-cache", "deadline_s": .01})
        assert failure.value.code == "NO_PROVIDER_AVAILABLE"
        with engine.db.session() as session:
            row = session.get(RequestRow, failure.value.request_id)
            assert row.status == "error" and row.finished_at is not None
        assert not provider.calls
    finally:
        await engine.stop()


async def test_response_bounds_and_deadline_clamp(tmp_path):
    provider = Stub("huge", {Capability.PAPER_METADATA}, Result(
        records=[{"requested_id": "A", "provider": "huge", "raw": "x" * 4000}],
        per_id_coverage={"A": {"found": True, "providers": ["huge"]}}))
    settings = make_settings(tmp_path / "runtime", 8765)
    settings.max_response_bytes = 1024
    engine = engine_with(tmp_path, [provider], Capability.PAPER_METADATA, settings=settings)
    try:
        with pytest.raises(ToolError) as failure:
            await engine.execute("paper_metadata", {"ids": ["A"], "fresh": True})
        assert failure.value.request_id
        assert "size limit" in str(failure.value)
        with engine.db.session() as session:
            assert session.get(RequestRow, failure.value.request_id).status == "error"
        assert validate(Capability.PAPER_METADATA, {"ids": ["A"], "deadline_s": 100})["deadline_s"] == 50
        assert validate(Capability.PAPER_METADATA, {"ids": ["A"], "deadline_s": .01})["deadline_s"] == 5
    finally:
        await engine.stop()


async def test_public_tool_crawl_depth_and_year_filters_and_claim(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "runtime", sock.getsockname()[1])
    sent = []
    async def handler(request):
        sent.append((request.url.path, request.url.params, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/crawl"):
            return httpx.Response(200, json={"success": True, "id": "recoverable"})
        if request.url.path.endswith("/paper/search"):
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/paper/batch"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/citations"):
            return httpx.Response(200, json={"data": [{"citingPaper": {"paperId": "b" * 40},
                                                       "contexts": ["Source claim context"]}]})
        if "/paper/" in request.url.path:
            return httpx.Response(200, json={"paperId": "a" * 40, "title": "Fixture", "url": "https://example.org/paper"})
        return httpx.Response(404)

    firecrawl = FirecrawlProvider()
    scholar = SemanticScholarProvider()
    registry = {p.name: p for p in [firecrawl, scholar]}
    db = Database(settings.database_path)
    db.initialize(registry)
    with db.session() as session:
        for provider in registry:
            session.add(Account(provider=provider, label="fixture", credential="ok"))
        for cap, provider in [(Capability.SITE_CRAWL, firecrawl.name),
                              (Capability.PAPER_SEARCH, scholar.name),
                              (Capability.CITATION_VERIFY, scholar.name)]:
            session.merge(Routing(capability=cap.value, mode="sequential", providers=[provider]))
    token_id, token = create_client_token(db, "fixture")
    from research_engine.storage.crypto import Cipher, SecretStore
    with db.session() as session:
        firecrawl_account = next(row.id for row in session.scalars(select(Account)) if row.provider == firecrawl.name)
    SecretStore(db, Cipher.from_file(settings.encryption_key_file, db)).set(
        firecrawl_account, {"api_key": "test-only-key"})
    db.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        engine = Engine(settings, providers=registry, http_client=http)
        async def safe_url(target):
            return target
        # DNS-free controlled fixture; only the outbound request path is mocked.
        from unittest.mock import patch
        with patch("research_engine.providers.url_guard.validate_url", safe_url):
            app = create_app(settings, engine=engine)
            runtime = Runtime(f"http://127.0.0.1:{settings.port}", token, token_id, settings, app)
            with run_http_app(app, sock):
                async with runtime.client() as client:
                    crawl = await client.call_tool("site_crawl", {"url": "https://example.org", "max_depth": 5})
                    assert not crawl.is_error, crawl.content
                    assert next(body for path, _, body in sent if path.endswith("/crawl"))["maxDiscoveryDepth"] == 5
                    year = await client.call_tool("paper_search", {"query": "test", "year_from": 2000,
                                                                     "year_to": 2020, "fresh": True})
                    assert not year.is_error, year.content
                    assert next(params for path, params, _ in sent if path.endswith("/paper/search"))["year"] == "2000-2020"
                    verification = await client.call_tool("citation_verify", {"citation": "DOI:10.1234/a",
                                                                                 "claim": "a statement", "fresh": True})
                    assert not verification.is_error, verification.content
                    assert json.loads(verification.content[0].text)["claim_evidence"][0]["passage"] == "Source claim context"
                    before = len(sent)
                    for tool, args in [("site_crawl", {"url": ""}),
                                       ("site_crawl", {"url": "https://example.org", "max_depth": 99}),
                                       ("paper_search", {"query": "test", "year_from": 2020, "year_to": 2000}),
                                       ("citation_graph", {"seeds": ["id"], "direction": "sideways"})]:
                        invalid = await client.call_tool(tool, args, raise_on_error=False)
                        assert invalid.is_error
                        assert len(sent) == before
