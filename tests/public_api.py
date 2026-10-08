"""Map internal capability fixtures onto the nine public MCP workflow tools.

Contract tests exercise capability semantics (fanout, coverage, sequential
reads) through the real public MCP surface, so each internal capability is
called via the workflow tool and operation that dispatches to it.
"""

from __future__ import annotations

from typing import Any

PUBLIC_TOOLS = frozenset({"search", "read", "paper_search", "paper_explore", "verify", "code_search",
                          "site_research", "deep_research", "get_job"})


def to_public(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name in PUBLIC_TOOLS:
        return name, dict(args)
    a = dict(args)
    if name in {"web_search", "news_search"}:
        return "search", {**a, "focus": "news" if name == "news_search" else "web"}
    if name in {"web_read", "paper_read"}:
        return "read", {**a, "source_type": name.removesuffix("_read")}
    if name == "paper_metadata":
        return "paper_explore", {"operation": "metadata", **a}
    if name == "paper_related":
        return "paper_explore", {"operation": "related", "ids": a.pop("seeds"),
                                 **({"relation": a.pop("mode")} if "mode" in a else {}), **a}
    if name == "citation_graph":
        return "paper_explore", {"operation": "citations", "ids": a.pop("seeds"), **a}
    if name == "citation_verify":
        return "verify", {"operation": "claim" if a.get("claim") else "citation", **a}
    if name == "editorial_check":
        return "verify", {"operation": "editorial", **a}
    if name == "developer_search":
        return "code_search", {"scope": "docs", **a}
    if name == "repo_search":
        mode = a.pop("mode", "repos")
        return "code_search", {"scope": "repositories" if mode == "repos" else mode, **a}
    if name in {"site_map", "site_crawl"}:
        return "site_research", {"operation": "map" if name == "site_map" else "crawl", **a}
    if name in {"deep_literature_search", "systematic_review"}:
        operation = "literature" if name == "deep_literature_search" else "systematic_review"
        return "deep_research", {"operation": operation, "question": a.pop("goal", a.pop("question", "")), **a}
    raise KeyError(name)
