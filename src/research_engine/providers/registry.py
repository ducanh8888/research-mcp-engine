"""Only explicitly implemented capabilities are routed; hidden MCP tools stay hidden."""

from __future__ import annotations

from research_engine.providers.base import ASYNC_CAPABILITIES, Capability, Provider


def build_registry() -> dict[str, Provider]:
    from research_engine.providers import scholar, web
    from research_engine.providers.dev import github
    from research_engine.providers.mcp import adapters
    from research_engine.providers import omniroute

    registry: dict[str, Provider] = {}
    for module in (web, github, scholar, adapters, omniroute):
        for entry in module.PROVIDERS:
            provider = entry() if isinstance(entry, type) else entry
            if provider.name in registry:
                raise ValueError(f"Duplicate provider name: {provider.name}")
            registry[provider.name] = provider
    return registry


ORDER = {
    "web_search": ["omni:duckduckgo-free", "omni:exa-search", "omni:brave-search", "omni:serper-search"],
    "news_search": ["omni:brave-search", "omni:serper-search"],
    "web_read": ["omni:jina-reader", "trafilatura"],
    "site_map": ["firecrawl"], "site_crawl": ["firecrawl"],
    "paper_search": ["openalex", "crossref", "semantic_scholar", "arxiv", "consensus_api"],
    "paper_read": ["openalex", "semantic_scholar", "arxiv", "elicit_api"],
    "paper_metadata": ["crossref", "openalex", "semantic_scholar", "arxiv", "scite_rest"],
    "paper_related": ["semantic_scholar", "openalex"],
    "citation_verify": ["crossref", "openalex", "semantic_scholar", "scite_rest", "scite_mcp"],
    "citation_graph": ["openalex", "semantic_scholar"],
    "editorial_check": ["crossref", "openalex", "semantic_scholar", "scite_rest"],
    "deep_literature_search": ["undermind", "elicit_mcp"],
    "systematic_review": ["elicit_api", "elicit_mcp"],
    "developer_search": ["firecrawl", "exa", "github"], "repo_search": ["github"],
}
SEQUENTIAL = {Capability.WEB_READ, Capability.PAPER_READ, Capability.PAPER_METADATA, *ASYNC_CAPABILITIES}


def default_routes(registry: dict[str, Provider]) -> dict[str, dict]:
    routes = {}
    for cap in Capability:
        compatible = [name for name, provider in registry.items() if cap in provider.capabilities]
        ordered = [name for name in ORDER.get(cap.value, []) if name in compatible]
        if ordered:
            routes[cap.value] = {"mode": "sequential" if cap in SEQUENTIAL else "fanout", "providers": ordered}
    return routes
