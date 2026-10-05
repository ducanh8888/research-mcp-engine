"""Ordinary engine search reranking uses only encrypted bridge credentials."""

from __future__ import annotations

import httpx
import pytest

from research_engine.providers.base import Capability, Provider
from research_engine.providers.omniroute import OmniRouteConnection
from research_engine.router.execute import Engine
from research_engine.server.schemas import Hit, Result
from research_engine.storage.db import Account, ProviderRow, Routing
from test_mcp_e2e import make_settings


class FusedResults(Provider):
    name = "fused_fixture"
    keyless = True
    capabilities = frozenset({Capability.WEB_SEARCH})

    async def call(self, cap, req, ctx):
        return Result(hits=[Hit(provider=self.name, rank=n, title=str(n),
                                url=f"https://example.org/{n}") for n in (1, 2)])


@pytest.mark.asyncio
async def test_bridge_rerank_applies_to_search_and_preserves_fallback(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"x-omniroute-provider": "jina",
                                             "x-omniroute-model": "test-reranker"},
                              json={"results": [{"index": 1, "relevance_score": 0.9},
                                                {"index": 0, "relevance_score": 0.1}]})

    settings = make_settings(tmp_path, 8765)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        engine = Engine(settings, providers={FusedResults.name: FusedResults(),
                                             "omniroute": OmniRouteConnection()}, http_client=client)
        try:
            with engine.db.session() as session:
                provider = session.get(ProviderRow, "omniroute")
                provider.options = {"base_url": "https://gateway.example/v1",
                                    "rerank": {"enabled": True, "backend": "omniroute",
                                               "model": "jina/test-reranker"}}
                session.add(Account(provider="omniroute", credential="ok", label="service"))
                session.merge(Routing(capability="web_search", mode="fanout", providers=[FusedResults.name]))
                session.flush()
                account_id = session.query(Account).filter_by(provider="omniroute").one().id
            engine.secrets.set(account_id, {"api_key": "bridge-only-secret"})
            response = await engine.execute("web_search", {"query": "fixture", "fresh": True})
            assert [item["title"] for item in response["items"]] == ["2", "1"]
            assert response["rerank"]["status"] == "applied"
            assert calls[0].headers["Authorization"] == "Bearer bridge-only-secret"
            assert b"bridge-only-secret" not in calls[0].content

            calls.clear()
            with engine.db.session() as session:
                provider = session.get(ProviderRow, "omniroute")
                provider.options = {**provider.options, "rerank": {"enabled": False}}
            disabled = await engine.execute("web_search", {"query": "different", "fresh": True})
            assert [item["title"] for item in disabled["items"]] == ["1", "2"]
            assert calls == []
        finally:
            await engine.stop()
