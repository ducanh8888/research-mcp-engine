"""C33 evidence-coverage contracts through authenticated, real HTTP MCP calls.

Every upstream here is a local fixture or HTTPX mock. A failing assertion against
an older router documents the missing behavior; it is not a live-provider check.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError as ClientToolError
from sqlalchemy import select

from research_engine.providers.base import Capability, ErrorKind, Provider, ProviderError
from research_engine.providers.omniroute import OmniRouteConnection, OmniRouteProvider
from research_engine.providers.scholar.crossref import CrossrefProvider
from research_engine.providers.scholar.openalex import OpenAlexProvider
from research_engine.router.execute import Engine
from research_engine.server.app import create_app
from research_engine.server.schemas import Document, Hit, Result
from research_engine.storage.db import (
    Account,
    Attempt,
    Database,
    ProviderRow,
    QueryCache,
    Routing,
    create_client_token,
)
from public_api import to_public
from test_mcp_e2e import Runtime, make_settings, run_http_app


SEARCH_ARGS = {
    "web_search": {"query": "fixture evidence"},
    "news_search": {"query": "fixture evidence"},
    "paper_search": {"query": "fixture evidence"},
    "paper_related": {"seeds": ["10.1234/seed"], "mode": "similar"},
    "developer_search": {"query": "fixture evidence"},
    "repo_search": {"query": "fixture evidence"},
}


class EvidenceSource(Provider):
    """A routed source with observable calls, not a replacement for the MCP path."""

    def __init__(
        self,
        name: str,
        cap: Capability,
        result: Result | None = None,
        *,
        error: ProviderError | BaseException | None = None,
        pause: float = 0,
    ):
        self.name, self.capabilities = name, frozenset({cap})
        self.result, self.error, self.pause = result or Result(), error, pause
        self.calls: list[int] = []
        self.cancelled = threading.Event()

    async def call(self, cap, req, ctx):
        assert cap in self.capabilities
        self.calls.append(ctx.account_id)
        try:
            if self.pause:
                await asyncio.sleep(self.pause)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.error is not None:
            raise self.error
        return self.result


class MeetingSource(EvidenceSource):
    """Both sources must enter before either completes; first-success cannot pass."""

    def __init__(self, name: str, cap: Capability, meeting: asyncio.Event, result: Result):
        super().__init__(name, cap, result)
        self.meeting = meeting
        self.entered = threading.Event()

    async def call(self, cap, req, ctx):
        self.entered.set()
        await asyncio.wait_for(self.meeting.wait(), timeout=1.5)
        return await super().call(cap, req, ctx)


class LastMeetingSource(MeetingSource):
    async def call(self, cap, req, ctx):
        self.meeting.set()
        return await super().call(cap, req, ctx)


class ConcurrentPeer(EvidenceSource):
    """Fails unless the other source starts before this source returns."""

    def __init__(
        self, name: str, cap: Capability, started: threading.Event, result: Result, entered: threading.Event
    ):
        super().__init__(name, cap, result)
        self.started, self.entered = started, entered

    async def call(self, cap, req, ctx):
        self.entered.set()
        await asyncio.wait_for(asyncio.to_thread(self.started.wait, 1.5), timeout=2)
        assert self.started.is_set(), "Independent source did not start concurrently"
        return await super().call(cap, req, ctx)


class AccountSource(EvidenceSource):
    def __init__(self, name: str, errors: dict[int, BaseException], result: Result):
        super().__init__(name, Capability.WEB_SEARCH, result)
        self.errors = errors

    async def call(self, cap, req, ctx):
        self.calls.append(ctx.account_id)
        if error := self.errors.get(ctx.account_id):
            raise error
        return self.result


class AmbiguousStart(EvidenceSource):
    def __init__(self, name: str, *, ambiguous: bool):
        super().__init__(name, Capability.SITE_CRAWL)
        self.ambiguous = ambiguous

    async def start(self, cap, req, ctx):
        self.calls.append(ctx.account_id)
        if self.ambiguous:
            raise ProviderError(ErrorKind.TRANSIENT, "start may have succeeded", ambiguous_start=True)
        return "fixture-job-ref"


@asynccontextmanager
async def mcp_fixture(
    tmp_path: Path,
    providers: list[Provider],
    routes: dict[str, list[str]],
    *,
    modes: dict[str, str] | None = None,
    counts: dict[str, int] | None = None,
    upstream=None,
    bridge: bool = False,
    rerank: dict | None = None,
):
    """Isolated SQLite + ephemeral loopback uvicorn + real authenticated MCP transport."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    settings = make_settings(tmp_path / "runtime", sock.getsockname()[1])
    registry = {provider.name: provider for provider in providers}
    if bridge:
        registry["omniroute"] = OmniRouteConnection()
    db = Database(settings.database_path)
    db.initialize(registry)
    with db.session() as session:
        for name, provider in registry.items():
            if name == "omniroute":
                row = session.get(ProviderRow, name)
                row.options = {
                    "base_url": "https://gateway.example/v1",
                    "rerank": rerank or {"enabled": False},
                }
            existing = list(session.scalars(select(Account).where(Account.provider == name)))
            for position in range(len(existing), (counts or {}).get(name, 1)):
                session.add(
                    Account(provider=name, label=f"fixture-{position}", priority=position, credential="ok")
                )
        for capability, names in routes.items():
            session.merge(
                Routing(capability=capability, mode=(modes or {}).get(capability, "fanout"), providers=names)
            )
    token_id, token = create_client_token(db, "C33 HTTP fixture")
    db.close()
    handler = upstream or (lambda request: pytest.fail(f"Unexpected upstream call to {request.url}"))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            engine = Engine(settings, providers=registry, http_client=http)
            try:
                if bridge:
                    with engine.db.session() as session:
                        connection = session.scalar(select(Account).where(Account.provider == "omniroute"))
                        connection_id = connection.id
                    engine.secrets.set(connection_id, {"api_key": "fixture-service-key"})
                app = create_app(settings, engine=engine)
                runtime = Runtime(f"http://127.0.0.1:{settings.port}", token, token_id, settings, app)
                with run_http_app(app, sock):
                    yield runtime, engine
            finally:
                await engine.stop()
    finally:
        sock.close()


