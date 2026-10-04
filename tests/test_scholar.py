"""Provider fixtures validate v3 truthfulness contracts, independently of live keys."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from research_engine.providers.base import Capability as C, ErrorKind, ProviderError
from research_engine.providers.scholar.arxiv import ArxivProvider, SPACING
from research_engine.providers.scholar.common import bibliographic, doi, json_request
from research_engine.providers.scholar.crossref import CrossrefProvider, normalize as crossref_hit
from research_engine.providers.scholar.openalex import OpenAlexProvider, inverted_abstract
from research_engine.providers.scholar.scite import SciteRestProvider
from research_engine.providers.scholar.semantic_scholar import SemanticScholarProvider

FOUND = "10.1234/found"
MISSING = "10.1234/missing"


@pytest.fixture
async def context():
    clients = []

    def factory(handler, provider="test", credentials=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        ctx = SimpleNamespace(provider=provider, credentials=credentials or {}, options={}, account_id=1,
                              remaining=lambda: 60, client=client)

        async def request(method, url, **kwargs):
            return await client.request(method, url, **kwargs)

        async def validate_url(url):
            if "127.0.0.1" in url:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Private target blocked")

        ctx.request, ctx.validate_url = request, validate_url
        return ctx

    yield factory
    for client in clients:
        await client.aclose()


def response(data, status=200, **kwargs):
    return httpx.Response(status, json=data, **kwargs)


@pytest.mark.parametrize("provider,work", [
    (OpenAlexProvider(), {"id": "https://openalex.org/W1", "doi": FOUND, "title": "Found paper"}),
    (CrossrefProvider(), {"DOI": FOUND, "title": ["Found paper"]}),
    (SciteRestProvider(), {"doi": FOUND, "title": "Found paper"}),
])
async def test_metadata_returns_explicit_per_id_misses(context, provider, work):
    def handler(req):
        if provider.name == "openalex" and req.url.path == "/works":
            assert req.url.params["filter"] == "doi:" + FOUND + "|" + MISSING
            return response({"results": [work]})
        if MISSING in str(req.url):
            return response({}, 404)
        return response({"message": work} if provider.name == "crossref" else work)
    result = await provider.call(C.PAPER_METADATA, {"ids": [FOUND, MISSING, "unsupported:123"]},
                                 context(handler, provider.name))
    assert [item["requested_id"] for item in result.records] == [FOUND]
    assert result.not_found == [MISSING, "unsupported:123"]
    assert result.per_id_coverage[FOUND]["found"] is True
    assert result.per_id_coverage[MISSING]["found"] is False
    assert result.records[0]["providers"] == [provider.name]


def test_crossref_normalization_strips_markup_and_retains_bibliography():
    hit = crossref_hit({"DOI": FOUND.upper(), "title": ["An &amp; explicit title"],
        "abstract": "<jats:p>This <jats:italic>abstract</jats:italic> is deposited.</jats:p>",
        "author": [{"given": "Jane", "family": "Doe"}], "published": {"date-parts": [[2024, 1, 1]]}})
    assert hit.ids["doi"] == FOUND
    assert hit.title == "An & explicit title"
    assert hit.snippet == "This abstract is deposited."
    assert hit.authors == ["Jane Doe"] and hit.year == 2024


def test_doi_keeps_balanced_parentheses_and_removes_citation_punctuation():
    assert doi("https://doi.org/10.1234/(Work).") == "10.1234/(work)"
    assert doi("(doi:10.1234/work)") == "10.1234/work"


def test_inverted_abstract_uses_original_positions():
    assert inverted_abstract({"second": [1], "first": [0], "third": [2]}) == "first second third"


def test_bibliographic_verification_never_uses_rank_or_citation_count():
    hit = crossref_hit({"DOI": FOUND, "title": ["The correct title"], "score": 100,
                       "is-referenced-by-count": 10000})
    assert bibliographic({"citation": FOUND}, hit, "crossref").verification["bibliographic"] == "match"
    assert bibliographic({"citation": {"doi": FOUND, "title": "The wrong title"}}, hit,
                         "crossref").verification["bibliographic"] == "mismatch"
    assert bibliographic({"citation": "The wrong title " + FOUND}, hit,
                         "crossref").verification["bibliographic"] == "unknown"
    assert bibliographic({"citation": FOUND}, None, "crossref").verification["bibliographic"] == "unknown"


async def test_scite_tallies_are_not_claim_evidence(context):
    def handler(req):
        return response({"doi": FOUND, "title": "Paper"} if "/papers/" in str(req.url) else
                        {"doi": FOUND, "total": 42, "supporting": 20, "contradicting": 2})
    result = await SciteRestProvider().call(C.CITATION_VERIFY,
        {"citation": FOUND, "statement": "An unverified statement"}, context(handler, "scite_rest"))
    assert result.verification["bibliographic"] == "match"
    assert not result.claim_evidence
    assert result.citation_tallies[0]["supporting"] == 20
    assert result.citation_tallies[0]["scope"] == "all_citations_to_paper"


@pytest.mark.parametrize("provider,work", [
    (OpenAlexProvider(), {"id": "https://openalex.org/W1", "doi": FOUND, "is_retracted": False}),
    (CrossrefProvider(), {"DOI": FOUND, "title": ["Paper"], "updated-by": []}),
    (SciteRestProvider(), {"doi": FOUND, "title": "Paper", "retracted": False, "editorialNotices": []}),
])
async def test_missing_editorial_notice_is_unknown(context, provider, work):
    ctx = context(lambda _: response({"message": work} if provider.name == "crossref" else work), provider.name)
    result = await provider.call(C.EDITORIAL_CHECK, {"ids": [FOUND]}, ctx)
    assert result.checks[0]["status"] == "unknown"
    assert result.checks[0]["notices"] == []


async def test_crossref_notice_retains_explicit_source(context):
    work = {"DOI": FOUND, "updated-by": [{"type": "retraction", "DOI": "10.1234/notice",
            "source": "retraction-watch", "updated": {"date-parts": [[2025, 1, 1]]}}]}
    result = await CrossrefProvider().call(C.EDITORIAL_CHECK, {"ids": [FOUND]},
        context(lambda _: response({"message": work}), "crossref"))
    assert result.checks[0]["status"] == "retracted"
    assert result.checks[0]["notices"][0]["source"] == "retraction-watch"


async def test_scite_notice_retains_notice_doi(context):
    work = {"doi": FOUND, "retracted": True, "editorialNotices": [
        {"type": "retraction", "noticeDoi": "10.1234/notice", "date": "2025-01-01"}]}
    result = await SciteRestProvider().call(C.EDITORIAL_CHECK, {"ids": [FOUND]},
        context(lambda _: response(work), "scite_rest"))
    assert result.checks[0]["status"] == "retracted"
    assert result.checks[0]["notices"][0]["notice_doi"] == "10.1234/notice"


async def test_openalex_graph_edges_point_from_citing_to_cited(context):
    def handler(req):
        if req.url.path == "/works/W1":
            return response({"id": "https://openalex.org/W1", "referenced_works": ["https://openalex.org/W2"]})
        assert req.url.params["filter"] == "cites:W1"
        return response({"results": [{"id": "https://openalex.org/W3"}], "meta": {"count": 1}})
    result = await OpenAlexProvider().call(C.CITATION_GRAPH, {"seeds": ["W1"], "direction": "both"},
                                          context(handler, "openalex"))
    assert {(edge["source"], edge["target"]) for edge in result.edges} == {
        ("openalex:W1", "openalex:W2"), ("openalex:W3", "openalex:W1")}
    assert not result.truncated


async def test_openalex_graph_sets_truncation_when_budget_excludes_references(context):
    work = {"id": "https://openalex.org/W1", "referenced_works": ["https://openalex.org/W2", "https://openalex.org/W3"]}
    result = await OpenAlexProvider().call(C.CITATION_GRAPH,
        {"seeds": ["W1"], "direction": "out", "max_edges": 1}, context(lambda _: response(work), "openalex"))
    assert len(result.edges) == 1 and result.truncated


async def test_s2_batch_nulls_and_unknown_arxiv_versions_remain_unresolved(context):
    def handler(req):
        assert json.loads(req.content)["ids"] == ["DOI:" + FOUND, "pmid:999"]
        return response([{"paperId": "a" * 40, "title": "Found paper", "externalIds": {"DOI": FOUND}}, None])
    result = await SemanticScholarProvider().call(C.PAPER_METADATA,
        {"ids": [FOUND, "pmid:999", "arxiv:1706.03762v2"]}, context(handler, "semantic_scholar"))
    assert result.not_found == ["pmid:999", "arxiv:1706.03762v2"]
    assert result.records[0]["ids"]["doi"] == FOUND


async def test_s2_graph_retains_direction_and_provider_intents(context):
    paper_id = "a" * 40
    def handler(req):
        if req.url.path.endswith("/references"):
            return response({"data": [{"citedPaper": {"paperId": "b" * 40}, "intents": ["method"]}], "next": 12})
        if req.url.path.endswith("/citations"):
            return response({"data": [{"citingPaper": {"paperId": "c" * 40}, "intents": ["background"]}]})
        return response({"paperId": paper_id, "title": "Seed"})
    result = await SemanticScholarProvider().call(C.CITATION_GRAPH,
        {"seeds": [paper_id], "direction": "both", "include_intent": True}, context(handler, "semantic_scholar"))
    assert result.edges[0]["source"] == "s2:" + paper_id
    assert result.edges[0]["target"] == "s2:" + "b" * 40
    assert result.edges[1]["source"] == "s2:" + "c" * 40
    assert result.edges[1]["target"] == "s2:" + paper_id
    assert result.edges[0]["intents"] == ["method"] and result.truncated


ARXIV_FEED = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
 <entry><id>http://arxiv.org/abs/1706.03762v2</id><title>Attention Is All You Need</title>
 <summary>Abstract text</summary><published>2017-06-12T00:00:00Z</published>
 <author><name>A. Author</name></author><arxiv:doi>10.1234/published</arxiv:doi>
 <link href="http://arxiv.org/pdf/1706.03762v2" title="pdf" type="application/pdf"/>
 </entry></feed>'''


async def test_arxiv_exact_versions_and_publication_relations(context, monkeypatch):
    monkeypatch.setattr(SPACING, "interval", 0)
    monkeypatch.setattr(SPACING, "last", 0)
    result = await ArxivProvider().call(C.PAPER_METADATA,
        {"ids": ["1706.03762", "1706.03762v2", "1706.03762v3"]},
        context(lambda _: httpx.Response(200, text=ARXIV_FEED), "arxiv"))
    assert result.not_found == ["1706.03762v3"]
    ids = result.records[0]["ids"]
    assert ids == {"arxiv": "1706.03762v2"} and "doi" not in ids
    assert result.records[0]["metadata"]["raw"]["publication_doi"] == "10.1234/published"


@pytest.mark.parametrize("status,body,kind", [
    (401, {}, ErrorKind.AUTH), (403, {"error": "upgrade required"}, ErrorKind.PLAN),
    (402, {}, ErrorKind.EXHAUSTED), (429, {"error": "too many requests"}, ErrorKind.RATE_LIMITED),
    (429, {"error": "insufficient_quota"}, ErrorKind.EXHAUSTED),
    (500, {}, ErrorKind.TRANSIENT), (400, {}, ErrorKind.BAD_REQUEST), (404, {}, ErrorKind.TARGET),
])
async def test_json_errors_are_classified_for_routing(context, status, body, kind):
    ctx = context(lambda _: response(body, status, headers={"Retry-After": "7"}))
    with pytest.raises(ProviderError) as caught:
        await json_request(ctx, "GET", "https://api.example.org/test")
    assert caught.value.kind == kind
    assert caught.value.retry_after == 7


async def test_json_404_is_explicit_not_found_when_requested(context):
    assert await json_request(context(lambda _: response({}, 404)), "GET", "https://api.example.org/test",
                              not_found=True) is None
