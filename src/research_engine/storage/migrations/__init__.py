"""Numbered, idempotent upgrades; never recreate a database during startup.

Adapted from the migration ordering/checkpoint idea in R0Wi/mcp-gateway
db_migrations.py @59c1efd (MIT), using the engine's SQLAlchemy connection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module

from sqlalchemy import Engine, text

MIGRATIONS = (
    ("0001", "v0001_core"),
    ("0002", "v0002_cache"),
    ("0003", "v0003_jobs"),
    ("0004", "v0004_account_state"),
)


def run_migrations(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        ))
        applied = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        unknown = applied - {version for version, _ in MIGRATIONS}
        if unknown:
            raise RuntimeError("Database contains newer schema migrations; use a compatible engine")
        for version, module_name in MIGRATIONS:
            if version in applied:
                continue
            module = import_module(f"{__name__}.{module_name}")
            module.upgrade(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(version, applied_at) VALUES (:version, :at)"),
                {"version": version, "at": datetime.now(UTC).isoformat()},
            )
