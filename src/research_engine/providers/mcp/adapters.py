"""Explicit, normalized hosted research tools; never expose upstream namespaces.

Tool names/arguments and paper fields are documented at:
https://docs.scite.ai/mcp/tools/literature.md
https://github.com/elicit/api-examples/tree/main/integrations/mcp

Neither vendor publishes a complete MCP response envelope. We accept only a
machine-readable paper collection with documented fields, and report other
shapes as incompatible rather than treating prose or errors as empty success.
Undermind publishes tool names but no input/result schema and is not
registered. In particular read_pdfs returns QA, not paper source text.
"""

from __future__ import annotations

import json
from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Hit, Provider, ProviderError, Result
from research_engine.providers.mcp.clients import HOSTED_ENDPOINTS
from research_engine.providers.scholar.common import doi, limit, strip_tags
from research_engine.providers.scholar.elicit_api import _hit as elicit_hit


def _input_schema(tools: list[Any], name: str, arguments: dict[str, Any]) -> None:
    tool = next((tool for tool in tools if getattr(tool, "name", None) == name), None)
    if tool is None:
        raise ProviderError(ErrorKind.PLAN, f"Hosted MCP tool {name} is unavailable for this account")
    schema = getattr(tool, "input_schema", None)
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise ProviderError(ErrorKind.TRANSIENT, f"Hosted MCP tool {name} has an unsupported input schema")
    properties = schema["properties"]
    required = schema.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or not set(arguments).issubset(properties) or not set(required).issubset(arguments)):
        raise ProviderError(ErrorKind.TRANSIENT, f"Hosted MCP tool {name} has changed its input schema")
    for key, value in arguments.items():
        field = properties[key]
        if not isinstance(field, dict):
            raise ProviderError(ErrorKind.TRANSIENT, f"Hosted MCP tool {name} has an incompatible input schema")
        expected = field.get("type")
        allowed = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
        if expected in allowed and (not isinstance(value, allowed[expected]) or expected == "integer" and
                                    isinstance(value, bool)):
            raise ProviderError(ErrorKind.TRANSIENT, f"Hosted MCP tool {name} changed argument {key}")
        if expected == "object" and isinstance(value, dict) and isinstance(field.get("properties"), dict):
            if not set(value).issubset(field["properties"]) and field.get("additionalProperties") is False:
                raise ProviderError(ErrorKind.TRANSIENT, f"Hosted MCP tool {name} changed argument {key}")


def _tool_error(value: str) -> ProviderError:
    text = value.lower()
    if any(word in text for word in ("monthly mcp usage limit", "insufficient_quota", "quota exceeded")):
        kind = ErrorKind.EXHAUSTED
    elif any(word in text for word in ("rate_limited", "rate limit", "too many requests")):
        kind = ErrorKind.RATE_LIMITED
    elif any(word in text for word in ("api_access_denied", "upgrade your plan", "subscription required")):
        kind = ErrorKind.PLAN
    elif any(word in text for word in ("invalid_token", "unauthorized")):
        kind = ErrorKind.AUTH
    else:
        kind = ErrorKind.TRANSIENT
    # Never expose arbitrary vendor text: MCP error content may include credentials.
    return ProviderError(kind, f"Hosted MCP tool returned {kind.value}")


def _decode(response: Any) -> Any:
    content = getattr(response, "content", None)
    if getattr(response, "is_error", False):
        raise _tool_error(" ".join(getattr(block, "text", "") for block in content or []))
    structured = getattr(response, "structured_content", None)
    if structured is not None:
        if not isinstance(structured, (dict, list)):
            raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP returned incompatible structured data")
        return structured
    if not isinstance(content, list) or len(content) != 1 or getattr(content[0], "type", None) != "text":
        raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP returned no machine-readable result")
    try:
        data = json.loads(content[0].text)
    except (ValueError, TypeError) as exc:
        raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP returned non-JSON text") from exc
    if not isinstance(data, (dict, list)):
        raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP returned incompatible JSON")
    return data


async def _call(ctx: CallContext, endpoint: str, name: str, args: dict[str, Any]) -> Any:
    if ctx.mcp_manager is None:
        raise ProviderError(ErrorKind.AUTH, "Hosted MCP account is not configured")
    remaining = ctx.remaining()
    if remaining <= 0:
        raise TimeoutError("Hosted MCP request deadline exceeded")
    credentials = ctx.credentials
    try:
        tools = await ctx.mcp_manager.list_tools(ctx.account_id, endpoint, timeout=remaining, credentials=credentials)
    except (TimeoutError, ConnectionError) as exc:
        raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP discovery failed") from exc
    _input_schema(tools, name, args)
    remaining = ctx.remaining()
    if remaining <= 0:
        raise TimeoutError("Hosted MCP request deadline exceeded")
    try:
        response = await ctx.mcp_manager.call_tool(ctx.account_id, endpoint, name, args,
                                                   timeout=remaining, credentials=credentials)
    except (TimeoutError, ConnectionError) as exc:
        raise ProviderError(ErrorKind.TRANSIENT, "Hosted MCP call was interrupted") from exc
    return _decode(response)


