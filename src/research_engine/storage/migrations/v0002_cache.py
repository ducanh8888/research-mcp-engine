"""Frozen v0002 source document and complete query result cache tables."""

from sqlalchemy import Connection, text


DDL = (
    """CREATE TABLE IF NOT EXISTS query_cache (
        key VARCHAR NOT NULL PRIMARY KEY, payload JSON NOT NULL, expires_at DATETIME NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_query_cache_expires_at ON query_cache (expires_at)",
    """CREATE TABLE IF NOT EXISTS doc_cache (
        key VARCHAR NOT NULL PRIMARY KEY, blob_ref VARCHAR NOT NULL,
        metadata_json JSON NOT NULL, expires_at DATETIME NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_doc_cache_expires_at ON doc_cache (expires_at)",
)


def upgrade(connection: Connection) -> None:
    for statement in DDL:
        connection.execute(text(statement))