async def call(runtime: Runtime, tool: str, args: dict) -> tuple[dict, bool]:
    tool, args = to_public(tool, args)
    async with runtime.client() as client:
        try:
            response = await client.call_tool(tool, args)
        except ClientToolError as error:
            # FastMCP raises for isError=True before exposing structured content.
            # The JSON error here is the actual MCP tool response text.
            payload = json.loads(str(error))
            assert payload["error"]["code"]
            return payload, True
    payload = json.loads(response.content[0].text)
    assert payload == response.structured_content
    return payload, bool(response.is_error)


@pytest.mark.parametrize("mode", ["similar", "citing", "cited"])
async def test_public_related_modes_preserve_each_seed_and_partial_evidence(tmp_path: Path, mode: str):
    seeds = ["W1", "W2"]

    def openalex(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works":
            seed = request.url.params["filter"].rsplit(":", 1)[-1]
            return httpx.Response(200, json={"results": [{"id": f"https://openalex.org/C{seed[-1]}",
                                                            "display_name": "Citing result"}]})
        work = request.url.path.rsplit("/", 1)[-1]
        if work in seeds:
            return httpx.Response(200, json={"id": f"https://openalex.org/{work}",
                "related_works": [f"https://openalex.org/W9{work[-1]}"],
                "referenced_works": [f"https://openalex.org/W8{work[-1]}"]})
        return httpx.Response(200, json={"id": f"https://openalex.org/{work}",
                                          "display_name": f"Neighbor {work}"})

    sources = [OpenAlexProvider(), EvidenceSource("unavailable", Capability.PAPER_RELATED,
                                                  error=ProviderError(ErrorKind.TRANSIENT, "fixture outage"))]
    async with mcp_fixture(tmp_path, sources, {"paper_related": ["openalex", "unavailable"]},
                           upstream=openalex) as (runtime, _):
        payload, failed = await call(runtime, "paper_related", {"seeds": seeds, "mode": mode, "limit": 4})
    assert not failed and payload["status"] == "partial"
    assert_coverage(payload, {"openalex"}, failed={"unavailable"})
    assert set(payload["per_seed_coverage"]) == set(seeds)
    assert all(payload["per_seed_coverage"][seed][0]["found"] for seed in seeds)
    assert {source["related_seed"] for item in payload["items"] for source in item["providers"]} == set(seeds)
    assert all(source["related_mode"] == mode for item in payload["items"]
               for source in item["providers"])


@pytest.mark.parametrize("citation,reason", [
    ("10.1234/verified", "found"),
    ("Author. A Precisely Matching Paper. (2021). Journal.", "found"),
    ("A Precisely Matching Paper. 2021", "unknown"),
])
async def test_public_metadata_resolves_ids_and_preserves_citation_ambiguity(
    tmp_path: Path, citation: str, reason: str,
):
    def crossref(request: httpx.Request) -> httpx.Response:
        def record(identifier: str) -> dict:
            return {"DOI": identifier, "title": ["A Precisely Matching Paper"],
                    "published": {"date-parts": [[2021]]}}

        if request.url.path.startswith("/works/"):
            return httpx.Response(200, json={"message": record("10.1234/verified")})
        records = [record("10.1234/verified")]
        if citation == "A Precisely Matching Paper. 2021":
            records.append(record("10.1234/other"))
        return httpx.Response(200, json={"message": {"items": records}})

    source = CrossrefProvider()
    async with mcp_fixture(tmp_path, [source], {"paper_metadata": ["crossref"]},
                           upstream=crossref) as (runtime, _):
        args = {"ids": [citation]} if citation.startswith("10.") else {"citation": citation}
        payload, failed = await call(runtime, "paper_metadata", args)
    assert not failed and payload["per_id_coverage"][citation]["status"] == reason
    if reason == "found":
        assert payload["records"][0]["ids"]["doi"] == "10.1234/verified"
    else:
        assert payload["records"] == [] and citation not in payload["not_found"]
        assert payload["per_id_coverage"][citation]["sources"][0]["reason"] == "ambiguous"


def hit(provider: str, name: str, *, rank: int = 1, doi: str | None = None) -> Hit:
    return Hit(
        provider=provider,
        rank=rank,
        title=name,
        url=f"https://example.org/{name}",
        ids={"doi": doi} if doi else {},
    )


def assert_coverage(
    payload: dict, ok: set[str], *, failed: set[str] = frozenset(), skipped: set[str] = frozenset()
) -> None:
    assert set(payload["coverage"]["ok"]) == ok
    assert {entry["p"] for entry in payload["coverage"]["failed"]} == failed
    assert {entry["p"] for entry in payload["coverage"]["skipped"]} == skipped


@pytest.mark.parametrize("capability", list(SEARCH_ARGS))
@pytest.mark.parametrize("first_zero_hits", [False, True], ids=["fast-success", "valid-zero-hits"])
async def test_every_search_collects_all_peers_even_on_legacy_sequential_route(
    tmp_path: Path,
    capability: str,
    first_zero_hits: bool,
    monkeypatch,
):
    # Public code_search scope=docs asks for documentation; these fixtures stand in
    # for developer-index sources that can honor that kind.
    monkeypatch.setattr("research_engine.router.requests.DEVELOPER_KIND_PROVIDERS",
                        {"firecrawl", "first", "second", "third"})
    cap = Capability(capability)
    first_started = threading.Event()
    second_started, third_started = threading.Event(), threading.Event()
    first_result = Result(hits=[] if first_zero_hits else [hit("first", "a")])

    class ImmediateSource(EvidenceSource):
        async def call(self, cap, req, ctx):
            first_started.set()
            if not first_zero_hits:
                await asyncio.wait_for(asyncio.to_thread(second_started.wait, 1.5), timeout=2)
                assert second_started.is_set(), "Fast result prevented concurrent fanout"
            return await super().call(cap, req, ctx)

    first = ImmediateSource("first", cap, first_result)
    second = ConcurrentPeer("second", cap, third_started, Result(hits=[hit("second", "b")]), second_started)
    third = ConcurrentPeer("third", cap, second_started, Result(hits=[hit("third", "c")]), third_started)
    # A successful first source must overlap another; for valid zero hits the
    # remaining two peers must overlap even if the first returns immediately.
    providers = [first, second, third]
    names = [source.name for source in providers]
    # An operator's provider selection is preserved, but sequential SEARCH mode
    # is legacy configuration that must not turn into first-success fallback.
    async with mcp_fixture(tmp_path, providers, {capability: names}, modes={capability: "sequential"}) as (
        runtime,
        _,
    ):
        payload, error = await call(runtime, capability, SEARCH_ARGS[capability])
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, set(names))
    assert first_started.is_set() and second.entered.is_set() and third.entered.is_set()
    assert [len(source.calls) for source in providers] == [1, 1, 1]
    assert {item["title"] for item in payload["items"]} == (
        {"b", "c"} if first_zero_hits else {"a", "b", "c"}
    )
    assert all(item["providers"][0]["p"] in names for item in payload["items"])


