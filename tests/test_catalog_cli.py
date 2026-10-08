"""Registry seeding and admin password compatibility boundaries."""

from research_engine.providers.base import Capability, Provider
from research_engine.providers.registry import build_registry, default_routes
from research_engine.storage.crypto import hash_password, verify_password


class UnapprovedSearch(Provider):
    name = "unapproved_search"
    capabilities = frozenset({Capability.WEB_SEARCH})


def test_registry_requires_shipped_adapters():
    registry = build_registry()
    assert "github" in registry
    assert "openalex" in registry


def test_registration_does_not_seed_a_route():
    routes = default_routes({"unapproved_search": UnapprovedSearch()})
    assert "web_search" not in routes


def test_password_creation_and_existing_bcrypt_compatibility():
    import bcrypt

    password = "a-local-admin-password"
    assert verify_password(password, hash_password(password))
    old_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    assert verify_password(password, old_hash)
    assert not verify_password("wrong", old_hash)


def test_env_import_creates_one_account_per_comma_separated_key(tmp_path, monkeypatch):
    from sqlalchemy import select

    from research_engine.cli import KEYS, _database, import_environment
    from research_engine.storage.crypto import Cipher, SecretStore
    from research_engine.storage.db import Account
    from test_mcp_e2e import make_settings

    for variable in [*KEYS, "OMNI_ROUTE_API_URL"]:
        monkeypatch.delenv(variable, raising=False)
    settings = make_settings(tmp_path / "runtime", 8765)
    monkeypatch.setenv("CONSENSUS_API_KEY", " fixture-key-one, fixture-key-two ,fixture-key-one,")
    monkeypatch.setenv("OMNI_ROUTE_API_KEY", "fixture,bridge-key-with-comma")
    db, registry = _database(settings)
    try:
        assert import_environment(settings, db, registry).count("consensus_api") == 2
        store = SecretStore(db, Cipher.from_file(settings.encryption_key_file, db))

        def accounts(provider):
            with db.session() as session:
                return [(a.id, a.label, a.priority, a.enabled) for a in session.scalars(
                    select(Account).where(Account.provider == provider).order_by(Account.label))]

        consensus = accounts("consensus_api")
        assert [(label, priority, enabled) for _, label, priority, enabled in consensus] == [
            ("environment", 0, True), ("environment-2", 1, True)]
        assert [store.get(account_id)["api_key"] for account_id, *_ in consensus] == [
            "fixture-key-one", "fixture-key-two"]
        bridge = accounts("omniroute")
        assert len(bridge) == 1 and store.get(bridge[0][0])["api_key"] == "fixture,bridge-key-with-comma"

        monkeypatch.setenv("CONSENSUS_API_KEY", "fixture-key-three")
        import_environment(settings, db, registry)
        consensus = accounts("consensus_api")
        assert [(label, enabled) for _, label, _, enabled in consensus] == [
            ("environment", True), ("environment-2", False)]
        assert store.get(consensus[0][0])["api_key"] == "fixture-key-three"
    finally:
        db.close()


def test_env_import_lifecycle_preserves_state_and_manual_accounts(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from research_engine.cli import KEYS, _database, import_environment
    from research_engine.storage.db import Account
    from test_mcp_e2e import make_settings

    for variable in [*KEYS, "OMNI_ROUTE_API_URL"]:
        monkeypatch.delenv(variable, raising=False)
    settings = make_settings(tmp_path / "runtime", 8765)
    db, registry = _database(settings)

    def state():
        with db.session() as session:
            return {a.label: (a.enabled, a.cooldown_until is not None) for a in session.scalars(
                select(Account).where(Account.provider == "consensus_api"))}

    try:
        with db.session() as session:
            session.add(Account(provider="consensus_api", label="environment-manual", credential="ok", enabled=True))
            session.add(Account(provider="consensus_api", label="team", credential="ok", enabled=False))
        monkeypatch.setenv("CONSENSUS_API_KEY", "key-one,key-two")
        import_environment(settings, db, registry)
        with db.session() as session:
            second = session.scalar(select(Account).where(Account.label == "environment-2"))
            second.cooldown_until = datetime.now(UTC) + timedelta(hours=1)
        # Removing every key is a no-op: an absent variable never disables accounts.
        monkeypatch.delenv("CONSENSUS_API_KEY")
        import_environment(settings, db, registry)
        assert state()["environment-2"] == (True, True)
        monkeypatch.setenv("CONSENSUS_API_KEY", "key-one")
        import_environment(settings, db, registry)
        assert state()["environment-2"] == (False, True)
        # Reintroducing the key restores usability but keeps its availability evidence.
        monkeypatch.setenv("CONSENSUS_API_KEY", "key-one,key-two")
        import_environment(settings, db, registry)
        assert state() == {"environment": (True, False), "environment-2": (True, True),
                           "environment-manual": (True, False), "team": (False, False)}
    finally:
        db.close()


def test_multiple_env_accounts_remain_one_evidence_vote():
    from research_engine.merge.core import merge_hits
    from research_engine.server.schemas import Hit

    hits = [Hit(provider="consensus_api", account=account, rank=1, title="Same paper", url="https://doi.org/10.1234/x",
                ids={"doi": "10.1234/x"}) for account in (1, 2)]
    item = merge_hits(hits).items[0]
    assert item["score"] == 1 / 61  # accounts of one provider never add RRF votes
