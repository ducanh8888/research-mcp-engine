"""Nine public research workflows over the engine's internal capabilities.

The public MCP surface is deliberately small and workflow-shaped. Each tool
validates its semantic operation, then dispatches deterministically to one
existing capability; routing, provider selection, fusion and coverage stay in
the engine. Provider names, accounts and transports are never parameters.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from research_engine.router.execute import ToolError
from research_engine.router.requests import DEFAULT_DEADLINE_S
from research_engine.server.auth import client_token_id
from research_engine.server.schemas import (
    ExploreOutput, JobOutput, ReadOutput, SearchOutput, SiteOutput, VerifyWorkflowOutput,
)

INSTRUCTIONS = (
    "Research MCP gathers attributable evidence from many web, academic and developer sources. "
    "Pick the workflow: everyday or current information → search (focus=news for news); open a "
    "result, URL, DOI or handle → read; scholarly literature → paper_search; a known paper's "
    "metadata, related, citing or cited papers → paper_explore; check a citation, a claim or "
    "retraction/editorial notices → verify; programming docs, repositories, code or issues → "
    "code_search; list or crawl a website → site_research; long literature reviews → deep_research; "
    "poll asynchronous work → get_job. Search tools return ranked source pointers (handle, url, "
    "provenance), not full text: for important facts, search then read the source. Web search and "
    "paper search draw on separate evidence pools. Always inspect status and coverage: partial means "
    "some intended sources failed, were skipped (for example they cannot apply a requested filter) "
    "or timed out; returned items are still valid evidence. Provenance attributes a source, it is "
    "not a judgment of truth. Long reads are paginated: pass next_cursor back as cursor. "
    "fresh=true bypasses this server's cache only; upstream services may still serve cached data."
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
STARTS_WORK = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                              open_world_hint=True)
_PAPER_HANDLE = re.compile(r"(?i)^(?:doi|arxiv|pmid|pmcid|openalex|s2):\S+$")
_DOI = re.compile(r"(?i)^(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/\S+$")
_ARXIV = re.compile(r"(?i)^(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v\d+)?$")
_PAPER_HOSTS = frozenset({"doi.org", "dx.doi.org", "arxiv.org", "www.arxiv.org", "export.arxiv.org"})


def _invalid(message: str) -> ToolError:
    return ToolError("INVALID_INPUT", message)


def _reject(operation: str, **given: Any) -> None:
    """Fail before routing when an argument does not belong to the chosen operation."""
    extra = sorted(name for name, value in given.items() if value not in (None, [], ""))
    if extra:
        raise _invalid(f"{', '.join(extra)} cannot be used with {operation}")


def paper_target(target: str) -> bool:
    """Only unambiguous scholarly identifiers default to paper reading."""
    value = target.strip()
    if _PAPER_HANDLE.match(value) or _DOI.match(value) or _ARXIV.match(value):
        return True
    host = (urlsplit(value).hostname or "").lower()
    return host in _PAPER_HOSTS


def register_tools(engine) -> FastMCP:
    mcp = FastMCP("Research MCP", instructions=INSTRUCTIONS)

    def response(payload: dict[str, Any], error: bool = False) -> CallToolResult:
        try:
            payload = engine._bounded(payload, payload.get("request_id"))
        except ToolError as exc:
            payload, error = exc.payload(), True
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return CallToolResult(content=[TextContent(type="text", text=text)],
                              structuredContent=payload, isError=error)

    async def invoke(capability: str, args: dict[str, Any]) -> CallToolResult:
        try:
            payload = await engine.execute(capability, {k: v for k, v in args.items()
                                                       if v is not None}, client_token_id.get())
            error = False
        except ToolError as exc:
            payload, error = exc.payload(), True
        except Exception as exc:
            payload, error = {"error": {"code": "INTERNAL", "message": f"Engine error ({type(exc).__name__})"}}, True
        return response(payload, error)

    async def dispatch(plan) -> CallToolResult:
        """Validate the public workflow, then run exactly one internal capability."""
        try:
            capability, args = plan()
        except ToolError as exc:
            return response(exc.payload(), True)
        return await invoke(capability, args)

    @mcp.tool(output_schema=SearchOutput.model_json_schema(), annotations=READ_ONLY)
    async def search(query: str, focus: Literal["web", "news"] = "web", limit: int = 8,
                     domains: list[str] | None = None,
                     recency: Literal["day", "week", "month", "year"] | None = None,
                     fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Default tool for everyday and current information: general web or news.

        focus=web (default) searches the web; focus=news searches recent news. Every
        configured web or news source runs concurrently and results are fused with
        provenance. Results are pointers (handle, url, snippet), not full text: call
        read on important results. domains restricts results to those sites and
        recency to the last day/week/month/year; sources that cannot apply a filter
        are skipped and listed in coverage (status=partial), never returned unfiltered.
        Absolute date ranges are not supported. fresh=true bypasses this server's
        cache only. deadline_s is the whole request budget (clamped to 5–50 seconds).
        """
        def plan():
            return (("news_search" if focus == "news" else "web_search"),
                    {"query": query, "limit": limit, "domains": domains, "recency": recency,
                     "fresh": fresh, "deadline_s": deadline_s})
        return await dispatch(plan)

    @mcp.tool(output_schema=ReadOutput.model_json_schema(), annotations=READ_ONLY)
    async def read(target: str, source_type: Literal["auto", "web", "paper"] = "auto",
                   cursor: str | None = None, fresh: bool = False,
                   deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Read source text from a URL, a returned handle, a DOI or an arXiv ID.

        source_type=auto treats DOIs, arXiv IDs, doi.org/arxiv.org links and scholarly
        handles (doi:, arxiv:, pmid:, openalex:, s2:) as papers and everything else,
        including ordinary article URLs, as web pages; set web or paper to override.
        Readers are tried in order until one returns usable text. Paper results state
        document.kind: fulltext or abstract (an abstract is not the full paper). Long
        documents are paginated: pass document.next_cursor back as cursor with the
        same target. fresh=true bypasses this server's document cache only.
        """
        def plan():
            kind = source_type
            if kind == "auto":
                kind = "paper" if isinstance(target, str) and paper_target(target) else "web"
            return (f"{kind}_read", {"target": target, "cursor": cursor, "fresh": fresh,
                                     "deadline_s": deadline_s})
        return await dispatch(plan)

    @mcp.tool(output_schema=SearchOutput.model_json_schema(), annotations=READ_ONLY)
    async def paper_search(query: str, limit: int = 8, year_from: int | None = None,
                           year_to: int | None = None, fresh: bool = False,
                           deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Discover academic papers and studies (a separate pool from web search).

        Scholarly sources run concurrently; results are fused by DOI/arXiv/PMID identity
        with conflicting identifiers kept apart. Items are pointers: read a paper with
        read (its handle or DOI), or use paper_explore for metadata and citations.
        year_from/year_to filter by publication year on sources that support it.
        """
        def plan():
            return "paper_search", {"query": query, "limit": limit, "year_from": year_from,
                                    "year_to": year_to, "fresh": fresh, "deadline_s": deadline_s}
        return await dispatch(plan)

    @mcp.tool(output_schema=ExploreOutput.model_json_schema(), annotations=READ_ONLY)
    async def paper_explore(operation: Literal["metadata", "related", "citations"],
                            ids: list[str] | None = None, citation: str | None = None,
                            relation: Literal["similar", "citing", "cited"] | None = None,
                            direction: Literal["in", "out", "both"] | None = None,
                            depth: int | None = None, limit: int | None = None,
                            fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Work with known papers: identity, related work and citation networks.

        operation=metadata resolves ids (DOI, arXiv, PMID, OpenAlex, S2) or one free-text
        citation; a citation is only resolved with title/year evidence and a strong
        identifier, otherwise it stays unknown (see per_id_coverage). operation=related
        needs ids as seeds and relation=similar (default), citing (papers citing each
        seed) or cited (each seed's references); every seed is handled and reported in
        per_seed_coverage; limit caps results. operation=citations returns a bounded
        citation graph from ids with direction=in (citing), out (cited) or both
        (default) and depth 1 (default) or 2; check truncated. Resolve free-text
        citations with metadata first, then pass their identifiers.
        """
        def plan():
            common = {"fresh": fresh, "deadline_s": deadline_s}
            if operation == "metadata":
                _reject("metadata", relation=relation, direction=direction, depth=depth, limit=limit)
                if not ids and not citation:
                    raise _invalid("metadata needs ids or citation")
                if ids and citation:
                    raise _invalid("metadata takes ids or citation, not both")
                return "paper_metadata", {"ids": ids, "citation": citation, **common}
            if not ids:
                raise _invalid(f"{operation} needs ids (paper identifiers used as seeds)")
            if operation == "related":
                _reject("related", citation=citation, direction=direction, depth=depth)
                return "paper_related", {"seeds": ids, "mode": relation or "similar",
                                         "limit": 8 if limit is None else limit, **common}
            _reject("citations", citation=citation, relation=relation, limit=limit)
            return "citation_graph", {"seeds": ids, "direction": direction or "both",
                                      "depth": 1 if depth is None else depth, **common}
        return await dispatch(plan)

    @mcp.tool(output_schema=VerifyWorkflowOutput.model_json_schema(), annotations=READ_ONLY)
    async def verify(operation: Literal["citation", "claim", "editorial"],
                     citation: str | None = None, claim: str | None = None,
                     ids: list[str] | None = None, fresh: bool = False,
                     deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Check a citation, evidence for a claim, or retraction/editorial notices.

        operation=citation checks that citation (text or identifier) matches a real
        record: bibliographic is match, mismatch, conflict (sources disagree) or unknown.
        operation=claim also needs claim, the exact statement attributed to the cited
        work, and returns citing passages in claim_evidence. citation_tallies are counts
        of citing statements, never proof of a claim. operation=editorial needs ids and
        returns source-attributed notices in checks; no notice means unknown, not clear.
        """
        def plan():
            common = {"fresh": fresh, "deadline_s": deadline_s}
            if operation == "editorial":
                _reject("editorial", citation=citation, claim=claim)
                if not ids:
                    raise _invalid("editorial needs ids")
                return "editorial_check", {"ids": ids, **common}
            _reject(operation, ids=ids)
            if not citation:
                raise _invalid(f"{operation} needs citation")
            if operation == "citation":
                _reject("citation", claim=claim)
                return "citation_verify", {"citation": citation, **common}
            if not claim:
                raise _invalid("claim needs claim, the statement to check")
            return "citation_verify", {"citation": citation, "claim": claim, **common}
        return await dispatch(plan)

    @mcp.tool(output_schema=SearchOutput.model_json_schema(), annotations=READ_ONLY)
    async def code_search(query: str,
                          scope: Literal["docs", "repositories", "code", "issues"] = "repositories",
                          repos: list[str] | None = None, language: str | None = None,
                          min_stars: int | None = None, limit: int = 8, fresh: bool = False,
                          deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Technical research: library documentation, repositories, code and issues.

        scope=docs searches indexed technical documentation; repositories (default)
        finds projects (min_stars allowed); code searches source code; issues searches
        issue and discussion threads. repos (owner/name) narrows docs, code or issues;
        language filters by programming language. Code search may require an
        authenticated upstream account and then reports a plan error in coverage.
        """
        def plan():
            common = {"query": query, "language": language, "limit": limit, "fresh": fresh,
                      "deadline_s": deadline_s}
            if scope == "docs":
                _reject("scope=docs", min_stars=min_stars)
                return "developer_search", {**common, "kind": "docs", "repos": repos}
            if scope == "repositories":
                _reject("scope=repositories", repos=repos)
                return "repo_search", {**common, "mode": "repos", "min_stars": min_stars}
            _reject(f"scope={scope}", min_stars=min_stars)
            return "repo_search", {**common, "mode": scope, "repos": repos}
        return await dispatch(plan)

    @mcp.tool(output_schema=SiteOutput.model_json_schema(), annotations=STARTS_WORK)
    async def site_research(url: str, operation: Literal["map", "crawl"] = "map",
                            limit: int | None = None, max_depth: int | None = None,
                            include_paths: list[str] | None = None,
                            exclude_paths: list[str] | None = None, fresh: bool = False,
                            deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Discover a website's URLs, or start a bounded crawl of its pages.

        operation=map (default) returns urls found on the site (limit caps them).
        operation=crawl starts an asynchronous crawl bounded by limit (pages, default
        25), max_depth (0–10, default 2) and optional include_paths/exclude_paths; it
        returns a job_id to poll with get_job. Read individual pages with read.
        """
        def plan():
            if operation == "map":
                _reject("map", max_depth=max_depth, include_paths=include_paths, exclude_paths=exclude_paths)
                return "site_map", {"url": url, "limit": 25 if limit is None else limit,
                                    "fresh": fresh, "deadline_s": deadline_s}
            return "site_crawl", {"url": url, "limit": 25 if limit is None else limit,
                                  "max_depth": 2 if max_depth is None else max_depth,
                                  "include_paths": include_paths, "exclude_paths": exclude_paths,
                                  "deadline_s": deadline_s}
        return await dispatch(plan)

    @mcp.tool(output_schema=JobOutput.model_json_schema(), annotations=STARTS_WORK)
    async def deep_research(operation: Literal["literature", "systematic_review"], question: str,
                            criteria: list[str] | None = None) -> CallToolResult:
        """Start a long-running literature search or systematic review job.

        Uses an eligible hosted research service and returns a job_id; poll it with
        get_job (minutes, not seconds). criteria (inclusion/exclusion rules) applies to
        systematic_review only. If no eligible service is configured the call fails
        with NO_PROVIDER_AVAILABLE; it is never replaced by an ordinary search.
        """
        def plan():
            if operation == "literature":
                _reject("literature", criteria=criteria)
                return "deep_literature_search", {"goal": question}
            return "systematic_review", {"question": question, "criteria": criteria}
        return await dispatch(plan)

    @mcp.tool(output_schema=JobOutput.model_json_schema(), annotations=READ_ONLY)
    async def get_job(job_id: str, wait_s: float = 0) -> CallToolResult:
        """Poll an asynchronous job started by site_research or deep_research.

        Pass the returned job_id; wait_s waits up to 40 seconds for completion. Poll
        again while status is running. Jobs belong to the client that started them.
        """
        try:
            if (isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or
                    not math.isfinite(wait_s) or not 0 <= wait_s <= min(40, engine.settings.max_wait_s)):
                raise ToolError("INVALID_INPUT", "wait_s must be between 0 and 40")
            job = await engine.jobs.get(job_id, client_token_id.get(), wait_s)
            payload, error = job.model_dump(), False
        except (KeyError, PermissionError):
            payload, error = {"error": {"code": "NOT_FOUND", "message": "Job not found for this client"}}, True
        except ToolError as exc:
            payload, error = exc.payload(), True
        except Exception as exc:
            payload, error = {"error": {"code": "INTERNAL", "message": f"Job error ({type(exc).__name__})"}}, True
        return response(payload, error)

    return mcp