async def test_search_merges_exact_ids_and_plain_rrf_from_every_source(tmp_path: Path):
    a = EvidenceSource(
        "alpha",
        Capability.PAPER_SEARCH,
        Result(hits=[hit("alpha", "shared", doi="10.1234/shared"), hit("alpha", "only-a", rank=2)]),
    )
    b = EvidenceSource(
        "beta",
        Capability.PAPER_SEARCH,
        Result(hits=[hit("beta", "same-paper-new-url", doi="10.1234/shared"), hit("beta", "only-b", rank=2)]),
    )
    async with mcp_fixture(tmp_path, [a, b], {"paper_search": [a.name, b.name]}) as (runtime, _):
        payload, error = await call(runtime, "paper_search", {"query": "same evidence"})
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, {"alpha", "beta"})
    assert len(payload["items"]) == 3
    shared = payload["items"][0]
    assert shared["handle"] == "doi:10.1234/shared"
    assert shared["score"] == pytest.approx(2 / 61)
    assert {source["p"] for source in shared["providers"]} == {"alpha", "beta"}
    assert all(item["score"] == pytest.approx(1 / 62) for item in payload["items"][1:])


@pytest.mark.parametrize("failure", ["error", "unavailable", "deadline"])
async def test_reduced_coverage_is_partial_and_never_cached(tmp_path: Path, failure: str):
    good = EvidenceSource("good", Capability.WEB_SEARCH, Result(hits=[hit("good", "survivor")]))
    bad = EvidenceSource(
        "bad",
        Capability.WEB_SEARCH,
        error=ProviderError(ErrorKind.RATE_LIMITED, "fixture 429", retry_after=60)
        if failure == "error"
        else None,
        pause=6 if failure == "deadline" else 0,
    )
    async with mcp_fixture(tmp_path, [good, bad], {"web_search": [good.name, bad.name]}) as (runtime, engine):
        if failure == "unavailable":
            with engine.db.session() as session:
                session.scalar(select(Account).where(Account.provider == "bad")).enabled = False
        args = {"query": "partial evidence", "deadline_s": 5 if failure == "deadline" else 40}
        if failure == "deadline":
            # Slow source is already in flight before the shared request timer
            # runs out; the successful source must not be cancelled with it.
            assert bad.pause > args["deadline_s"]
        first, error = await call(runtime, "web_search", args)
        assert not error and first["status"] == "partial"
        assert_coverage(
            first,
            {"good"},
            failed={"bad"} if failure != "unavailable" else set(),
            skipped={"bad"} if failure == "unavailable" else set(),
        )
        assert [item["title"] for item in first["items"]] == ["survivor"]
        assert first["items"][0]["providers"][0]["p"] == "good"
        with engine.db.session() as session:
            assert list(session.scalars(select(QueryCache))) == []
        if failure != "deadline":
            second, error = await call(runtime, "web_search", args)
            assert not error and second["status"] == "partial"
            assert len(good.calls) == 2 and first["request_id"] != second["request_id"]
        else:
            assert bad.cancelled.is_set()


