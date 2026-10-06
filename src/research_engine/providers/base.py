"""Small provider protocol shared by REST, local and hosted MCP transports."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

import httpx

from research_engine.server.schemas import Document as Document, Hit as Hit, Result as Result


class Capability(StrEnum):
    WEB_SEARCH = "web_search"
    NEWS_SEARCH = "news_search"
    WEB_READ = "web_read"
    SITE_MAP = "site_map"
    SITE_CRAWL = "site_crawl"
    PAPER_SEARCH = "paper_search"
    PAPER_READ = "paper_read"
    PAPER_METADATA = "paper_metadata"
    PAPER_RELATED = "paper_related"
    CITATION_VERIFY = "citation_verify"
    CITATION_GRAPH = "citation_graph"
    EDITORIAL_CHECK = "editorial_check"
    SYSTEMATIC_REVIEW = "systematic_review"
    DEEP_LITERATURE_SEARCH = "deep_literature_search"
    DEVELOPER_SEARCH = "developer_search"
    REPO_SEARCH = "repo_search"


ASYNC_CAPABILITIES = frozenset({Capability.SITE_CRAWL, Capability.SYSTEMATIC_REVIEW,
                                Capability.DEEP_LITERATURE_SEARCH})
SEARCH_CAPABILITIES = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH,
    Capability.PAPER_SEARCH, Capability.PAPER_RELATED, Capability.DEVELOPER_SEARCH,
    Capability.REPO_SEARCH})
Request = dict[str, Any]


class ErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    EXHAUSTED = "exhausted"
    AUTH = "auth"
    PLAN = "plan"
    TRANSIENT = "transient"
    BAD_REQUEST = "bad_request"
    TARGET = "target"


class ProviderError(Exception):
    def __init__(self, kind: str | ErrorKind, message: str, *, retry_after: float | None = None,
                 reset_at: datetime | None = None, block_capability: bool = True,
                 ambiguous_start: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.kind = ErrorKind(kind)
        self.retry_after = retry_after
        self.reset_at = reset_at
        self.block_capability = block_capability
        self.ambiguous_start = ambiguous_start
        self.status_code = status_code


@dataclass
class CallContext:
    client: httpx.AsyncClient
    credentials: dict[str, Any]
    account_id: int
    provider: str
    deadline: float
    options: dict[str, Any] = field(default_factory=dict)
    mcp_manager: Any = None
    limiter: Any = None
    quota_group: str | None = None
    _targets: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def credential(self) -> dict[str, Any]:
        return self.credentials

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        from research_engine.providers.http import request

        guarded = url in self._targets
        retries = kwargs.pop("retries", None)
        if retries is None:
            retries = 1 if method.upper() in {"GET", "HEAD", "OPTIONS"} and not guarded else 0

        async def send() -> httpx.Response:
            if guarded:
                from research_engine.providers.url_guard import safe_fetch
                return await safe_fetch(self.client, url, deadline=self.deadline, method=method, **kwargs)
            return await request(self.client, method, url, deadline=self.deadline, retries=0, **kwargs)

        for attempt in range(retries + 1):
            try:
                if self.limiter is None:
                    return await send()
                async with self.limiter.admit(
                    self.provider, self.account_id, quota_group=self.quota_group,
                    rate_limit_rps=self.options.get("rate_limit_rps"),
                    concurrency=self.options.get("concurrency"), deadline=self.deadline,
                ):
                    response = await send()
                if response.status_code not in {408, 500, 502, 503, 504} or attempt == retries:
                    return response
            except ProviderError as error:
                if attempt == retries or error.kind != ErrorKind.TRANSIENT:
                    raise
            await asyncio.sleep(min(0.25 * 2 ** attempt, self.remaining()))
        raise ProviderError(ErrorKind.TRANSIENT, "Provider request failed")

    async def validate_url(self, target: str) -> Any:
        from research_engine.providers.url_guard import validate_url
        guarded = await validate_url(target)
        self._targets[target] = guarded
        return guarded


@dataclass
class JobUpdate:
    status: str
    result: Result | dict[str, Any] | None = None
    error: str | None = None
    poll_after_s: float | None = None


class Provider:
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()
    keyless: ClassVar[bool] = False
    options: ClassVar[dict[str, Any]] = {}
    poll_interval_s: ClassVar[float] = 15

    async def call(self, cap: Capability, req: Request, ctx: CallContext) -> Result:
        raise NotImplementedError

    async def start(self, cap: Capability, req: Request, ctx: CallContext) -> str:
        raise ProviderError(ErrorKind.PLAN, "This provider has no async implementation")

    async def poll(self, ctx: CallContext, ref: str) -> JobUpdate:
        raise ProviderError(ErrorKind.PLAN, "This provider has no polling implementation")

    async def cancel(self, ctx: CallContext, ref: str) -> bool:
        return False
