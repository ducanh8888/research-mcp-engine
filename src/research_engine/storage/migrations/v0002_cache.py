"""P1 source document and complete query result caches."""

from sqlalchemy import Connection


def upgrade(connection: Connection) -> None:
    from research_engine.storage.db import Base
    for name in ("query_cache", "doc_cache"):
        Base.metadata.tables[name].create(connection, checkfirst=True)
