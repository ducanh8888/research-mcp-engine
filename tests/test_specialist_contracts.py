"""No-network contracts for specialist request translation and conservative graph merge."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from research_engine.merge.graph import merge_graphs
from research_engine.providers.base import Capability as C, ErrorKind, ProviderError, Result
from research_engine.providers.dev import PROVIDERS as DEV_PROVIDERS
from research_engine.providers.dev.github import GitHubProvider
from research_engine.providers.scholar.arxiv import ArxivProvider
from research_engine.providers.scholar.consensus_api import ConsensusAPIProvider
from research_engine.providers.scholar.crossref import CrossrefProvider
from research_engine.providers.scholar.elicit_api import ElicitAPIProvider
from research_engine.providers.scholar.openalex import OpenAlexProvider
from research_engine.providers.scholar.semantic_scholar import SemanticScholarProvider


@pytest.fixture
async def context():
    clients = []

    def make(provider, handler, credentials=None):
        requests = []

        def capture(request):
            requests.append(request)
            return handler(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        clients.append(client)
        ctx = SimpleNamespace(provider=provider, credentials=credentials or {"api_key": "fixture-key"},
                              options={}, account_id=1, remaining=lambda: 60, client=client, requests=requests)

        async def request(method, url, **kwargs):
            return await client.request(method, url, **kwargs)

        ctx.request = request
        return ctx

    yield make
    for client in clients:
        await client.aclose()


def response(data):
    return httpx.Response(200, json=data)


@pytest.mark.parametrize("mode,field", [("similar", "related_works"), ("cited", "referenced_works")])
async def test_openalex_related_fetches_each_seed_and_selected_neighbors(context, mode, field):
    def handler(req):
        if req.url.path.endswith(("/W1", "/W2")):
            seed = req.url.path.rsplit("/", 1)[-1]
            return response({"id": "https://openalex.org/" + seed,
                             field: ["https://openalex.org/W9" + seed[-1]]})
        neighbor = req.url.path.rsplit("/", 1)[-1]
        return response({"id": "https://openalex.org/" + neighbor, "display_name": neighbor})

    ctx = context("openalex", handler)
    result = await OpenAlexProvider().call(C.PAPER_RELATED,
        {"seeds": ["W1", "W2"], "mode": mode, "limit": 2}, ctx)
    assert [hit.ids["openalex"] for hit in result.hits] == ["W91", "W92"]
    assert [hit.raw["related_seed"] for hit in result.hits] == ["W1", "W2"]
    assert all(hit.raw["related_mode"] == mode for hit in result.hits)
    assert len(ctx.requests) == 4


async def test_openalex_citing_uses_filter_for_all_seeds(context):
    def handler(req):
        if req.url.path == "/works":
            seed = req.url.params["filter"].split(":", 1)[-1]
            return response({"results": [{"id": f"https://openalex.org/C{seed[-1]}"}]})
        seed = req.url.path.rsplit("/", 1)[-1]
        return response({"id": f"https://openalex.org/{seed}"})

    ctx = context("openalex", handler)
    result = await OpenAlexProvider().call(C.PAPER_RELATED,
        {"seeds": ["W1", "W2"], "mode": "citing", "limit": 2}, ctx)
    assert [hit.ids["openalex"] for hit in result.hits] == ["C1", "C2"]
    assert [req.url.params["filter"] for req in ctx.requests if req.url.path == "/works"] == ["cites:W1", "cites:W2"]


@pytest.mark.parametrize("mode,side", [("citing", "citations"), ("cited", "references")])
async def test_s2_related_requests_correct_side_for_each_seed(context, mode, side):
    def handler(req):
        identifier = req.url.path.split("/")[4]
        if req.url.path.endswith(side):
            key = "citingPaper" if side == "citations" else "citedPaper"
            return response({"data": [{key: {"paperId": "c" * 39 + identifier[-1], "title": "Neighbor"}}]})
        return response({"paperId": identifier, "title": "Seed"})

    seeds = ["a" * 39 + "1", "b" * 39 + "2"]
    ctx = context("semantic_scholar", handler)
    result = await SemanticScholarProvider().call(C.PAPER_RELATED,
        {"seeds": seeds, "mode": mode, "limit": 2}, ctx)
    assert [hit.ids["s2"][-1] for hit in result.hits] == ["1", "2"]
    assert sum(req.url.path.endswith(side) for req in ctx.requests) == 2


@pytest.mark.parametrize("provider,work,body", [
    (CrossrefProvider(), {"DOI": "10.1234/a", "title": ["A Precisely Matching Paper"],
                          "published": {"date-parts": [[2021]]}}, "message"),
    (OpenAlexProvider(), {"id": "https://openalex.org/W1", "doi": "10.1234/a",
                          "display_name": "A Precisely Matching Paper", "publication_year": 2021}, "results"),
    (SemanticScholarProvider(), {"paperId": "a" * 40, "title": "A Precisely Matching Paper",
                                 "year": 2021, "externalIds": {"DOI": "10.1234/a"}}, "data"),
])
async def test_citation_text_requires_title_and_year_not_search_rank(context, provider, work, body):
    def handler(req):
        if body == "message":
            return response({"message": {"items": [work]}})
        return response({body: [work]})

    ctx = context(provider.name, handler)
    citation = "Author. A Precisely Matching Paper. (2021). Journal."
    found = await provider.call(C.PAPER_METADATA, {"citation": citation}, ctx)
    assert found.records[0]["requested_id"] == citation
    assert found.records[0]["ids"]["doi"] == "10.1234/a"
    assert found.per_id_coverage[citation]["resolution"] == "title_year"
    unknown = await provider.call(C.PAPER_METADATA, {"citation": "Unrelated article (2021)"}, ctx)
    assert not unknown.records and not unknown.not_found
    assert unknown.per_id_coverage["Unrelated article (2021)"]["reason"] == "unresolved"


async def test_citation_text_ambiguity_preserves_unknown_outcome(context):
    def work(identifier):
        return {"DOI": identifier, "title": ["A Precisely Matching Paper"],
                "published": {"date-parts": [[2021]]}}
    ctx = context("crossref", lambda _: response({"message": {"items": [work("10.1234/a"), work("10.1234/b")]}}))
    citation = "A Precisely Matching Paper. 2021"
    result = await CrossrefProvider().call(C.PAPER_METADATA, {"citation": citation}, ctx)
    assert not result.records and not result.not_found
    assert result.per_id_coverage[citation]["reason"] == "ambiguous"


@pytest.mark.parametrize("provider,expected", [
    (OpenAlexProvider(), ["from_publication_date:2020-01-01", "to_publication_date:2024-12-31"]),
    (CrossrefProvider(), ["from-pub-date:2020-01-01", "until-pub-date:2024-12-31"]),
])
async def test_open_scholarly_search_translates_years(context, provider, expected):
    payload = {"message": {"items": []}} if provider.name == "crossref" else {"results": []}
    ctx = context(provider.name, lambda _: response(payload))
    await provider.call(C.PAPER_SEARCH, {"query": "biology", "year_from": 2020, "year_to": 2024}, ctx)
    assert all(part in ctx.requests[0].url.params["filter"] for part in expected)


async def test_s2_and_consensus_year_translation(context):
    s2 = context("semantic_scholar", lambda _: response({"data": []}))
    await SemanticScholarProvider().call(C.PAPER_SEARCH, {"query": "biology", "year_from": 2020,
                                                            "year_to": 2024}, s2)
    assert s2.requests[0].url.params["year"] == "2020-2024"
    consensus = context("consensus_api", lambda _: response({"results": []}))
    await ConsensusAPIProvider().call(C.PAPER_SEARCH, {"query": "biology", "year_from": 2020,
                                                        "year_to": 2024}, consensus)
    assert consensus.requests[0].url.params["year_min"] == "2020"
    assert consensus.requests[0].url.params["year_max"] == "2024"


async def test_arxiv_does_not_substitute_submission_for_publication_year(context):
    ctx = context("arxiv", lambda _: httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>'))
    with pytest.raises(ProviderError) as error:
        await ArxivProvider().call(C.PAPER_SEARCH, {"query": "biology", "year_from": 2020}, ctx)
    assert error.value.kind == ErrorKind.BAD_REQUEST and not ctx.requests


async def test_elicit_rejects_unverified_year_filter_before_http(context):
    ctx = context("elicit_api", lambda _: response({"papers": []}))
    with pytest.raises(ProviderError) as error:
        await ElicitAPIProvider().call(C.PAPER_SEARCH, {"query": "biology", "year_from": 2020}, ctx)
    assert error.value.kind == ErrorKind.BAD_REQUEST and not ctx.requests


async def test_s2_public_claim_returns_attributed_contexts(context):
    identifier = "a" * 40
    def handler(req):
        if req.url.path.endswith("/citations"):
            return response({"data": [{"citingPaper": {"paperId": "b" * 40},
                                       "contexts": ["An actual citing passage"]}]})
        return response({"paperId": identifier, "title": "A Precisely Matching Paper"})
    ctx = context("semantic_scholar", handler)
    result = await SemanticScholarProvider().call(C.CITATION_VERIFY,
        {"citation": identifier, "claim": "A testable claim"}, ctx)
    assert result.claim_evidence[0]["passage"] == "An actual citing passage"
    assert result.claim_evidence[0]["source_id"] == "b" * 40


def test_graph_merge_remaps_overlapping_doi_edges_and_retains_assertions():
    first = Result(nodes=[{"id": "openalex:W1", "ids": {"doi": "10.1234/a"}, "providers": ["openalex"]},
                          {"id": "openalex:W2", "ids": {"doi": "10.1234/b"}, "providers": ["openalex"]}],
                   edges=[{"source": "openalex:W1", "target": "openalex:W2", "provider": "openalex",
                           "relation": "cites"}])
    second = Result(nodes=[{"id": "s2:" + "a" * 40, "ids": {"doi": "10.1234/a"},
                            "providers": ["semantic_scholar"]},
                           {"id": "s2:" + "b" * 40, "ids": {"doi": "10.1234/b"},
                            "providers": ["semantic_scholar"]}],
                    edges=[{"source": "s2:" + "a" * 40, "target": "s2:" + "b" * 40,
                            "provider": "semantic_scholar", "relation": "cites", "intents": ["background"]}])
    merged = merge_graphs([first, second])
    assert len(merged["nodes"]) == 2 and len(merged["edges"]) == 1
    assert merged["edges"][0]["source"] == "doi:10.1234/a"
    assert merged["edges"][0]["target"] == "doi:10.1234/b"
    assert len(merged["edges"][0]["sources"]) == 2
    assert merged["edges"][0]["sources"][1]["assertion"]["intents"] == ["background"]
    assert len(merged["nodes"][0]["sources"]) == 2


def test_graph_merge_keeps_conflicting_identifiers_distinct_and_bounds_edges():
    nodes = [
        {"id": f"openalex:W{i}", "ids": {"openalex": f"W{i}", "doi": f"10.1234/{i}"},
         "providers": ["openalex"]} for i in range(1, 4)
    ]
    edges = [{"source": nodes[0]["id"], "target": node["id"], "provider": "openalex"}
             for node in nodes[1:]]
    merged = merge_graphs([Result(nodes=nodes, edges=edges)], max_nodes=2, max_edges=1)
    assert len(merged["nodes"]) == 2 and len(merged["edges"]) == 1 and merged["truncated"]
    assert {edge["source"] for edge in merged["edges"]} <= {node["id"] for node in merged["nodes"]}
    assert {edge["target"] for edge in merged["edges"]} <= {node["id"] for node in merged["nodes"]}
    conflicting = merge_graphs([Result(nodes=[
        {"id": "openalex:W1", "ids": {"doi": "10.1234/a"}, "providers": ["openalex"]},
        {"id": "s2:" + "a" * 40, "ids": {"doi": "10.1234/b", "openalex": "W1"},
         "providers": ["semantic_scholar"]}])])
    assert len(conflicting["nodes"]) == 2
    assert {node["ids"]["doi"] for node in conflicting["nodes"]} == {"10.1234/a", "10.1234/b"}


def test_github_is_in_specialist_catalog():
    assert any(entry.name == "github" for entry in DEV_PROVIDERS)


async def test_github_repo_and_issue_search_have_explicit_modes_and_quota(context):
    def handler(req):
        mode = req.url.path.rsplit("/", 1)[-1]
        if mode == "repositories":
            return httpx.Response(200, json={"items": [{"full_name": "org/repo",
                "html_url": "https://github.com/org/repo", "stargazers_count": 80}]},
                headers={"x-ratelimit-remaining": "20", "x-ratelimit-resource": "search"})
        return response({"items": [{"html_url": "https://github.com/org/repo/issues/1",
                                    "title": "Bug report", "body": "Reproduction", "number": 1}]})

    ctx = context("github", handler)
    repos = await GitHubProvider().call(C.REPO_SEARCH,
        {"query": "vectors", "language": "python", "min_stars": 20}, ctx)
    assert ctx.requests[0].url.params["q"] == "vectors language:python stars:>=20"
    assert repos.hits[0].raw["stars"] == 80
    assert repos.quota_remaining == 20
    issues = await GitHubProvider().call(C.DEVELOPER_SEARCH,
        {"query": "regression", "repos": ["org/repo"]}, ctx)
    assert ctx.requests[1].url.path == "/search/issues"
    assert ctx.requests[1].url.params["q"] == "regression repo:org/repo"
    assert issues.hits[0].raw["number"] == 1


async def test_github_rejects_unsafe_repository_qualifier_before_upstream(context):
    ctx = context("github", lambda _: response({"items": []}))
    with pytest.raises(ProviderError) as error:
        await GitHubProvider().call(C.DEVELOPER_SEARCH,
            {"query": "test", "repos": ["org/repo qualifier"]}, ctx)
    assert error.value.kind == ErrorKind.BAD_REQUEST and not ctx.requests
