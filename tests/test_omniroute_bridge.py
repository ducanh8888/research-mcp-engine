"""Explicit OmniRoute search/fetch/rerank contracts with an in-memory HTTP transport."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from research_engine.providers.base import Capability, CallContext, ErrorKind
from research_engine.providers.omniroute import OmniRouteBridge, OmniRouteProvider, PROVIDERS
from research_engine.providers.omniroute.bridge import BridgeError


class Context(CallContext):
    def __init__(self, client: httpx.AsyncClient, *, key: str = "service-only",
                 base_url: str = "https://gateway.example/v1"):
        super().__init__(client, {"api_key": key}, 12, "omni:brave-search", time.monotonic() + 30,
                         {"base_url": base_url})
        self.targets: list[str] = []

    async def validate_url(self, target: str):
        self.targets.append(target)
        if "127.0.0.1" in target:
            raise BridgeError(ErrorKind.TARGET, "Disallowed target")


SEARCH = {"provider": "brave-search", "results": [{"title": "Source", "url": "https://example.org/article",
    "snippet": "Source evidence", "position": 1,
    "citation": {"provider": "brave-search", "rank": 1, "retrieved_at": "2026-10-05T00:00:00Z"}}],
    "errors": [], "cached": False}


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def search_request(request):
    assert request.method == "POST" and request.url.path == "/v1/search"
    assert request.headers["Authorization"] == "Bearer service-only"
    assert "user-bearer" not in str(dict(request.headers))
    assert "service-only" not in request.content.decode()
    return json.loads(request.content)


async def test_fixed_virtual_providers_and_connection_holder():
    assert {provider.name for provider in PROVIDERS} == {
        "omniroute", "omni:brave-search", "omni:serper-search", "omni:jina-reader",
        "omni:duckduckgo-free", "omni:exa-search", "omni:firecrawl", "omni:tavily-search",
        "omni:ollama-search", "omni:jina-search", "omni:linkup-search",
        "omni:anysearch-search", "omni:nimble-search",
    }
    assert not PROVIDERS[0].capabilities and not PROVIDERS[0].keyless
    assert all(provider.keyless and provider.bridge_connection == "omniroute" for provider in PROVIDERS[1:])
    assert [p.provider_id for p in PROVIDERS[1:]] == [
        "brave-search", "serper-search", "jina-reader", "duckduckgo-free", "exa-search",
        "firecrawl", "tavily-search", "ollama-search", "jina-search", "linkup-search",
        "anysearch-search", "nimble-search",
    ]


@pytest.mark.parametrize("cap,search_type", [
    (Capability.WEB_SEARCH, "web"), (Capability.NEWS_SEARCH, "news")
])
async def test_search_explicit_provider_and_source_provenance(cap, search_type):
    def handler(request):
        body = search_request(request)
        assert body == {"query": "paper metadata", "provider": "brave-search", "max_results": 2,
                        "search_type": search_type, "strict_filters": True}
        return httpx.Response(200, json=SEARCH)

    async with client_for(handler) as client:
        result = await OmniRouteProvider("omni:brave-search", "brave-search", {cap}).call(
            cap, {"query": "paper metadata", "limit": 2}, Context(client))
    assert len(result.hits) == 1
    assert result.hits[0].provider == "brave-search" and result.hits[0].account is None
    assert result.hits[0].raw["transport"] == "omniroute"
    assert result.hits[0].rank == 1 and result.hits[0].snippet == "Source evidence"
    assert result.usage == {"transport": "omniroute", "cached": False}


async def test_zero_search_results_is_valid_when_provider_verified():
    async with client_for(lambda request: httpx.Response(200, json={"provider": "brave-search",
                         "results": [], "errors": []})) as client:
        result = await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "none"}, Context(client))
    assert result.hits == []


@pytest.mark.parametrize("payload", [
    {"provider": "serper-search", "results": [], "errors": []},
    {"results": [], "errors": []},
    {**SEARCH, "results": [{**SEARCH["results"][0], "citation": {"provider": "serper-search"}}]},
    {**SEARCH, "errors": [{"error": "partial"}]},
    {**SEARCH, "results": [{"title": "Broken", "citation": {"provider": "brave-search"}}]},
])
async def test_search_rejects_provider_substitution_partial_or_invalid_hits(payload):
    async with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "source"}, Context(client))
    assert caught.value.kind == ErrorKind.TRANSIENT
    assert "service-only" not in str(caught.value)


@pytest.mark.parametrize("unsupported", [
    {"date_from": "2026-10-01"}, {"provider_options": {"provider": "serper-search"}}, {"fresh": True},
])
async def test_unsupported_options_are_rejected_without_network(unsupported):
    # The router never forwards ``fresh``; a direct adapter caller still cannot smuggle options upstream.
    async with client_for(lambda request: pytest.fail("No upstream request expected")) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "source", **unsupported}, Context(client))
    assert caught.value.kind == ErrorKind.BAD_REQUEST and not caught.value.block_capability


async def test_verified_filters_use_omniroute_request_shape():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={**SEARCH, "provider": "brave-search", "results": []})

    async with client_for(handler) as client:
        await PROVIDERS[1].call(Capability.NEWS_SEARCH, {"query": "source", "domains": [" Example.org "],
                                                         "recency": "week"}, Context(client))
    assert sent[0]["filters"] == {"include_domains": ["example.org"]}
    assert sent[0]["time_range"] == "week" and sent[0]["search_type"] == "news"
    assert "fresh" not in sent[0]


def test_router_forwards_filters_only_to_verified_bridge_providers():
    from research_engine.router.requests import InvalidRequest, for_provider

    args = {"query": "q", "domains": ["example.org"], "fresh": True, "deadline_s": 40}
    assert for_provider(Capability.WEB_SEARCH, args, "omni:exa-search") == {"query": "q", "domains": ["example.org"]}
    for name in ("omni:serper-search", "omni:ollama-search", "omni:duckduckgo-free"):
        with pytest.raises(InvalidRequest):
            for_provider(Capability.WEB_SEARCH, args, name)
    recency = {"query": "q", "recency": "day"}
    assert for_provider(Capability.NEWS_SEARCH, recency, "omni:nimble-search")["recency"] == "day"
    with pytest.raises(InvalidRequest):
        for_provider(Capability.NEWS_SEARCH, recency, "omni:tavily-search")
    with pytest.raises(InvalidRequest):
        for_provider(Capability.WEB_SEARCH, {"query": "q", "date_from": "2026-01-01"}, "omni:exa-search")
    assert "fresh" not in for_provider(Capability.WEB_READ, {"target": "https://e.org", "fresh": True}, "omni:jina-reader")


async def test_reader_flat_response_source_text_and_provenance():
    def handler(request):
        assert request.url.path == "/api/v1/web/fetch"
        assert json.loads(request.content) == {"url": "https://example.org/article", "provider": "jina-reader",
                                               "format": "markdown", "include_metadata": True}
        return httpx.Response(200, json={"provider": "jina-reader", "url": "https://example.org/article",
                                         "content": "Source text", "metadata": {"title": "Source"}})

    async with client_for(handler) as client:
        ctx = Context(client, base_url="http://127.0.0.1:20128/api/v1")
        result = await PROVIDERS[3].call(Capability.WEB_READ, {"target": "https://example.org/article"}, ctx)
    assert ctx.targets == ["https://example.org/article", "https://example.org/article"]
    assert result.document.text == "Source text" and result.document.source == "jina-reader"
    assert result.raw == {"transport": "omniroute", "provider": "jina-reader"}


@pytest.mark.parametrize("payload", [
    {"provider": "firecrawl", "url": "https://example.org/article", "content": "text"},
    {"url": "https://example.org/article", "content": "text"},
    {"provider": "jina-reader", "url": "https://example.org/article", "content": ""},
])
async def test_reader_rejects_substitution_missing_provider_and_empty_source(payload):
    async with client_for(lambda request: httpx.Response(200, json=payload)) as client:
        with pytest.raises(BridgeError):
            await PROVIDERS[3].call(Capability.WEB_READ, {"target": "https://example.org/article"}, Context(client))


async def test_reader_validates_target_before_forwarding_service_key():
    async with client_for(lambda request: pytest.fail("No upstream request expected")) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[3].call(Capability.WEB_READ, {"target": "http://127.0.0.1/private"}, Context(client))
    assert caught.value.kind == ErrorKind.TARGET


@pytest.mark.parametrize("status,payload,expected,scope", [
    (401, {"error": "invalid key service-only"}, ErrorKind.AUTH, "connection"),
    (429, {"error": "rate limited"}, ErrorKind.RATE_LIMITED, "provider"),
    (429, {"error": "quota exhausted"}, ErrorKind.EXHAUSTED, "provider"),
    (402, {"error": "credits exhausted"}, ErrorKind.EXHAUSTED, "provider"),
    (503, {"error": "offline"}, ErrorKind.TRANSIENT, "provider"),
])
async def test_typed_errors_do_not_reflect_secrets(status, payload, expected, scope):
    async with client_for(lambda request: httpx.Response(status, json=payload,
                         headers={"Retry-After": "2"})) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "source"}, Context(client))
    assert caught.value.kind == expected and caught.value.scope == scope
    assert "service-only" not in str(caught.value)
    assert caught.value.retry_after == 2


async def test_gateway_timeout_and_outage_are_connection_scoped():
    def handler(request):
        raise httpx.ConnectError("unavailable", request=request)

    async with client_for(handler) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "source"}, Context(client))
    assert caught.value.scope == "connection" and caught.value.kind == ErrorKind.TRANSIENT


async def test_rerank_explicit_provider_model_and_sorted_indices():
    def handler(request):
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content) == {"model": "cohere/rerank-v4.0-pro", "query": "science",
            "documents": ["a", "b"], "top_n": 2, "return_documents": False}
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1}]},
            headers={"X-OmniRoute-Provider": "cohere", "X-OmniRoute-Model": "rerank-v4.0-pro"})

    async with client_for(handler) as client:
        ranking = await OmniRouteBridge().rerank("cohere/rerank-v4.0-pro", "science", ["a", "b"], Context(client))
    assert [row["index"] for row in ranking] == [1, 0]


@pytest.mark.parametrize("headers", [
    {}, {"X-OmniRoute-Provider": "jina-ai"},
    {"X-OmniRoute-Provider": "cohere", "X-OmniRoute-Model": "wrong"},
])
async def test_rerank_rejects_unverified_provider_or_model(headers):
    async with client_for(lambda request: httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": 1.0}]}, headers=headers)) as client:
        with pytest.raises(BridgeError) as caught:
            await OmniRouteBridge().rerank("cohere/rerank-v4.0-pro", "science", ["a"], Context(client))
    assert caught.value.kind == ErrorKind.TRANSIENT


async def test_no_service_credential_fails_before_network():
    async with client_for(lambda request: pytest.fail("No upstream request expected")) as client:
        with pytest.raises(BridgeError) as caught:
            await PROVIDERS[1].call(Capability.WEB_SEARCH, {"query": "source"}, Context(client, key=""))
    assert caught.value.kind == ErrorKind.AUTH and caught.value.scope == "connection"