async def test_no_success_is_a_public_no_provider_error_not_empty_success(tmp_path: Path):
    failed = EvidenceSource(
        "failed", Capability.WEB_SEARCH, error=ProviderError(ErrorKind.TARGET, "fixture target")
    )
    unavailable = EvidenceSource("unavailable", Capability.WEB_SEARCH)
    async with mcp_fixture(
        tmp_path, [failed, unavailable], {"web_search": [failed.name, unavailable.name]}
    ) as (runtime, engine):
        with engine.db.session() as session:
            session.scalar(select(Account).where(Account.provider == "unavailable")).enabled = False
        payload, is_error = await call(runtime, "web_search", {"query": "no sources"})
        with engine.db.session() as session:
            assert not list(session.scalars(select(QueryCache)))
    assert is_error and payload["error"]["code"] == "NO_PROVIDER_AVAILABLE"
    assert payload["request_id"]
    assert payload["coverage"]["ok"] == []
    assert {row["p"] for row in payload["coverage"]["failed"]} == {"failed"}
    assert {row["p"] for row in payload["coverage"]["skipped"]} == {"unavailable"}
    assert len(failed.calls) == 1 and unavailable.calls == []


def bridge_search(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.url.path == "/v1/search"
    assert request.headers["Authorization"] == "Bearer fixture-service-key"
    assert body["provider"] == "exa-search" and body["strict_filters"] is True
    return httpx.Response(
        200,
        json={
            "provider": "exa-search",
            "results": [
                {
                    "title": "bridged",
                    "url": "https://example.org/bridged",
                    "position": 1,
                    "citation": {"provider": "exa-search", "rank": 1},
                }
            ],
            "errors": [],
        },
    )


async def test_bridge_failure_keeps_specialist_but_never_uses_unrouted_direct_adapter(tmp_path: Path):
    bridge = OmniRouteProvider("omni:exa-search", "exa-search", {Capability.WEB_SEARCH})
    direct = EvidenceSource("exa", Capability.WEB_SEARCH, Result(hits=[hit("exa", "forbidden")]))
    specialist = EvidenceSource(
        "specialist", Capability.WEB_SEARCH, Result(hits=[hit("specialist", "specialist-paper")])
    )
    requests: list[httpx.Request] = []

    def outage(request):
        requests.append(request)
        raise httpx.ConnectError("fixture outage", request=request)

    async with mcp_fixture(
        tmp_path,
        [bridge, direct, specialist],
        {"web_search": [bridge.name, specialist.name]},
        upstream=outage,
        bridge=True,
    ) as (runtime, _):
        payload, error = await call(runtime, "web_search", {"query": "bridge outage"})
    assert not error and payload["status"] == "partial"
    assert_coverage(payload, {"specialist"}, failed={bridge.name})
    assert [item["title"] for item in payload["items"]] == ["specialist-paper"]
    assert direct.calls == [] and len(requests) == 1
    assert json.loads(requests[0].content)["provider"] == "exa-search"
    assert "fixture-service-key" not in json.dumps(payload)


@pytest.mark.parametrize("aliases", [("exa", "omni:exa-search"), ("scite_rest", "scite_mcp")])
async def test_one_actual_provider_operation_cannot_get_two_search_votes(
    tmp_path: Path, aliases: tuple[str, str]
):
    direct = EvidenceSource(
        aliases[0], Capability.PAPER_SEARCH if aliases[0].startswith("scite") else Capability.WEB_SEARCH
    )
    duplicate = (
        EvidenceSource(aliases[1], Capability.PAPER_SEARCH)
        if aliases[0].startswith("scite")
        else OmniRouteProvider("omni:exa-search", "exa-search", {Capability.WEB_SEARCH})
    )
    cap = Capability.PAPER_SEARCH if aliases[0].startswith("scite") else Capability.WEB_SEARCH
    direct.result = Result(hits=[hit(aliases[0], "shared")])
    if isinstance(duplicate, EvidenceSource):
        duplicate.result = Result(hits=[hit(aliases[1], "shared")])
    else:
        direct.result.hits[0].url = "https://example.org/bridged"
    args = SEARCH_ARGS[cap.value]
    upstream_requests: list[httpx.Request] = []

    def upstream(request):
        upstream_requests.append(request)
        return bridge_search(request)

    async with mcp_fixture(
        tmp_path,
        [direct, duplicate],
        {cap.value: list(aliases)},
        bridge=isinstance(duplicate, OmniRouteProvider),
        upstream=upstream if isinstance(duplicate, OmniRouteProvider) else None,
    ) as (runtime, _):
        payload, error = await call(runtime, cap.value, args)
    # The operation is ONE real source even when accessed via two interfaces.
    # Rejection or canonical one-bridge normalization are both acceptable.
    if error:
        assert payload["error"]["code"] in {"INVALID_INPUT", "INTERNAL", "NO_PROVIDER_AVAILABLE"}
        assert direct.calls == [] and payload.get("coverage", {}).get("ok", []) == []
        assert upstream_requests == []
    else:
        assert payload["status"] == "complete"
        assert len(payload["coverage"]["ok"]) == 1
        assert len(payload["items"]) == 1
        assert payload["items"][0]["score"] == pytest.approx(1 / 61)
        assert len(payload["items"][0]["providers"]) == 1
        if isinstance(duplicate, OmniRouteProvider):
            assert direct.calls == []
            assert payload["coverage"]["ok"] == [duplicate.name]
            assert payload["items"][0]["providers"][0]["transport"] == "omniroute"
            assert len(upstream_requests) == 1


@pytest.mark.parametrize(
    "kind,retry",
    [
        (ErrorKind.AUTH, True),
        (ErrorKind.RATE_LIMITED, True),
        (ErrorKind.EXHAUSTED, True),
        (ErrorKind.PLAN, False),
        (ErrorKind.BAD_REQUEST, False),
        (ErrorKind.TARGET, False),
        (ErrorKind.TRANSIENT, False),
        ("internal", False),
    ],
)
async def test_account_retry_is_only_auth_rate_or_quota_availability(
    tmp_path: Path,
    kind: ErrorKind | str,
    retry: bool,
):
    peer = EvidenceSource("peer", Capability.WEB_SEARCH, Result(hits=[hit("peer", "peer-hit")]))
    owner = AccountSource("pooled", {}, Result(hits=[hit("pooled", "pool-hit")]))
    async with mcp_fixture(
        tmp_path, [owner, peer], {"web_search": [owner.name, peer.name]}, counts={owner.name: 2}
    ) as (runtime, engine):
        with engine.db.session() as session:
            ids = list(
                session.scalars(
                    select(Account.id)
                    .where(Account.provider == owner.name)
                    .order_by(Account.priority, Account.id)
                )
            )
        owner.errors[ids[0]] = (
            RuntimeError("fixture internal error")
            if kind == "internal"
            else ProviderError(kind, f"fixture {kind}", retry_after=60)
        )
        payload, error = await call(runtime, "web_search", {"query": "account selection"})
        with engine.db.session() as session:
            attempts = list(
                session.scalars(select(Attempt).where(Attempt.provider == owner.name).order_by(Attempt.id))
            )
    assert not error and len(peer.calls) == 1
    assert owner.calls == (ids if retry else ids[:1])
    assert len(attempts) == (2 if retry else 1)
    assert attempts[0].outcome in {"failed", "internal"}
    if retry:
        assert payload["status"] == "complete"
        assert_coverage(payload, {"pooled", "peer"})
        assert {item["title"] for item in payload["items"]} == {"pool-hit", "peer-hit"}
        assert payload["items"][0]["score"] == pytest.approx(1 / 61)
        assert (
            len(
                [
                    source
                    for item in payload["items"]
                    for source in item["providers"]
                    if source["p"] == "pooled"
                ]
            )
            == 1
        )
    else:
        assert payload["status"] == "partial"
        assert_coverage(payload, {"peer"}, failed={"pooled"})
        assert [item["title"] for item in payload["items"]] == ["peer-hit"]


async def test_shared_quota_failure_makes_second_account_ineligible(tmp_path: Path):
    owner = AccountSource("pooled", {}, Result(hits=[hit("pooled", "wrong")]))
    peer = EvidenceSource("peer", Capability.WEB_SEARCH, Result(hits=[hit("peer", "safe")]))
    async with mcp_fixture(
        tmp_path, [owner, peer], {"web_search": [owner.name, peer.name]}, counts={owner.name: 2}
    ) as (runtime, engine):
        with engine.db.session() as session:
            accounts = list(
                session.scalars(
                    select(Account)
                    .where(Account.provider == owner.name)
                    .order_by(Account.priority, Account.id)
                )
            )
            ids = [account.id for account in accounts]
            for account in accounts:
                account.quota_group = "shared-fixture-pool"
        owner.errors[ids[0]] = ProviderError(ErrorKind.EXHAUSTED, "fixture quota", retry_after=60)
        payload, error = await call(runtime, "web_search", {"query": "shared limit"})
    assert not error and payload["status"] == "partial"
    assert_coverage(payload, {"peer"}, failed={"pooled"})
    assert owner.calls == ids[:1]


@pytest.mark.parametrize(
    "capability,args",
    [
        ("citation_verify", {"citation": "10.1234/shared"}),
        ("citation_graph", {"seeds": ["10.1234/shared"]}),
        ("editorial_check", {"ids": ["10.1234/shared"]}),
    ],
)
async def test_typed_assertions_fan_out_without_rrf(tmp_path: Path, capability: str, args: dict):
    cap = Capability(capability)
    if cap == Capability.CITATION_VERIFY:
        first_result = Result(
            verification={"bibliographic": "match", "sources": [{"provider": "first", "verdict": "match"}]},
            claim_evidence=[{"provider": "first", "passage": "reported claim"}],
        )
        second_result = Result(
            verification={
                "bibliographic": "mismatch",
                "sources": [{"provider": "second", "verdict": "mismatch"}],
            },
            citation_tallies=[{"provider": "second", "scope": "all_citations_to_paper", "supporting": 2}],
        )
    elif cap == Capability.CITATION_GRAPH:
        first_result = Result(
            nodes=[
                {
                    "id": "doi:10.1234/shared",
                    "ids": {"doi": "10.1234/shared"},
                    "title": "Paper",
                    "providers": ["first"],
                },
                {"id": "doi:10.1234/a", "ids": {"doi": "10.1234/a"}, "providers": ["first"]},
            ],
            edges=[
                {
                    "source": "doi:10.1234/a",
                    "target": "doi:10.1234/shared",
                    "provider": "first",
                    "relation": "cites",
                }
            ],
        )
        second_result = Result(
            nodes=[
                {
                    "id": "doi:10.1234/shared",
                    "ids": {"doi": "10.1234/shared"},
                    "title": "Paper",
                    "providers": ["second"],
                },
                {"id": "doi:10.1234/a", "ids": {"doi": "10.1234/a"}, "providers": ["second"]},
            ],
            edges=[
                {
                    "source": "doi:10.1234/a",
                    "target": "doi:10.1234/shared",
                    "provider": "second",
                    "relation": "cites",
                }
            ],
        )
    else:
        first_result = Result(checks=[{"provider": "first", "status": "unknown", "notices": []}])
        second_result = Result(
            checks=[
                {
                    "provider": "second",
                    "status": "retracted",
                    "notices": [{"source": "second", "type": "retraction"}],
                }
            ]
        )
    meeting = asyncio.Event()
    first = MeetingSource("first", cap, meeting, first_result)
    second = LastMeetingSource("second", cap, meeting, second_result)
    async with mcp_fixture(
        tmp_path, [first, second], {capability: [first.name, second.name]}, modes={capability: "sequential"}
    ) as (runtime, _):
        payload, error = await call(runtime, capability, args)
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, {"first", "second"})
    assert first.entered.is_set() and second.entered.is_set()
    assert "items" not in payload and "score" not in payload
    if cap == Capability.CITATION_VERIFY:
        assert payload["bibliographic"] == "conflict"
        assert {source["verdict"] for source in payload["sources"]} == {"match", "mismatch"}
        assert payload["claim_evidence"][0]["provider"] == "first"
        assert payload["citation_tallies"][0]["scope"] == "all_citations_to_paper"
    elif cap == Capability.CITATION_GRAPH:
        assert not payload["truncated"] and len(payload["nodes"]) == 2
        assert len(payload["edges"]) == 1
        assert {row["provider"] for row in payload["edges"][0]["sources"]} == {"first", "second"}
        assert {source["provider"] for node in payload["nodes"] for source in node["sources"]} == {
            "first",
            "second",
        }
        assert {endpoint for edge in payload["edges"] for endpoint in (edge["source"], edge["target"])} <= {
            node["id"] for node in payload["nodes"]
        }
    else:
        assert {check["status"] for check in payload["checks"]} == {"unknown", "retracted"}
        assert {check["provider"] for check in payload["checks"]} == {"first", "second"}


