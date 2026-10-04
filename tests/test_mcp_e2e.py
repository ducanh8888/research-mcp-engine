"""Exercise discovery and tool calls over a real Streamable HTTP socket."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from research_engine.config import Settings
from research_engine.providers.base import Capability, ErrorKind, Provider, ProviderError
from research_engine.server.app import create_app
from research_engine.server.schemas import Document, Hit, Result
from research_engine.storage.crypto import hash_password
from research_engine.storage.db import Account, Database, Routing, create_client_token


@dataclass
class Runtime:
    url: str
    token: str
    token_id: int
    settings: Settings
    app: object

    def client(self) -> Client:
        transport = StreamableHttpTransport(
            self.url + "/mcp", headers={"Authorization": f"Bearer {self.token}"}
        )
        return Client(transport, timeout=10, init_timeout=10)


def make_settings(path: Path, port: int) -> Settings:
    path.mkdir(parents=True, exist_ok=True)
    encryption = path / "encryption.key"
    encryption.write_bytes(Fernet.generate_key())
    encryption.chmod(0o600)
    session = path / "admin-session.key"
    session.write_text("temporary-session-secret-for-http-tests-12345", encoding="utf-8")
    session.chmod(0o600)
    origin = f"http://127.0.0.1:{port}"
    return Settings(
        data_dir=path,
        encryption_key_file=encryption,
        admin_session_secret_file=session,
        admin_password_hash=hash_password("temporary-admin-password"),
        trusted_origins=[origin],
        public_base_url=origin,
        port=port,
        max_wait_s=5,
    )


@contextmanager
def run_http_app(app: object, sock: socket.socket):
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=sock.getsockname()[1], log_level="warning",
        access_log=False, lifespan="on",
    ))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            server.run(sockets=[sock])
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if errors:
                raise RuntimeError("HTTP fixture failed to start") from errors[0]
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("HTTP fixture did not start within ten seconds")
            time.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)
            raise RuntimeError("HTTP fixture failed to stop")


@pytest.fixture
def running_server(tmp_path: Path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    settings = make_settings(tmp_path / "runtime", port)
    db = Database(settings.database_path)
    db.initialize()
    token_id, token = create_client_token(db, "HTTP fixture")
    db.close()
    app = create_app(settings)
    runtime = Runtime(f"http://127.0.0.1:{port}", token, token_id, settings, app)
    with run_http_app(app, sock):
        yield runtime


async def test_http_discovery_is_stable_without_provider_accounts(running_server: Runtime):
    async with httpx.AsyncClient() as http:
        health = await http.get(running_server.url + "/health")
    assert health.status_code == 200
    async with running_server.client() as client:
        first = await client.list_tools()
        second = await client.list_tools()
    names = {tool.name for tool in first}
    assert {"web_search", "web_read", "paper_search", "paper_metadata", "citation_verify"} <= names
    assert names == {tool.name for tool in second}
    for tool in first:
        properties = tool.input_schema.get("properties", {})
        assert "provider" not in properties
        assert "account_id" not in properties


class SyntheticSearch(Provider):
    name = "http-fixture-search"
    capabilities = frozenset({Capability.WEB_SEARCH})
    keyless = True

    async def call(self, cap, req, ctx) -> Result:
        return Result(hits=[
            Hit(provider=self.name, account=ctx.account_id, rank=1,
                title="Fixture evidence", url="https://example.org/article?id=1&utm_source=test",
                snippet="A deterministic provenance result."),
            Hit(provider=self.name, account=ctx.account_id, rank=2,
                title="Fixture evidence", url="https://example.org/article?id=1&utm_source=other",
                snippet="A duplicate of the same URL."),
        ])


class RateLimitedSearch(Provider):
    name = "http-fixture-limited"
    capabilities = frozenset({Capability.WEB_SEARCH})
    keyless = True

    async def call(self, cap, req, ctx) -> Result:
        raise ProviderError(ErrorKind.RATE_LIMITED, "Synthetic quota event", retry_after=60)


class FailedRead(Provider):
    name = "http-fixture-bad-read"
    capabilities = frozenset({Capability.WEB_READ})
    keyless = True

    def __init__(self, history):
        self.history = history

    async def call(self, cap, req, ctx) -> Result:
        self.history.append(self.name)
        raise ProviderError(ErrorKind.TRANSIENT, "Synthetic read outage")


class SyntheticRead(Provider):
    name = "http-fixture-read"
    capabilities = frozenset({Capability.WEB_READ})
    keyless = True

    def __init__(self, history):
        self.history = history

    async def call(self, cap, req, ctx) -> Result:
        self.history.append(self.name)
        return Result(document=Document(
            url=req.get("url", req.get("target", "https://example.org/article?id=1")),
            text="Persisted fixture document evidence.", source=self.name,
        ))


async def test_http_failover_partial_coverage_and_handle_after_restart(tmp_path: Path):
    from research_engine.engine import Engine

    history: list[str] = []
    providers = {
        provider.name: provider for provider in (
            SyntheticSearch(), RateLimitedSearch(), FailedRead(history), SyntheticRead(history),
        )
    }
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "durable-runtime", sock.getsockname()[1])
    db = Database(settings.database_path)
    db.initialize(providers)
    with db.session() as session:
        for provider in providers:
            session.add(Account(provider=provider, label="HTTP fixture"))
        session.add(Routing(capability="web_search", mode="fanout",
                            providers=[RateLimitedSearch.name, SyntheticSearch.name]))
        session.add(Routing(capability="web_read", mode="sequential",
                            providers=[FailedRead.name, SyntheticRead.name]))
    token_id, token = create_client_token(db, "persistent HTTP fixture")
    db.close()
    engine = Engine(settings, providers=providers)
    app = create_app(settings, engine=engine)
    runtime = Runtime(f"http://127.0.0.1:{settings.port}", token, token_id, settings, app)
    with run_http_app(app, sock):
        async with runtime.client() as client:
            search = json.loads((await client.call_tool("web_search", {"query": "fixture", "limit": 5})).content[0].text)
            # v3: an attempted 429 is a completed failure; only missed deadlines make results partial.
            assert search["status"] == "complete"
            assert len(search["items"]) == 1
            assert SyntheticSearch.name in search["coverage"]["ok"]
            assert any(row["p"] == RateLimitedSearch.name for row in search["coverage"]["failed"])
            item = search["items"][0]
            handle = item["h"]
            assert handle
            assert item["u"] == "https://example.org/article?id=1"
            read = json.loads((await client.call_tool("web_read", {"target": handle, "fresh": True})).content[0].text)
            assert read["document"]["text"] == "Persisted fixture document evidence."
            assert history[0] == FailedRead.name
            assert history[-1] == SyntheticRead.name
            assert SyntheticRead.name in read["coverage"]["ok"]
            assert any(row["p"] == FailedRead.name for row in read["coverage"]["failed"])

    restart_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    restart_sock.bind(("127.0.0.1", 0))
    restart_sock.listen(128)
    restart_url = f"http://127.0.0.1:{restart_sock.getsockname()[1]}"
    settings = settings.model_copy(update={"port": restart_sock.getsockname()[1],
                                          "public_base_url": restart_url,
                                          "trusted_origins": [restart_url]})
    restarted = create_app(settings, engine=Engine(settings, providers=providers))
    runtime = Runtime(restart_url, token, token_id, settings, restarted)
    with run_http_app(restarted, restart_sock):
        async with runtime.client() as client:
            read = json.loads((await client.call_tool("web_read", {"target": handle})).content[0].text)
        assert read["document"]["text"] == "Persisted fixture document evidence."
        assert read["document"]["handle"] == handle
