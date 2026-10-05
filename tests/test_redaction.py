"""Secret-bearing upstream failures stay out of attempts, admin state and results."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from research_engine.providers.base import Capability, ErrorKind, Provider, ProviderError
from research_engine.router.execute import Engine, ToolError
from research_engine.router.redact import safe_error
from research_engine.storage.db import Account, Attempt, Routing
from test_mcp_e2e import make_settings


class LeakyProvider(Provider):
    name = "leaky"
    capabilities = frozenset({Capability.WEB_SEARCH})

    async def call(self, cap, req, ctx):
        raise ProviderError(ErrorKind.PLAN, "https://example.org/?code=secret-test with "
                            + ctx.credentials["nested"]["oauth"]["access_token"])


def test_nested_secret_and_url_are_not_reflected():
    secret = "sensitive-access-token"
    error = ProviderError(ErrorKind.AUTH, f"Visit https://example.org/?token={secret} to reconnect")
    message = safe_error(error, {"nested": {"oauth": {"access_token": secret}}})
    assert secret not in message and "https://" not in message
    assert message.startswith("auth:")
    unknown = safe_error(ProviderError(ErrorKind.TRANSIENT,
                                        "contact provider with unknown-private-string"))
    assert "unknown-private-string" not in unknown


@pytest.mark.asyncio
async def test_call_redacts_attempt_block_and_public_error(tmp_path):
    settings = make_settings(tmp_path, 8765)
    engine = Engine(settings, providers={"leaky": LeakyProvider()})
    secret = "sensitive-access-token"
    try:
        with engine.db.session() as session:
            session.add(Account(provider="leaky", credential="ok", label="fixture"))
            session.merge(Routing(capability="web_search", mode="fanout", providers=["leaky"]))
            session.flush()
            account_id = session.scalar(select(Account).where(Account.provider == "leaky")).id
        engine.secrets.set(account_id, {"nested": {"oauth": {"access_token": secret}}})
        with pytest.raises(ToolError) as caught:
            await engine.execute("web_search", {"query": "fixture", "fresh": True})
        assert secret not in str(caught.value.payload())
        with engine.db.session() as session:
            attempt = session.scalar(select(Attempt))
            account = session.get(Account, account_id)
            assert secret not in attempt.error
            assert secret not in str(account.blocked_capabilities)
        test_result = await engine.test_account(account_id)
        assert test_result["status"] == "blocked"
        assert secret not in str(test_result)
    finally:
        await engine.stop()