async def test_site_map_aggregates_urls_from_all_sources_not_first_success(tmp_path: Path):
    meeting = asyncio.Event()
    first = MeetingSource(
        "map-a",
        Capability.SITE_MAP,
        meeting,
        Result(urls=["https://example.org/a", "https://example.org/shared"]),
    )
    second = LastMeetingSource(
        "map-b",
        Capability.SITE_MAP,
        meeting,
        Result(urls=["https://example.org/shared", "https://example.org/b"]),
    )
    async with mcp_fixture(
        tmp_path, [first, second], {"site_map": [first.name, second.name]}, modes={"site_map": "sequential"}
    ) as (runtime, _):
        payload, error = await call(runtime, "site_map", {"url": "https://example.org"})
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, {"map-a", "map-b"})
    assert payload["urls"] == ["https://example.org/a", "https://example.org/shared", "https://example.org/b"]
    assert first.entered.is_set() and second.entered.is_set()
    assert "items" not in payload


async def test_source_read_and_unresolved_metadata_keep_sequential_semantics(tmp_path: Path):
    empty = EvidenceSource("empty", Capability.WEB_READ, Result())
    read = EvidenceSource(
        "read",
        Capability.WEB_READ,
        Result(document=Document(url="https://example.org/a", text="Original source text", source="read")),
    )
    async with mcp_fixture(
        tmp_path / "read",
        [empty, read],
        {"web_read": [empty.name, read.name]},
        modes={"web_read": "sequential"},
    ) as (runtime, _):
        payload, error = await call(runtime, "web_read", {"target": "https://example.org/a", "fresh": True})
    assert not error and payload["document"]["text"] == "Original source text"
    assert empty.calls and read.calls
    assert_coverage(payload, {"read"}, failed={"empty"})
    assert payload["status"] == "partial"

    first_reader = EvidenceSource(
        "first-reader",
        Capability.WEB_READ,
        Result(document=Document(url="https://example.org/b", text="First reader text", source="first-reader")),
    )
    unused = EvidenceSource("unused-reader", Capability.WEB_READ, Result())
    async with mcp_fixture(
        tmp_path / "first-read",
        [first_reader, unused],
        {"web_read": [first_reader.name, unused.name]},
        modes={"web_read": "sequential"},
    ) as (runtime, _):
        payload, error = await call(runtime, "web_read", {"target": "https://example.org/b", "fresh": True})
    # An unused later reader is not missing coverage for a usable source document.
    assert not error and payload["status"] == "complete" and not unused.calls
    assert_coverage(payload, {"first-reader"}, skipped={"unused-reader"})

    class MetadataSource(EvidenceSource):
        def __init__(self, name: str, found: str):
            super().__init__(name, Capability.PAPER_METADATA)
            self.found = found
            self.requests: list[list[str]] = []

        async def call(self, cap, req, ctx):
            self.requests.append(req["ids"][:])
            keys = req["ids"]
            return Result(
                records=[{"requested_id": self.found, "id": self.found, "provider": self.name}],
                not_found=[key for key in keys if key != self.found],
                per_id_coverage={key: {"found": key == self.found, "providers": [self.name]} for key in keys},
            )

    first, second = MetadataSource("metadata-a", "A"), MetadataSource("metadata-b", "B")
    async with mcp_fixture(
        tmp_path / "metadata",
        [first, second],
        {"paper_metadata": [first.name, second.name]},
        modes={"paper_metadata": "sequential"},
    ) as (runtime, _):
        payload, error = await call(runtime, "paper_metadata", {"ids": ["A", "B"]})
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, {first.name, second.name})
    assert first.requests == [["A", "B"]] and second.requests == [["B"]]
    assert {record["requested_id"] for record in payload["records"]} == {"A", "B"}
    assert payload["not_found"] == []


