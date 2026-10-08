"""Public documented hosted tools, schema guards and account-scoped OAuth fixtures."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthToken
from mcp_types import CallToolResult, TextContent, Tool

from research_engine.providers.base import CallContext, Capability, ErrorKind, ProviderError
from research_engine.providers.mcp.adapters import ElicitMCPProvider, PROVIDERS, SciteMCPProvider
from research_engine.providers.mcp.clients import AccountMCPClientManager, HOSTED_ENDPOINTS, _NoForwardTransport
from research_engine.providers.mcp.oauth import AccountTokenStorage, OAuthFlowCoordinator, OAuthStateError


class FakeManager:
    def __init__(self, name: str, properties: dict, response: CallToolResult):
        self.name = name
        self.properties = properties
        self.response = response
        self.calls = []

    async def list_tools(self, account_id, url, **kwargs):
        return [Tool(name=self.name, inputSchema={"type": "object", "properties": self.properties})]

    async def call_tool(self, account_id, url, name, arguments, **kwargs):
        self.calls.append((account_id, url, name, arguments, kwargs))
        return self.response


def ctx(manager: FakeManager, provider: str) -> CallContext:
    return CallContext(SimpleNamespace(), {"auth_type": "oauth"}, 7, provider, time.monotonic() + 3,
                       mcp_manager=manager)


def tool_result(data, *, structured=True):
    return CallToolResult(structuredContent=data if structured else None,
                          content=[TextContent(text=json.dumps(data))])


async def test_scite_documented_search_result_is_paper_metadata_not_claim_verification():
    manager = FakeManager("search_literature", {"term": {}, "limit": {}, "date_from": {}, "date_to": {}},
                          tool_result({"hits": [{"doi": "10.1038/ABC", "title": "Trial", "year": 2023,
                             "abstract": "Author abstract", "citations": [{"snippet": "supports X",
                             "type": "supporting"}], "tally": {"supporting": 7},
                             "editorialNotices": [{"status": "Has correction"}]}]}))
    result = await SciteMCPProvider().call(Capability.PAPER_SEARCH,
                 {"query": "treatment", "limit": 3, "year_from": 2021, "year_to": 2024}, ctx(manager, "scite_mcp"))
    assert manager.calls[0][:4] == (7, HOSTED_ENDPOINTS["scite"], "search_literature",
                                  {"term": "treatment", "limit": 3, "date_from": "2021-01-01",
                                   "date_to": "2024-12-31"})
    hit = result.hits[0]
    assert hit.ids == {"doi": "10.1038/abc"} and hit.snippet == "Author abstract"
    assert hit.raw["tally"]["supporting"] == 7 and hit.raw["citations"][0]["snippet"] == "supports X"
    assert result.verification is None and result.document is None


async def test_elicit_search_preserves_source_only_and_translates_year_filters():
    manager = FakeManager("search_papers", {"query": {}, "maxResults": {}, "filters": {}},
                          tool_result({"papers": [{"elicitId": "p1", "title": "Study", "year": 2022,
                              "abstract": "Original abstract", "summary": "Generated answer"}], "warnings": []},
                              structured=False))
    result = await ElicitMCPProvider().call(Capability.PAPER_SEARCH,
                     {"query": "heart", "year_from": 2018, "filters": {"hasPdf": True}}, ctx(manager, "elicit_mcp"))
    assert manager.calls[0][3] == {"query": "heart", "maxResults": 8,
                                    "filters": {"hasPdf": True, "minYear": 2018}}
    assert result.hits[0].ids == {"elicit": "p1"}
    assert result.hits[0].snippet == "Original abstract"
    assert "Generated answer" not in result.hits[0].snippet


@pytest.mark.parametrize("result", [tool_result({"answer": "not a paper"}),
                                   CallToolResult(content=[TextContent(text="Not JSON")]),
                                   tool_result({"hits": [{"title": "Missing DOI"}]}),
                                   CallToolResult(content=[TextContent(text="quota exceeded")], isError=True)])
async def test_scite_rejects_unverified_result_without_claiming_empty_success(result):
    manager = FakeManager("search_literature", {"term": {}, "limit": {}}, result)
    with pytest.raises(ProviderError) as error:
        await SciteMCPProvider().call(Capability.PAPER_SEARCH, {"query": "trial"}, ctx(manager, "scite_mcp"))
    assert error.value.kind in {ErrorKind.TRANSIENT, ErrorKind.EXHAUSTED}
    assert len(manager.calls) == 1


async def test_mcp_schema_drift_and_unsupported_filters_fail_before_upstream_call():
    manager = FakeManager("search_literature", {"query": {}, "limit": {}}, tool_result({"hits": []}))
    with pytest.raises(ProviderError) as error:
        await SciteMCPProvider().call(Capability.PAPER_SEARCH, {"query": "trial"}, ctx(manager, "scite_mcp"))
    assert error.value.kind == ErrorKind.TRANSIENT and not manager.calls
    manager = FakeManager("search_papers", {"query": {}, "maxResults": {}}, tool_result({"papers": []}))
    with pytest.raises(ProviderError) as error:
        await ElicitMCPProvider().call(Capability.PAPER_SEARCH,
                        {"query": "trial", "filters": {"invented": True}}, ctx(manager, "elicit_mcp"))
    assert error.value.kind == ErrorKind.BAD_REQUEST and not manager.calls


def test_no_unverified_hosted_tool_is_registered():
    assert {provider.name for provider in PROVIDERS} == {"scite_mcp", "elicit_mcp"}
    assert all(provider.capabilities == frozenset({Capability.PAPER_SEARCH}) for provider in PROVIDERS)
    assert "undermind" not in {provider.name for provider in PROVIDERS}


def test_oauth_requires_published_undermind_client_metadata():
    manager = AccountMCPClientManager(SimpleNamespace(), "https://callback.example.org")
    with pytest.raises(ValueError, match="client metadata URL"):
        manager._build_client(1, HOSTED_ENDPOINTS["undermind"], {"auth_type": "oauth"})
    client = manager._build_client(1, HOSTED_ENDPOINTS["undermind"],
                {"auth_type": "oauth", "client_metadata_url": "https://callback.example.org/oauth/client.json"})
    assert client.transport.auth.context.client_metadata_url == "https://callback.example.org/oauth/client.json"
    client = manager._build_client(1, HOSTED_ENDPOINTS["scite"],
                                  {"auth_type": "oauth", "scopes": ["mcp"]})
    assert client.transport.auth.context.client_metadata.scope == "mcp"


def test_transport_never_enables_incoming_bearer_forwarding():
    transport = _NoForwardTransport(HOSTED_ENDPOINTS["scite"], headers={"Authorization": "Bearer fixture-key"})
    from fastmcp.client.transports.base import TransportOptions

    async def check():
        async with transport.connect_session(transport_options=TransportOptions(forward_incoming_headers=True)):
            pass

    with pytest.raises(ValueError, match="cannot forward"):
        asyncio.run(check())


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account_id, kind="credentials"):
        return self.values.get((account_id, kind), {}).copy()

    def set(self, account_id, value, kind="credentials"):
        self.values[account_id, kind] = value.copy()


async def test_account_token_storage_preserves_rotation_and_restart_expiry():
    secrets = MemorySecrets()
    storage = AccountTokenStorage(secrets, 7)
    await storage.set_tokens(OAuthToken(access_token="first", refresh_token="refresh", expires_in=120))
    assert "expires_at" in secrets.get(7, "oauth_tokens")
    await storage.set_tokens(OAuthToken(access_token="second", expires_in=60))
    reloaded = await AccountTokenStorage(secrets, 7).get_tokens()
    assert reloaded.access_token == "second" and reloaded.refresh_token == "refresh"
    assert 0 < reloaded.expires_in <= 60
    info = OAuthClientInformationFull(client_id="registered")
    await storage.set_client_info(info)
    assert (await AccountTokenStorage(secrets, 7).get_client_info()).client_id == "registered"


async def test_oauth_callback_is_sdk_v2_result_and_exactly_once():
    coordinator = OAuthFlowCoordinator(timeout=2)

    async def connector():
        redirect, callback = coordinator.handlers(7)
        await redirect("https://auth.example.org/authorize?state=fixture-state")
        received.append(await callback())

    received = []
    pending = await coordinator.begin(7, connector)
    assert pending["status"] == "authorization_pending"
    assert coordinator.callback("fixture-state", code="fixture-code", issuer="https://auth.example.org") == {
        "account_id": 7, "status": "exchanging_token"}
    with pytest.raises(OAuthStateError):
        coordinator.callback("fixture-state", code="reuse")
    for _ in range(30):
        if received:
            break
        await asyncio.sleep(0.01)
    assert received == [AuthorizationCodeResult(code="fixture-code", state="fixture-state",
                                                iss="https://auth.example.org")]
    await coordinator.close()


async def test_restarted_connect_replaces_stale_flow_and_rejects_old_state():
    coordinator = OAuthFlowCoordinator(timeout=5)

    def connector_for(state):
        async def connector():
            redirect, callback = coordinator.handlers(7)
            await redirect(f"https://auth.example.org/authorize?state={state}")
            await callback()
        return connector

    first = await coordinator.begin(7, connector_for("first-state"))
    assert first["state"] == "first-state"
    # The owner abandoned the first consent page and pressed Connect again.
    second = await coordinator.begin(7, connector_for("second-state"))
    assert second["status"] == "authorization_pending" and second["state"] == "second-state"
    with pytest.raises(OAuthStateError):
        coordinator.callback("first-state", code="late-code")
    assert coordinator.callback("second-state", code="fresh-code")["status"] == "exchanging_token"
    await coordinator.close()


def test_elicit_oauth_requests_refresh_scope():
    from research_engine.providers.mcp.clients import HOSTED_SCOPES
    assert HOSTED_SCOPES["elicit"] == ["elicit.mcp", "offline_access"]
