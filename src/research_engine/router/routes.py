"""Capability modes and actual upstream identity for explicit provider routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from research_engine.providers.base import ASYNC_CAPABILITIES, SEARCH_CAPABILITIES, Capability

SEQUENTIAL_CAPABILITIES = frozenset({Capability.WEB_READ, Capability.PAPER_READ,
                                     Capability.PAPER_METADATA, *ASYNC_CAPABILITIES})

# A direct commodity adapter is retained for upgrade/data compatibility, but
# cannot be a second vote or an implicit fallback for its bridged operation.
BRIDGED_OPERATIONS: dict[tuple[str, Capability], str] = {
    (direct, cap): virtual for direct, virtual, caps in (
        ("brave", "omni:brave-search", (Capability.WEB_SEARCH, Capability.NEWS_SEARCH)),
        ("serper", "omni:serper-search", (Capability.WEB_SEARCH, Capability.NEWS_SEARCH)),
        ("exa", "omni:exa-search", (Capability.WEB_SEARCH, Capability.NEWS_SEARCH)),
        ("duckduckgo", "omni:duckduckgo-free", (Capability.WEB_SEARCH,)),
        ("firecrawl", "omni:firecrawl", (Capability.WEB_SEARCH, Capability.NEWS_SEARCH, Capability.WEB_READ)),
        ("tavily", "omni:tavily-search", (Capability.WEB_SEARCH, Capability.NEWS_SEARCH, Capability.WEB_READ)),
        ("jina", "omni:jina-reader", (Capability.WEB_READ,)),
    ) for cap in caps
}


def required_mode(capability: Capability | str) -> str:
    cap = Capability(capability)
    return "sequential" if cap in SEQUENTIAL_CAPABILITIES else "fanout"


def actual_provider(name: str, capability: Capability | str) -> str:
    """One source identity per operation across transport/account aliases."""
    if name.startswith("omni:"):
        identifier = name.removeprefix("omni:")
        return {
            "brave-search": "brave", "serper-search": "serper", "exa-search": "exa",
            "duckduckgo-free": "duckduckgo", "tavily-search": "tavily",
            "jina-reader": "jina", "jina-search": "jina",
        }.get(identifier, identifier)
    if name in {"scite_rest", "scite_mcp"}:
        return "scite"
    if name in {"elicit_api", "elicit_mcp"}:
        return "elicit"
    return name


def normalize_route(capability: Capability | str, names: list[str], catalog: Mapping[str, Any]) -> list[str]:
    """Migrate covered direct entries to their bridge entry without changing order.

    Legacy credentials and account rows remain untouched. If a required bridge
    entry is not installed, fail visibly rather than invoke the direct adapter.
    The first position for an actual provider is retained, but its bridged
    interface wins when both interfaces were saved in a legacy route.
    """
    cap = Capability(capability)
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("Route requires a nonempty provider list")
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate provider in {cap.value} route")
    migrated: list[str] = []
    identities: dict[str, int] = {}
    for name in names:
        chosen = BRIDGED_OPERATIONS.get((name, cap), name)
        adapter = catalog.get(chosen)
        if adapter is None or cap not in adapter.capabilities:
            raise ValueError(f"Route provider {chosen} does not implement {cap.value}")
        identity = actual_provider(chosen, cap)
        if identity in identities:
            # Two configured names for the same actual provider never become
            # separate evidence. Covered direct operations migrate to bridge.
            existing = migrated[identities[identity]]
            if chosen.startswith("omni:") and not existing.startswith("omni:"):
                migrated[identities[identity]] = chosen
            elif (chosen.startswith("omni:") and existing.startswith("omni:")
                  and chosen != existing):
                raise ValueError(f"Ambiguous upstream provider aliases in {cap.value} route")
            continue
        identities[identity] = len(migrated)
        migrated.append(chosen)
    return migrated


def validate_route(capability: Capability | str, mode: str, names: list[str],
                   catalog: Mapping[str, Any]) -> None:
    cap = Capability(capability)
    if mode != required_mode(cap):
        raise ValueError(f"{cap.value} requires {required_mode(cap)} routing")
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate provider in {cap.value} route")
    normalized = normalize_route(cap, names, catalog)
    if normalized != names:
        raise ValueError(f"{cap.value} route contains direct/bridged or hosted duplicate sources")


__all__ = ["BRIDGED_OPERATIONS", "SEQUENTIAL_CAPABILITIES", "SEARCH_CAPABILITIES",
           "actual_provider", "normalize_route", "required_mode", "validate_route"]
