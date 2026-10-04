"""P3 recoverable native upstream jobs."""

from sqlalchemy import Connection


def upgrade(connection: Connection) -> None:
    from research_engine.storage.db import Base
    Base.metadata.tables["jobs"].create(connection, checkfirst=True)
