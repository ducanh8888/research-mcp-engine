"""Stable capability tools; backend MCP names and account credentials stay private."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from research_engine.router.execute import ToolError
from research_engine.server.auth import client_token_id
from research_engine.server.schemas import (
    EditorialOutput, GraphOutput, JobOutput, MapOutput, MetadataOutput, ReadOutput, SearchOutput, VerifyOutput,
)


def register_tools(engine) -> FastMCP:
    mcp = FastMCP("Research Engine", instructions=(
        "Private retrieval infrastructure. Use search results as pointers to sources. Read handles to retrieve "
        "source text. Source provenance and citation tallies are not evidence interpretation. "
        "Check coverage and partial status. Async tools return owned jobs; poll using get_job."
    ))

    async def invoke(capability: str, args: dict[str, Any]) -> CallToolResult:
        try:
            payload = await engine.execute(capability, {k: v for k, v in args.items()
                                                       if v is not None and k != "invoke"},
                                           client_token_id.get())
            error = False
        except ToolError as exc:
            payload, error = exc.payload(), True
        except Exception as exc:
            payload, error = {"error": {"code": "INTERNAL", "message": f"Engine error ({type(exc).__name__})"}}, True
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                              structuredContent=payload, isError=error)

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def web_search(query: str, limit: int = 8, domains: list[str] | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Search the web; fuse exact identities and return attributable source pointers."""
        return await invoke("web_search", locals())

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def news_search(query: str, limit: int = 8, recency: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Search news with explicit date filters; retain the returned publication dates."""
        return await invoke("news_search", locals())

    @mcp.tool(output_schema=ReadOutput.model_json_schema())
    async def web_read(target: str, cursor: str | None = None, fresh: bool = False,
                       deadline_s: float = 30) -> CallToolResult:
        """Read source text from a URL or persisted search handle, with bounded pages."""
        return await invoke("web_read", locals())

    @mcp.tool(output_schema=MapOutput.model_json_schema())
    async def site_map(url: str, limit: int = 25, fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """List source URLs for a site without generating a narrative."""
        return await invoke("site_map", locals())

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def site_crawl(url: str, limit: int = 25, max_depth: int = 2,
                         include_paths: list[str] | None = None,
                         exclude_paths: list[str] | None = None) -> CallToolResult:
        """Start a persisted site crawl; poll its recoverable upstream job using get_job."""
        return await invoke("site_crawl", locals())

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def paper_search(query: str, limit: int = 8, year_from: int | None = None,
                           year_to: int | None = None, filters: dict[str, Any] | None = None,
                           fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Search scholarly sources, preserving identifier and version conflicts."""
        return await invoke("paper_search", locals())

    @mcp.tool(output_schema=ReadOutput.model_json_schema())
    async def paper_read(target: str, cursor: str | None = None, fresh: bool = False,
                         deadline_s: float = 30) -> CallToolResult:
        """Retrieve available paper full text or an explicitly labeled abstract."""
        return await invoke("paper_read", locals())

    @mcp.tool(output_schema=MetadataOutput.model_json_schema())
    async def paper_metadata(ids: list[str] | None = None, citation: str | None = None,
                             fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Resolve bibliographic metadata, falling back independently for missing identifiers."""
        if not ids and not citation:
            return CallToolResult(content=[TextContent(type="text", text="ids or citation is required")],
                                  structuredContent={"error": {"code": "INVALID_INPUT", "message": "ids or citation is required"}},
                                  isError=True)
        return await invoke("paper_metadata", locals())

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def paper_related(seeds: list[str], mode: str = "similar", limit: int = 8,
                            fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Find similar, citing or cited papers from the selected scholarly providers."""
        return await invoke("paper_related", locals())

    @mcp.tool(output_schema=VerifyOutput.model_json_schema())
    async def citation_verify(citation: str, claim: str | None = None,
                              fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Check bibliography and return any upstream claim evidence separately from citation tallies."""
        return await invoke("citation_verify", locals())

    @mcp.tool(output_schema=GraphOutput.model_json_schema())
    async def citation_graph(seeds: list[str], direction: str = "both", depth: int = 1,
                             fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Return bounded citation nodes and edges with explicit truncation."""
        return await invoke("citation_graph", locals())

    @mcp.tool(output_schema=EditorialOutput.model_json_schema())
    async def editorial_check(ids: list[str], fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Return explicit notices and provider unknowns; absence of a notice is not a clearance."""
        return await invoke("editorial_check", locals())

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def systematic_review(question: str, criteria: list[str] | None = None) -> CallToolResult:
        """Start an upstream review job; generated prose is kept outside primary source evidence."""
        return await invoke("systematic_review", locals())

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def deep_literature_search(goal: str) -> CallToolResult:
        """Start a hosted deep search and persist its upstream reference before polling."""
        return await invoke("deep_literature_search", locals())

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def developer_search(query: str, repos: list[str] | None = None,
                               language: str | None = None, limit: int = 8,
                               fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Search technical sources and repositories with provider attribution."""
        return await invoke("developer_search", locals())

    @mcp.tool(output_schema=SearchOutput.model_json_schema())
    async def repo_search(query: str, language: str | None = None, min_stars: int | None = None,
                          mode: str = "repos", limit: int = 8,
                          fresh: bool = False, deadline_s: float = 30) -> CallToolResult:
        """Search repository or code metadata without assuming semantic search support."""
        return await invoke("repo_search", locals())

    @mcp.tool(output_schema=JobOutput.model_json_schema())
    async def get_job(job_id: str, wait_s: float = 0) -> CallToolResult:
        """Poll a job owned by this client, waiting at most 40 seconds."""
        try:
            if not 0 <= wait_s <= min(40, engine.settings.max_wait_s):
                raise ToolError("INVALID_INPUT", "wait_s must be between 0 and 40")
            job = await engine.jobs.get(job_id, client_token_id.get(), wait_s)
            payload, error = job.model_dump(), False
        except (KeyError, PermissionError):
            payload, error = {"error": {"code": "NOT_FOUND", "message": "Job not found for this client"}}, True
        except ToolError as exc:
            payload, error = exc.payload(), True
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structuredContent=payload, isError=error)

    return mcp
