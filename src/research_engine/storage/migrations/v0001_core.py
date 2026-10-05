"""Frozen v0001 SQLite schema: never derive historical DDL from current ORM models."""

from sqlalchemy import Connection, text


# These definitions mirror the shipped v0001 schema (including its list-shaped
# account blocks and "valid" credential vocabulary). Forward changes live in
# later migrations so an upgraded database and a new database take the same path.
DDL = (
    "CREATE TABLE IF NOT EXISTS meta (key VARCHAR NOT NULL PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS providers (
        name VARCHAR NOT NULL PRIMARY KEY, enabled BOOLEAN NOT NULL,
        options JSON NOT NULL, capabilities JSON NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER NOT NULL PRIMARY KEY, provider VARCHAR NOT NULL,
        label VARCHAR NOT NULL, priority INTEGER NOT NULL, enabled BOOLEAN NOT NULL,
        quota_group VARCHAR, credential VARCHAR NOT NULL, cooldown_until DATETIME,
        cooldown_reason VARCHAR, blocked_capabilities JSON NOT NULL,
        quota_remaining FLOAT, quota_reset_at DATETIME, transient_failures INTEGER NOT NULL,
        last_used DATETIME, adapter_status VARCHAR NOT NULL, tool_schema_hash VARCHAR,
        tool_schema JSON, FOREIGN KEY(provider) REFERENCES providers (name)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_accounts_provider ON accounts (provider)",
    """CREATE TABLE IF NOT EXISTS account_secrets (
        account_id INTEGER NOT NULL, kind VARCHAR NOT NULL, encrypted_value BLOB NOT NULL,
        updated_at DATETIME NOT NULL, PRIMARY KEY (account_id, kind),
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS routing (
        capability VARCHAR NOT NULL PRIMARY KEY, mode VARCHAR NOT NULL, providers JSON NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS client_tokens (
        id INTEGER NOT NULL PRIMARY KEY, token_hash VARCHAR(64) NOT NULL,
        label VARCHAR NOT NULL, revoked BOOLEAN NOT NULL, created_at DATETIME NOT NULL,
        last_used DATETIME, UNIQUE (token_hash)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_client_tokens_token_hash ON client_tokens (token_hash)",
    """CREATE TABLE IF NOT EXISTS requests (
        id VARCHAR NOT NULL PRIMARY KEY, tool VARCHAR NOT NULL, args JSON NOT NULL,
        status VARCHAR NOT NULL, started_at DATETIME NOT NULL, finished_at DATETIME,
        coverage JSON NOT NULL, result JSON, replay_of VARCHAR, client_token_id INTEGER,
        FOREIGN KEY(replay_of) REFERENCES requests (id) ON DELETE SET NULL,
        FOREIGN KEY(client_token_id) REFERENCES client_tokens (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_requests_status ON requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_requests_started_at ON requests (started_at)",
    "CREATE INDEX IF NOT EXISTS ix_requests_client_token_id ON requests (client_token_id)",
    """CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER NOT NULL PRIMARY KEY, request_id VARCHAR NOT NULL,
        provider VARCHAR NOT NULL, account_id INTEGER, outcome VARCHAR NOT NULL,
        kind VARCHAR, latency_ms FLOAT, error TEXT, created_at DATETIME NOT NULL,
        FOREIGN KEY(request_id) REFERENCES requests (id) ON DELETE CASCADE,
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_attempts_request_id ON attempts (request_id)",
    """CREATE TABLE IF NOT EXISTS handles (
        handle VARCHAR NOT NULL PRIMARY KEY, canonical_ids JSON NOT NULL,
        url TEXT, title TEXT, metadata_json JSON NOT NULL,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS handle_aliases (
        alias VARCHAR NOT NULL PRIMARY KEY, target VARCHAR, ambiguous BOOLEAN NOT NULL,
        FOREIGN KEY(target) REFERENCES handles (handle) ON DELETE CASCADE
    )""",
)


def upgrade(connection: Connection) -> None:
    for statement in DDL:
        connection.execute(text(statement))