async def test_ambiguous_async_start_never_starts_another_provider(tmp_path: Path):
    first, second = AmbiguousStart("start-a", ambiguous=True), AmbiguousStart("start-b", ambiguous=False)
    async with mcp_fixture(
        tmp_path,
        [first, second],
        {"site_crawl": [first.name, second.name]},
        modes={"site_crawl": "sequential"},
    ) as (runtime, _):
        payload, error = await call(runtime, "site_crawl", {"url": "https://example.org"})
    assert error and payload["error"]["code"] == "NO_PROVIDER_AVAILABLE"
    assert "unknown" in payload["error"]["message"].lower()
    assert len(first.calls) == 1 and second.calls == []


@pytest.mark.parametrize(
    "enabled,ranking,expected",
    [
        (False, "none", ["shared", "tail"]),
        (True, "valid", ["tail", "shared"]),
        (True, "invalid", ["shared", "tail"]),
    ],
)
async def test_rerank_only_after_fusion_and_only_with_valid_enabled_output(
    tmp_path: Path,
    enabled: bool,
    ranking: str,
    expected: list[str],
):
    first = EvidenceSource(
        "first", Capability.WEB_SEARCH, Result(hits=[hit("first", "shared"), hit("first", "tail", rank=2)])
    )
    second = EvidenceSource("second", Capability.WEB_SEARCH, Result(hits=[hit("second", "shared")]))
    requests: list[httpx.Request] = []

    def reranker(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/rerank" and body["model"] == "jina/fixture-ranker"
        assert len(body["documents"]) == 2
        assert request.headers["Authorization"] == "Bearer fixture-service-key"
        return httpx.Response(
            200,
            headers={"x-omniroute-provider": "jina", "x-omniroute-model": "fixture-ranker"},
            json={
                "results": [
                    {"index": 1 if ranking == "valid" else 99, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    async with mcp_fixture(
        tmp_path,
        [first, second],
        {"web_search": [first.name, second.name]},
        bridge=True,
        upstream=reranker,
        rerank={"enabled": enabled, "backend": "omniroute", "model": "jina/fixture-ranker"},
    ) as (runtime, _):
        payload, error = await call(runtime, "web_search", {"query": "fused result"})
    assert not error and payload["status"] == "complete"
    assert_coverage(payload, {"first", "second"})
    assert [item["title"] for item in payload["items"]] == expected
    assert len(payload["items"]) == 2
    assert payload["items"][0 if ranking != "valid" else 1]["score"] == pytest.approx(2 / 61)
    assert Counter(
        source["p"] for source in payload["items"][0 if ranking != "valid" else 1]["providers"]
    ) == {"first": 1, "second": 1}
    assert len(requests) == int(enabled)
    if enabled:
        assert payload["rerank"]["status"] == ("applied" if ranking == "valid" else "fallback")
    else:
        assert "rerank" not in payload
