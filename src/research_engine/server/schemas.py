"""Provider and capability-specific output records; no inferred evidence quality."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Hit(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str
    id: str | None = None
    account: int | None = None
    rank: int = 1
    title: str = ""
    url: str = ""
    ids: dict[str, str] = Field(default_factory=dict)
    snippet: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    published: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    handle: str = ""
    url: str = ""
    text: str
    source: str
    kind: Literal["fulltext", "abstract", "page", "code"] = "page"
    next_cursor: str | None = None


class Result(BaseModel):
    """Adapters populate only their capability payload, retaining raw attribution."""

    model_config = ConfigDict(extra="allow")
    hits: list[Hit] = Field(default_factory=list)
    document: Document | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    per_id_coverage: dict[str, Any] = Field(default_factory=dict)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    checks: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    claim_evidence: list[dict[str, Any]] = Field(default_factory=list)
    citation_tallies: list[dict[str, Any]] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    quota_remaining: float | None = None
    quota_reset_at: str | None = None
    cursor: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Failure(BaseModel):
    p: str
    reason: str


class Coverage(BaseModel):
    ok: list[str] = Field(default_factory=list)
    failed: list[Failure] = Field(default_factory=list)
    skipped: list[Failure] = Field(default_factory=list)


class Envelope(BaseModel):
    status: Literal["complete", "partial"]
    coverage: Coverage
    request_id: str


class SearchOutput(Envelope):
    items: list[dict[str, Any]]
    per_seed_coverage: dict[str, list[dict[str, Any]]] | None = None
    recency: dict[str, Any] | None = None


class ReadOutput(Envelope):
    document: Document | None


class MapOutput(Envelope):
    urls: list[str]


class MetadataOutput(Envelope):
    records: list[dict[str, Any]]
    not_found: list[str]
    per_id_coverage: dict[str, Any]


class GraphOutput(Envelope):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    truncated: bool


class EditorialOutput(Envelope):
    checks: list[dict[str, Any]]


class VerifyOutput(Envelope):
    bibliographic: Literal["match", "mismatch", "conflict", "unknown"]
    sources: list[dict[str, Any]]
    claim_evidence: list[dict[str, Any]] = Field(default_factory=list)
    citation_tallies: list[dict[str, Any]] = Field(default_factory=list)


class JobOutput(BaseModel):
    job_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    poll_after_s: float = 15
    result: dict[str, Any] | None = None
    last_error: str | None = None
    request_id: str | None = None


ProviderHit = Hit
ProviderResult = Result


# Workflow tools that dispatch to several capabilities share one output schema;
# fields of the operation that did not run are absent.
class ExploreOutput(Envelope):
    records: list[dict[str, Any]] | None = None
    not_found: list[str] | None = None
    per_id_coverage: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None
    per_seed_coverage: dict[str, list[dict[str, Any]]] | None = None
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None
    truncated: bool | None = None


class VerifyWorkflowOutput(Envelope):
    bibliographic: Literal["match", "mismatch", "conflict", "unknown"] | None = None
    sources: list[dict[str, Any]] | None = None
    claim_evidence: list[dict[str, Any]] | None = None
    citation_tallies: list[dict[str, Any]] | None = None
    checks: list[dict[str, Any]] | None = None


class SiteOutput(BaseModel):
    """A site map envelope, or the job envelope of a started crawl."""

    status: str
    request_id: str | None = None
    coverage: Coverage | None = None
    urls: list[str] | None = None
    job_id: str | None = None
    poll_after_s: float | None = None
    result: dict[str, Any] | None = None
    last_error: str | None = None
