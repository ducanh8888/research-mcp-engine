"""Normalize account eligibility and add durable, comparable quota observations."""

import json

from sqlalchemy import Connection, text


LEGACY_BLOCK_REASON = "Legacy block (reason unknown)"


def upgrade(connection: Connection) -> None:
    # SQLite/pysqlite DDL can commit before a later backfill fails. Checking
    # existing columns lets an operator correct a malformed row and retry.
    columns = {row[1] for row in connection.execute(text("PRAGMA table_info(accounts)"))}
    for name, column_type in (
        ("quota_observed_at", "DATETIME"),
        ("quota_units", "VARCHAR"),
        ("quota_scope", "VARCHAR"),
    ):
        if name not in columns:
            connection.execute(text(f"ALTER TABLE accounts ADD COLUMN {name} {column_type}"))

    # SQLite JSON has no enforced shape; preserve reasons already recorded by
    # the dict writer, while converting all prior list rows without dropping any
    # capability denials. A malformed row must fail rather than erase blocks.
    for account_id, raw in connection.execute(text("SELECT id, blocked_capabilities FROM accounts")):
        blocks = json.loads(raw) if isinstance(raw, str) else raw
        if blocks is None:
            normalized = {}
        elif isinstance(blocks, list) and all(isinstance(cap, str) for cap in blocks):
            normalized = dict.fromkeys(blocks, LEGACY_BLOCK_REASON)
        elif isinstance(blocks, dict) and all(
            isinstance(cap, str) and isinstance(reason, str) for cap, reason in blocks.items()
        ):
            normalized = blocks
        else:
            raise ValueError(f"Account {account_id} contains invalid capability blocks")
        connection.execute(
            text("UPDATE accounts SET blocked_capabilities = :blocks WHERE id = :id"),
            {"blocks": json.dumps(normalized), "id": account_id},
        )

    connection.execute(text("UPDATE accounts SET credential = 'ok' WHERE credential = 'valid'"))
    connection.execute(text(
        "UPDATE accounts SET credential = 'needs_auth' WHERE credential = 'unknown'"
    ))
    unsupported = connection.execute(text(
        "SELECT id FROM accounts WHERE credential NOT IN ('ok', 'needs_auth', 'disabled')"
    )).first()
    if unsupported:
        raise ValueError(f"Account {unsupported[0]} has an unrecognized credential state")
