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
