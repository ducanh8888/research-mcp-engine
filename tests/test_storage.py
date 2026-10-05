"""Real SQLite migration, catalog, account-state and local maintenance regressions."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text

from research_engine.cache import BLOB_GRACE_S, Cache
from research_engine.storage.blobs import BlobStore
from research_engine.storage.db import (
    Account, AccountSecret, Attempt, Database, DocumentCache, Handle, Job,
    ProviderRow, QueryCache, RequestRow, Routing, utcnow,
)
from research_engine.storage.migrations import v0001_core, v0002_cache, v0003_jobs


class ShippedProvider:
    def __init__(self, capabilities, *, keyless=False, options=None):
        self.capabilities = capabilities
        self.keyless = keyless
        self.options = options or {}


def old_database(path):
    """Create a version-three DB using the immutable shipped historical DDL."""
    db = Database(path)
    with db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"))
        for version, module in (("0001", v0001_core), ("0002", v0002_cache), ("0003", v0003_jobs)):
            module.upgrade(connection)
            connection.execute(text(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (:version, '2025-01-01')"
            ), {"version": version})
    return db


def test_old_database_upgrade_preserves_data_and_matches_fresh_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    old = old_database(path)
    with old.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO providers(name, enabled, options, capabilities) "
            "VALUES ('fixture', 0, :options, :caps)"
        ), {"options": '{"operator": 1}', "caps": '["web_search", "paper_read"]'})
        connection.execute(text(
            "INSERT INTO accounts(provider, label, priority, enabled, credential, blocked_capabilities, "
            "transient_failures, adapter_status) VALUES "
            "('fixture', 'list', 0, 1, 'valid', :blocks, 0, 'unknown'), "
            "('fixture', 'dict', 1, 1, 'needs_auth', :reasons, 0, 'unknown'), "
            "('fixture', 'disabled', 2, 1, 'disabled', '[]', 0, 'unknown'), "
            "('fixture', 'unknown', 3, 1, 'unknown', '[]', 0, 'unknown')"
        ), {"blocks": '["paper_read"]', "reasons": '{"web_search": "denied by plan"}'})
        connection.execute(text(
            "INSERT INTO account_secrets(account_id, kind, encrypted_value, updated_at) "
            "VALUES (1, 'credentials', :secret, '2025-01-01')"
        ), {"secret": b"encrypted fixture bytes (not a real key)"})
        connection.execute(text(
            "INSERT INTO routing(capability, mode, providers) "
            "VALUES ('paper_read', 'sequential', '[\"fixture\"]')"
        ))
        connection.execute(text(
            "INSERT INTO handles(handle, canonical_ids, metadata_json, created_at, updated_at) "
            "VALUES ('h_old', '{}', '{}', '2025-01-01', '2025-01-01')"
        ))
    old.close()

    upgraded = Database(path)
    upgraded.initialize({"fixture": ShippedProvider(["web_search"], options={"new": 2})})
    with upgraded.session() as session:
        rows = list(session.scalars(select(Account).order_by(Account.id)))
        assert [row.credential for row in rows] == ["ok", "needs_auth", "disabled", "needs_auth"]
        assert rows[0].blocked_capabilities == {"paper_read": "Legacy block (reason unknown)"}
        assert rows[1].blocked_capabilities == {"web_search": "denied by plan"}
        assert all(row.quota_observed_at is row.quota_units is row.quota_scope is None for row in rows)
        assert session.get(AccountSecret, (1, "credentials")).encrypted_value == b"encrypted fixture bytes (not a real key)"
        provider = session.get(ProviderRow, "fixture")
        assert provider.capabilities == ["web_search"]
        assert provider.enabled is False and provider.options == {"operator": 1}
        assert session.get(Routing, "paper_read").providers == ["fixture"]
        assert session.get(Handle, "h_old") is not None
        rows[0].blocked_capabilities["web_search"] = "second plan denial"
    upgraded.close()

    fresh = Database(tmp_path / "new.sqlite")
    fresh.initialize()
    assert {name: {col["name"] for col in inspect(fresh.engine).get_columns(name)}
            for name in inspect(fresh.engine).get_table_names()} == {
        name: {col["name"] for col in inspect(upgraded.engine).get_columns(name)}
        for name in inspect(upgraded.engine).get_table_names()
    }
    with fresh.session() as session:
        assert session.execute(select(Account)).all() == []
        session.add(ProviderRow(name="fixture"))
        session.flush()
        session.add(Account(provider="fixture", label="new"))
    with fresh.session() as session:
        account = session.scalar(select(Account))
        assert account.credential == "needs_auth" and account.blocked_capabilities == {}
    fresh.close()

    restarted = Database(path)
    restarted.initialize({"fixture": ShippedProvider(["web_search"], options={"new": 2})})
    with restarted.session() as session:
        row = session.get(Account, 1)
        assert row.blocked_capabilities == {
            "paper_read": "Legacy block (reason unknown)",
            "web_search": "second plan denial",
        }
        assert [row[0] for row in session.execute(text("SELECT version FROM schema_migrations ORDER BY version"))] == [
            "0001", "0002", "0003", "0004",
        ]
    restarted.close()


def test_migration_rejects_invalid_legacy_block_without_losing_data(tmp_path):
    db = old_database(tmp_path / "bad.sqlite")
    with db.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO providers(name, enabled, options, capabilities) VALUES ('p', 1, '{}', '[]')"
        ))
        connection.execute(text(
            "INSERT INTO accounts(provider, label, priority, enabled, credential, blocked_capabilities, "
            "transient_failures, adapter_status) "
            "VALUES ('p', 'broken', 0, 1, 'valid', '[2]', 0, 'unknown')"
        ))
    with pytest.raises(ValueError, match="invalid capability blocks"):
        db.initialize()
    with db.engine.connect() as connection:
        assert connection.execute(text("SELECT blocked_capabilities FROM accounts")).scalar() == "[2]"
        assert connection.execute(text("SELECT version FROM schema_migrations WHERE version='0004'")).scalar() is None
    with db.engine.begin() as connection:
        connection.execute(text("UPDATE accounts SET blocked_capabilities = '[\"paper_read\"]'"))
    db.initialize()
    with db.session() as session:
        assert session.get(Account, 1).blocked_capabilities == {
            "paper_read": "Legacy block (reason unknown)",
        }
    db.close()


def test_catalog_refresh_preserves_operator_state_and_surfaces_removed_support(tmp_path):
    db = Database(tmp_path / "catalog.sqlite")
    original = {"provider": ShippedProvider(["web_search", "paper_read"], options={"default": "one"}, keyless=True)}
    db.initialize(original, {"web_search": {"mode": "sequential", "providers": ["provider"]}})
    with db.session() as session:
        row = session.get(ProviderRow, "provider")
        row.enabled, row.options = False, {"override": "saved"}
        session.get(Routing, "web_search").mode = "fanout"
        session.get(Account, 1).label = "operator account"
    db.initialize({"provider": ShippedProvider(["paper_read", "citation_verify"], options={"default": "two"})},
                  {"web_search": {"mode": "sequential", "providers": ["provider"]},
                   "paper_read": {"mode": "sequential", "providers": ["provider"]}})
    with db.session() as session:
        provider = session.get(ProviderRow, "provider")
        assert provider.capabilities == ["paper_read", "citation_verify"]
        assert provider.options == {"override": "saved"} and provider.enabled is False
        assert session.get(Routing, "web_search").mode == "fanout"
        assert session.get(Routing, "web_search").providers == ["provider"]
        assert session.get(Routing, "paper_read").providers == ["provider"]
        assert session.get(Account, 1).label == "operator account"
    db.close()


def test_cache_clear_retains_shared_blob_and_reclaims_last_reference(tmp_path):
    db = Database(tmp_path / "cache.sqlite")
    db.initialize()
    store = BlobStore(tmp_path / "blobs")
    cache = Cache(db, store)
    cache.put_document("paper_read", "A", {"text": "shared", "title": "A"})
    cache.put_document("web_read", "B", {"text": "shared", "title": "B"})
    cache.put_query("paper_search", {"query": "x"}, {"status": "complete"})
    with db.session() as session:
        rows = list(session.scalars(select(DocumentCache)))
        assert rows[0].blob_ref == rows[1].blob_ref
        session.delete(next(row for row in rows if row.metadata_json["title"] == "A"))
        ref = rows[0].blob_ref
    assert cache.get_document("web_read", "B")["text"] == "shared"
    assert cache.clear("query") == {"queries": 1, "documents": 0, "blobs": 0}
    assert store.path(ref).exists()
    assert cache.clear("document") == {"queries": 0, "documents": 1, "blobs": 1}
    assert not store.path(ref).exists()
    db.close()


def test_maintenance_dry_run_batches_expiry_and_preserves_live_data(tmp_path):
    db = Database(tmp_path / "maintenance.sqlite")
    db.initialize({"provider": ShippedProvider(["web_search"])})
    blobs = BlobStore(tmp_path / "blobs")
    cache = Cache(db, blobs)
    now = utcnow()
    old = now - timedelta(days=120)
    ref = blobs.put("stale")
    shared = blobs.put("still referenced")
    orphan = blobs.put("unreferenced")
    for value in (ref, shared, orphan):
        os.utime(blobs.path(value), (now.timestamp() - BLOB_GRACE_S - 30,) * 2)
    with db.session() as session:
        session.add_all([
            QueryCache(key="stale_q", payload={}, expires_at=old),
            QueryCache(key="live_q", payload={}, expires_at=now + timedelta(days=1)),
            DocumentCache(key="stale_d", blob_ref=ref, metadata_json={}, expires_at=old),
            DocumentCache(key="live_d", blob_ref=shared, metadata_json={}, expires_at=now + timedelta(days=1)),
            DocumentCache(key="shared_expired", blob_ref=shared, metadata_json={}, expires_at=old),
            RequestRow(id="old_done", tool="web_search", args={}, status="complete",
                       started_at=old, finished_at=old, coverage={}, result={}),
            RequestRow(id="old_running", tool="web_search", args={}, status="running",
                       started_at=old, finished_at=None, coverage={}),
            RequestRow(id="recent_done", tool="web_search", args={}, status="complete",
                       started_at=now, finished_at=now, coverage={}),
        ])
        session.add(Account(provider="provider", credential="ok"))
        session.flush()
        for status in ("completed", "running"):
            session.add(Job(id=f"job_{status}", capability="site_crawl", provider="provider",
                            account_id=1, args={}, upstream_job_ref="saved", status=status,
                            result={"complete": True} if status == "completed" else None,
                            created_at=old, updated_at=old, poll_after_s=15))
        session.add(Attempt(request_id="old_done", provider="provider", outcome="ok"))
    snapshot = cache.maintain(dry_run=True, now=now)
    assert snapshot == {"queries": 1, "documents": 2, "requests": 1, "jobs": 1, "blobs": 2}
    with db.session() as session:
        assert session.get(RequestRow, "old_done") is not None
        assert session.get(Job, "job_completed") is not None
    assert blobs.path(ref).exists() and blobs.path(orphan).exists()

    assert cache.maintain(now=now, limit=1) == {
        "queries": 1, "documents": 1, "requests": 1, "jobs": 1, "blobs": 1,
    }
    assert cache.maintain(now=now)["documents"] == 1
    with db.session() as session:
        assert session.get(QueryCache, "live_q") is not None
        assert session.get(DocumentCache, "live_d").blob_ref == shared
        assert session.get(RequestRow, "old_running") is not None
        assert session.get(RequestRow, "recent_done") is not None
        assert session.get(RequestRow, "old_done") is not None
        assert session.scalar(select(Attempt).where(Attempt.request_id == "old_done")) is not None
        assert session.get(Job, "job_running").upstream_job_ref == "saved"
        assert session.get(Job, "job_completed") is not None
    assert blobs.path(shared).exists() and not blobs.path(ref).exists() and not blobs.path(orphan).exists()
    db.close()


def test_startup_maintenance_sweeps_expired_and_old_orphans(tmp_path):
    db = Database(tmp_path / "restart.sqlite")
    db.initialize()
    blobs = BlobStore(tmp_path / "blobs")
    ref = blobs.put("orphaned")
    os.utime(blobs.path(ref), (0, 0))
    with db.session() as session:
        session.add(DocumentCache(key="stale", blob_ref=ref, metadata_json={},
                                  expires_at=utcnow() - timedelta(days=1)))
    db.close()
    restarted = Database(tmp_path / "restart.sqlite")
    restarted.initialize()
    Cache(restarted, BlobStore(tmp_path / "blobs"))
    with restarted.session() as session:
        assert session.get(DocumentCache, "stale") is None
    assert not blobs.path(ref).exists()
    restarted.close()