def _paper_list(data: Any, *, provider: str, key: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ProviderError(ErrorKind.TRANSIENT, f"{provider} MCP returned an incompatible paper collection")
    papers = data[key]
    if any(not isinstance(paper, dict) or not isinstance(paper.get("title"), str) for paper in papers):
        raise ProviderError(ErrorKind.TRANSIENT, f"{provider} MCP returned incompatible paper records")
    return papers


def _scite_hit(paper: dict[str, Any], ctx: CallContext, rank: int) -> Hit:
    identifier = doi(paper.get("doi"))
    if not identifier:
        raise ProviderError(ErrorKind.TRANSIENT, "Scite MCP paper has no valid DOI")
    authors = paper.get("authors") or []
    if not isinstance(authors, list):
        raise ProviderError(ErrorKind.TRANSIENT, "Scite MCP authors are not a list")
    names = [name if isinstance(name, str) else name.get("name", "") if isinstance(name, dict) else ""
             for name in authors]
    year = paper.get("year")
    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        raise ProviderError(ErrorKind.TRANSIENT, "Scite MCP paper year has changed type")
    # Full-text excerpts and Smart Citations are paper metadata/evidence, not an abstract.
    raw = {field: paper[field] for field in ("fulltextExcerpts", "citations", "tally", "editorialNotices",
                                           "access", "isOa", "oaStatus", "license") if field in paper}
    return Hit(provider=ctx.provider, account=ctx.account_id, rank=rank, id=identifier, ids={"doi": identifier},
               title=strip_tags(paper["title"]) or "", url=f"https://doi.org/{identifier}",
               authors=[name for name in names if name], snippet=strip_tags(paper.get("abstract")),
               year=year, venue=strip_tags(paper.get("journal")), raw=raw)


class SciteMCPProvider(Provider):
    """Search only: other Scite MCP result envelopes lack verified schemas."""

    name = "scite_mcp"
    capabilities = frozenset({Capability.PAPER_SEARCH})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.PAPER_SEARCH:
            raise ProviderError(ErrorKind.PLAN, "Scite MCP capability has no verified result contract")
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ProviderError(ErrorKind.BAD_REQUEST, "Scite search requires a query")
        args: dict[str, Any] = {"term": query.strip(), "limit": limit(req)}
        for source, destination in (("year_from", "date_from"), ("year_to", "date_to")):
            if req.get(source) is not None:
                year = req[source]
                if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
                    raise ProviderError(ErrorKind.BAD_REQUEST, f"Scite {source} must be a four-digit year")
                args[destination] = f"{year}-01-01" if source == "year_from" else f"{year}-12-31"
        if args.get("date_from") and args.get("date_to") and args["date_from"] > args["date_to"]:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Scite start year must precede end year")
        if req.get("filters") is not None:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Scite does not accept generic filters")
        data = await _call(ctx, HOSTED_ENDPOINTS["scite"], "search_literature", args)
        papers = _paper_list(data, provider="Scite", key="hits")
        return Result(hits=[_scite_hit(paper, ctx, rank) for rank, paper in enumerate(papers[:args["limit"]], 1)])


class ElicitMCPProvider(Provider):
    """Documented search_papers result; async results need observed MCP export manifests."""

    name = "elicit_mcp"
    capabilities = frozenset({Capability.PAPER_SEARCH})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.PAPER_SEARCH:
            raise ProviderError(ErrorKind.PLAN, "Elicit MCP capability has no verified result contract")
        query = req.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit search query must contain 1 to 2000 characters")
        args: dict[str, Any] = {"query": query.strip(), "maxResults": limit(req)}
        filters = req.get("filters")
        if filters is not None:
            if not isinstance(filters, dict):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit filters must be an object")
            allowed = {"minYear", "maxYear", "typeTags", "maxQuartile", "includeKeywords", "excludeKeywords",
                       "hasPdf", "pubmedOnly", "retracted"}
            if not set(filters).issubset(allowed):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit MCP search has unsupported filters")
            args["filters"] = dict(filters)
        for source, destination in (("year_from", "minYear"), ("year_to", "maxYear")):
            if req.get(source) is not None:
                year = req[source]
                if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 9999:
                    raise ProviderError(ErrorKind.BAD_REQUEST, f"Elicit {source} must be a four-digit year")
                args.setdefault("filters", {})[destination] = year
        if args.get("filters"):
            years = [args["filters"].get(key) for key in ("minYear", "maxYear")]
            if any(year is not None and (not isinstance(year, int) or isinstance(year, bool)) for year in years):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit filter years must be integers")
            if years[0] is not None and years[1] is not None and years[0] > years[1]:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit start year must precede end year")
        if req.get("search_mode"):
            mode = req["search_mode"]
            if mode not in {"keyword", "semantic"}:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Elicit search mode")
            if mode == "keyword" and args.get("filters"):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit keyword search does not accept filters")
            args["searchMode"] = mode
        if req.get("corpus"):
            if req["corpus"] not in {"elicit", "pubmed"}:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Elicit corpus")
            args["corpus"] = req["corpus"]
        data = await _call(ctx, HOSTED_ENDPOINTS["elicit"], "search_papers", args)
        papers = _paper_list(data, provider="Elicit", key="papers")
        return Result(hits=[elicit_hit(paper, ctx, rank) for rank, paper in enumerate(papers[:args["maxResults"]], 1)],
                      raw={"warnings": data.get("warnings", [])})


# Undermind is deliberately absent from registration: public docs publish tool
# names, not authenticated input/result schemas or a recoverable job reference.
# Its read_pdfs tool returns QA rather than a paper_read source document.
PROVIDERS = [SciteMCPProvider, ElicitMCPProvider]
