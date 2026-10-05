"""Fixed virtual providers using one OmniRoute connection, not commodity accounts.

Register ``PROVIDERS`` explicitly in the provider registry. The connection row has
one encrypted service credential and ``base_url`` option (the API root, ending in
``/v1`` or ``/api/v1``); virtual providers have only keyless routing markers.
The engine context must inject the connection credential and options into virtual
calls. Never put an MCP client bearer token into these credentials.
"""

from research_engine.providers.base import Capability, Provider

from .bridge import OmniRouteBridge, OmniRouteProvider


class OmniRouteConnection(Provider):
    """Credential/endpoint holder, with no routable capability of its own."""

    name = "omniroute"


PROVIDERS = [
    OmniRouteConnection(),
    OmniRouteProvider("omni:brave-search", "brave-search", {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}),
    OmniRouteProvider("omni:serper-search", "serper-search", {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}),
    OmniRouteProvider("omni:jina-reader", "jina-reader", {Capability.WEB_READ}),
]

__all__ = ["OmniRouteBridge", "OmniRouteConnection", "OmniRouteProvider", "PROVIDERS"]
