"""A missing shared bridge account must not poison virtual provider markers."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from research_engine.providers.base import Capability
from research_engine.providers.omniroute import OmniRouteConnection, OmniRouteProvider
from research_engine.router.execute import Engine, ToolError
from research_engine.storage.db import Account, ProviderRow, Routing
from test_mcp_e2e import make_settings


@pytest.mark.asyncio
async def test_missing_connection_keeps_virtual_account_ready(tmp_path):
    providers = [OmniRouteConnection(), OmniRouteProvider(
        "omni:exa-search", "exa-search", {Capability.WEB_SEARCH})]
    engine = Engine(make_settings(tmp_path, 8765), providers={p.name: p for p in providers})
    try:
        with engine.db.session() as session:
            session.merge(Routing(capability="web_search", mode="fanout", providers=["omni:exa-search"]))
            connection = session.get(ProviderRow, "omniroute")
            connection.options = {"base_url": "https://gateway.example/v1"}
        for _ in range(2):
            with pytest.raises(ToolError) as failure:
                await engine.execute("web_search", {"query": "fixture", "fresh": True})
            assert failure.value.code == "NO_PROVIDER_AVAILABLE"
            with engine.db.session() as session:
                virtual = session.scalar(select(Account).where(Account.provider == "omni:exa-search"))
                assert virtual.credential == "ok"
                assert virtual.transient_failures == 0
                assert virtual.cooldown_until is None
    finally:
        await engine.stop()
