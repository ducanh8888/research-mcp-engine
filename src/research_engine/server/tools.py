"""Stable capability tools; backend MCP names and account credentials stay private."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from research_engine.router.execute import ToolError
from research_engine.router.requests import DEFAULT_DEADLINE_S
from research_engine.server.auth import client_token_id
from research_engine.server.schemas import (
    EditorialOutput, GraphOutput, JobOutput, MapOutput, MetadataOutput, ReadOutput, SearchOutput, VerifyOutput,
)


def register_tools(engine) -> FastMCP:
    mcp = FastMCP("Research MCP", instructions=(
        "Task → tool: general/current web → web_search; recent news → news_search; academic studies → "
        "paper_search; read web URL/result → web_read; read paper → paper_read; DOI/citation metadata → "
        "paper_metadata; similar/citing/cited papers → paper_related; citation/claim checks → citation_verify; "
        "citation network → citation_graph; editorial/retraction notices → editorial_check; repositories/code "
        "→ repo_search or developer_search; site URLs → site_map; crawl → site_crawl; async jobs → get_job. "
        "Search returns source pointers, not full text: search then read important evidence. Web and paper "
        "search use separate provider pools. Inspect status and coverage; partial means an intended source "
        "failed, was skipped or timed out. Provenance is attribution, not a truth judgment. Poll async jobs "
        "using get_job. fresh=true bypasses the engine cache only; upstream uncached retrieval is not guaranteed."
    ))

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

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def web_search(query: str, limit: int = 8, domains: list[str] | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """General/current web research. Return attributable source pointers, not full text.

        Read important results with web_read. fresh bypasses only the engine cache;
        deadline_s is a shared request budget clamped to 5–50 seconds.
        """
        return await invoke("web_search", {"query": query, "limit": limit, "domains": domains,
                                          "date_from": date_from, "date_to": date_to,
                                          "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def news_search(query: str, limit: int = 8,
                          recency: Literal["day", "week", "month", "year"] | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Find recent news pointers with optional recency or ISO date bounds.

        Use web_read for source text. recency is day, week, month or year; unsupported
        filters may cause an explicit provider failure rather than silent filtering.
        """
        return await invoke("news_search", {"query": query, "limit": limit, "recency": recency,
                                           "date_from": date_from, "date_to": date_to,
                                           "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=ReadOutput.model_json_schema())
    async def web_read(target: str, cursor: str | None = None, fresh: bool = False,
                       deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Read a web URL or saved search handle for source text.

        Pass next_cursor from the previous page as cursor to continue the same
        document; a cursor from another document is invalid. fresh bypasses the
        engine document cache, not necessarily an upstream cache.
        """
        return await invoke("web_read", {"target": target, "cursor": cursor,
                                        "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=MapOutput.model_json_schema())
    async def site_map(url: str, limit: int = 25, fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """List source URLs for a site without generating a narrative."""
        return await invoke("site_map", {"url": url, "limit": limit, "fresh": fresh,
                                        "deadline_s": deadline_s})

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def site_crawl(url: str, limit: int = 25, max_depth: int = 2,
                         include_paths: list[str] | None = None,
                         exclude_paths: list[str] | None = None) -> CallToolResult:
        """Start a persisted site crawl; poll its recoverable upstream job using get_job."""
        return await invoke("site_crawl", {"url": url, "limit": limit, "max_depth": max_depth,
                                          "include_paths": include_paths, "exclude_paths": exclude_paths})

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def paper_search(query: str, limit: int = 8, year_from: int | None = None,
                           year_to: int | None = None, filters: dict[str, Any] | None = None,
                           fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Search academic papers/studies in a separate pool from web search.

        Results are source pointers; read important papers with paper_read.
        year_from/year_to filter only providers that support publication years.
        """
        return await invoke("paper_search", {"query": query, "limit": limit, "year_from": year_from,
                                            "year_to": year_to, "filters": filters, "fresh": fresh,
                                            "deadline_s": deadline_s})

    @mcp.tool(output_schema=ReadOutput.model_json_schema())
    async def paper_read(target: str, cursor: str | None = None, fresh: bool = False,
                         deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Read a paper URL/handle as available full text or a labeled abstract.

        Use next_cursor from the preceding response as cursor for another page.
        An abstract is not a full-text source document.
        """
        return await invoke("paper_read", {"target": target, "cursor": cursor,
                                          "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=MetadataOutput.model_json_schema())
    async def paper_metadata(ids: list[str] | None = None, citation: str | None = None,
                             fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Resolve identifiers or citation text with conservative bibliographic matching.

        Supply ids for known DOI/other identifiers, or citation for bibliographic
        text. A citation match requires title/year evidence and a strong identifier;
        ambiguous or unresolved text remains unknown. Inspect per_id_coverage and
        source assertions rather than assuming a top search hit proves identity.
        """
        return await invoke("paper_metadata", {"ids": ids, "citation": citation,
                                              "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def paper_related(seeds: list[str], mode: Literal["similar", "citing", "cited"] = "similar",
                            limit: int = 8,
                            fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Find similar, citing, or cited papers for every supplied seed.

        seeds is a nonempty list of paper identifiers. mode is similar, citing
        (papers citing each seed), or cited (references from each seed). Inspect
        per_seed_coverage for each seed/provider outcome; merged items are ranked
        pointers, with related_seed and related_mode on source provenance.
        """
        return await invoke("paper_related", {"seeds": seeds, "mode": mode, "limit": limit,
                                             "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=VerifyOutput.model_json_schema())
    async def citation_verify(citation: str, claim: str | None = None,
                              fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Verify a citation and optionally inspect evidence for a specific claim.

        citation is required text/identifier; claim is optional exact claim text.
        Bibliographic assertions, claim passages and citation tallies are separate;
        a tally does not prove the supplied claim.
        """
        return await invoke("citation_verify", {"citation": citation, "claim": claim,
                                               "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=GraphOutput.model_json_schema())
    async def citation_graph(seeds: list[str], direction: Literal["in", "out", "both"] = "both",
                             depth: int = 1,
                             fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Explore a bounded citation network from paper identifier seeds.

        direction=in returns citing papers; out returns cited references; both
        traverses both directions. depth is 1 or 2; inspect truncated and source
        assertions instead of treating this as a complete literature graph.
        """
        return await invoke("citation_graph", {"seeds": seeds, "direction": direction,
                                              "depth": depth, "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=EditorialOutput.model_json_schema())
    async def editorial_check(ids: list[str], fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Return explicit notices and provider unknowns; absence of a notice is not a clearance."""
        return await invoke("editorial_check", {"ids": ids, "fresh": fresh,
                                               "deadline_s": deadline_s})

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def systematic_review(question: str, criteria: list[str] | None = None) -> CallToolResult:
        """Start an upstream review job; generated prose is kept outside primary source evidence."""
        return await invoke("systematic_review", {"question": question, "criteria": criteria})

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def deep_literature_search(goal: str) -> CallToolResult:
        """Start a hosted deep search and persist its upstream reference before polling."""
        return await invoke("deep_literature_search", {"goal": goal})

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def developer_search(query: str, repos: list[str] | None = None,
                               language: str | None = None, limit: int = 8,
                               fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Search technical sources and repositories with provider attribution."""
        return await invoke("developer_search", {"query": query, "repos": repos, "language": language,
                                                "limit": limit, "fresh": fresh,
                                                "deadline_s": deadline_s})

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def repo_search(query: str, language: str | None = None, min_stars: int | None = None,
                          mode: Literal["repos", "code", "issues"] = "repos", limit: int = 8,
                          fresh: bool = False, deadline_s: float = DEFAULT_DEADLINE_S) -> CallToolResult:
        """Search GitHub repository, code, or issue metadata.

        mode=repos lists repositories, code searches code, issues searches issues;
        upstream permissions may block code search. Use developer_search for
        broader technical evidence rather than assuming semantic repository search.
        """
        return await invoke("repo_search", {"query": query, "language": language,
                                           "min_stars": min_stars, "mode": mode, "limit": limit,
                                           "fresh": fresh, "deadline_s": deadline_s})

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def get_job(job_id: str, wait_s: float = 0) -> CallToolResult:
        """Poll a site crawl or hosted review job owned by this client.

        Use the returned job_id; wait_s waits up to 40 seconds (subject to server
        max_wait_s). Poll again while status is running; unavailable jobs remain errors.
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
