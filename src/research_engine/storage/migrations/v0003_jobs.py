"""Frozen v0003 recoverable upstream jobs schema."""

from sqlalchemy import Connection, text


DDL = """CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR NOT NULL PRIMARY KEY, capability VARCHAR NOT NULL, args JSON NOT NULL,
    provider VARCHAR NOT NULL, account_id INTEGER NOT NULL, upstream_job_ref TEXT NOT NULL,
    status VARCHAR NOT NULL, client_token_id INTEGER, result JSON,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, last_error TEXT,
    poll_after_s FLOAT NOT NULL, cancelled_upstream BOOLEAN,
    FOREIGN KEY(provider) REFERENCES providers (name),
    FOREIGN KEY(account_id) REFERENCES accounts (id),
    FOREIGN KEY(client_token_id) REFERENCES client_tokens (id)
)"""


def upgrade(connection: Connection) -> None:
    connection.execute(text(DDL))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status)"))
