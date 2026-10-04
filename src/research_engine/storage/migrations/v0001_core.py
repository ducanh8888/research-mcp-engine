"""P0 bootstrap, providers, requests, and persistent evidence identities."""

from sqlalchemy import Connection


def upgrade(connection: Connection) -> None:
    from research_engine.storage.db import Base
    for name in (
        "meta", "providers", "accounts", "account_secrets", "routing",
        "client_tokens", "requests", "attempts", "handles", "handle_aliases",
    ):
        Base.metadata.tables[name].create(connection, checkfirst=True)
