"""Mapped SQLite records and the single transaction path used by all callers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String,
    Text, create_engine, event, select,
)
from sqlalchemy.engine import URL
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker, synonym
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite drops tzinfo; restore UTC on every load."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Persisted datetimes must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ProviderRow(Base):
    __tablename__ = "providers"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    capabilities: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    option_overrides = synonym("options")


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(ForeignKey("providers.name"), index=True)
    label: Mapped[str] = mapped_column(String, default="default")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quota_group: Mapped[str | None] = mapped_column(String, nullable=True)
    credential: Mapped[str] = mapped_column(String, default="needs_auth")
    cooldown_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cooldown_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    blocked_capabilities: Mapped[dict[str, str]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    quota_remaining: Mapped[float | None] = mapped_column(Float, nullable=True)
    quota_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    quota_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    quota_units: Mapped[str | None] = mapped_column(String, nullable=True)
    quota_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    transient_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    adapter_status: Mapped[str] = mapped_column(String, default="unknown")
    tool_schema_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_schema: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    secrets: Mapped[list[AccountSecret]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )


class AccountSecret(Base):
    __tablename__ = "account_secrets"
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Routing(Base):
    __tablename__ = "routing"
    capability: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, default="fanout")
    providers: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)


class ClientToken(Base):
    __tablename__ = "client_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class RequestRow(Base):
    __tablename__ = "requests"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: "r_" + uuid.uuid4().hex)
    tool: Mapped[str] = mapped_column(String)
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    replay_of: Mapped[str | None] = mapped_column(
        ForeignKey("requests.id", ondelete="SET NULL"), nullable=True
    )
    client_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_tokens.id", ondelete="SET NULL"), nullable=True, index=True
    )
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="Attempt.id"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    request: Mapped[RequestRow] = relationship(back_populates="attempts")


class Handle(Base):
    __tablename__ = "handles"
    handle: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class HandleAlias(Base):
    __tablename__ = "handle_aliases"
    alias: Mapped[str] = mapped_column(String, primary_key=True)
    target: Mapped[str | None] = mapped_column(
        ForeignKey("handles.handle", ondelete="CASCADE"), nullable=True
    )
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    handle = synonym("target")


class QueryCache(Base):
    __tablename__ = "query_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class DocumentCache(Base):
    __tablename__ = "doc_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    blob_ref: Mapped[str] = mapped_column(String)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: "j_" + uuid.uuid4().hex)
    capability: Mapped[str] = mapped_column(String)
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(ForeignKey("providers.name"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    upstream_job_ref: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    client_token_id: Mapped[int | None] = mapped_column(ForeignKey("client_tokens.id"), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_after_s: Mapped[float] = mapped_column(Float, default=15)
    cancelled_upstream: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error = synonym("last_error")


def _cap_name(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _descriptor_value(descriptor: Any, name: str, default: Any) -> Any:
    return descriptor.get(name, default) if isinstance(descriptor, Mapping) else getattr(descriptor, name, default)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {"poolclass": StaticPool} if memory else {}
        self.engine = create_engine(
            URL.create("sqlite+pysqlite", database=":memory:" if memory else str(self.path)),
            connect_args={"check_same_thread": False, "timeout": 10},
            **options,
        )

        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragmas(connection: Any, record: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.Session() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def initialize(
        self, catalog: Mapping[str, Any] | Iterable[Any] | None = None,
        routes: Mapping[str, Any] | None = None,
    ) -> None:
        from research_engine.storage.migrations import run_migrations
        run_migrations(self.engine)
        if catalog is None:
            return
        entries = catalog.items() if isinstance(catalog, Mapping) else ((p.name, p) for p in catalog)
        with self.session() as session:
            for name, descriptor in entries:
                shipped_capabilities = [
                    _cap_name(cap) for cap in _descriptor_value(descriptor, "capabilities", [])
                ]
                provider = session.get(ProviderRow, name)
                if provider is None:
                    session.add(ProviderRow(
                        name=name,
                        options=dict(_descriptor_value(descriptor, "options", {})),
                        capabilities=shipped_capabilities,
                    ))
                    session.flush()
                    if _descriptor_value(descriptor, "keyless", False):
                        session.add(Account(provider=name, label="local", credential="ok"))
                elif provider.capabilities != shipped_capabilities:
                    # Adapter support is a shipped fact. Enabled, options, accounts and
                    # operator routes belong to the operator and must not be overwritten.
                    provider.capabilities = shipped_capabilities
            for capability, route in (routes or {}).items():
                key = _cap_name(capability)
                if session.get(Routing, key) is not None:
                    continue
                mode = _descriptor_value(route, "mode", "fanout")
                provider_names = list(_descriptor_value(route, "providers", []))
                compatible = [
                    name for name in provider_names
                    if (provider := session.get(ProviderRow, name)) is not None
                    and key in provider.capabilities
                ]
                if compatible:
                    session.add(Routing(capability=key, mode=mode, providers=compatible))

    def close(self) -> None:
        self.engine.dispose()


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_client_token(db: Database, label: str) -> tuple[int, str]:
    if not label.strip():
        raise ValueError("Client token label must not be empty")
    raw = "re_" + secrets.token_urlsafe(32)
    with db.session() as session:
        token = ClientToken(label=label.strip(), token_hash=token_hash(raw))
        session.add(token)
        session.flush()
        token_id = token.id
    return token_id, raw


def lookup_token(db: Database, raw: str) -> ClientToken | None:
    if not raw or len(raw) > 4096:
        return None
    with db.session() as session:
        token = session.scalar(select(ClientToken).where(
            ClientToken.token_hash == token_hash(raw), ClientToken.revoked.is_(False)
        ))
        if token is not None:
            token.last_used = utcnow()
        return token
