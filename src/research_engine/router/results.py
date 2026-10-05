"""Capability-specific adapter result criteria before marking an attempt successful."""

from __future__ import annotations

from research_engine.providers.base import SEARCH_CAPABILITIES, Capability, ErrorKind, ProviderError, Result
from research_engine.server.schemas import Hit


def validate_result(cap: Capability, result: Result) -> None:
    if not isinstance(result, Result):
        raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned an invalid result type")
    if cap in SEARCH_CAPABILITIES:
        # Empty hits is a successful zero-result search, not an unavailable provider.
        for hit in result.hits:
            if not isinstance(hit, Hit) or not hit.provider or not (hit.url or hit.ids or hit.id):
                raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned a search hit without an identity")
    elif cap in {Capability.WEB_READ, Capability.PAPER_READ}:
        document = result.document
        if document is None or not document.text.strip() or not document.url or not document.source:
            raise ProviderError(ErrorKind.TARGET, "Adapter returned no usable source document", block_capability=False)
    elif cap == Capability.PAPER_METADATA:
        if not (result.records or result.not_found or result.per_id_coverage):
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned no metadata lookup outcome")
        if any(not isinstance(record, dict) or not record.get("requested_id", record.get("id"))
               for record in result.records):
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned invalid metadata records")
    elif cap == Capability.CITATION_VERIFY:
        if not isinstance(result.verification, dict) or result.verification.get("bibliographic") not in {
            "match", "mismatch", "unknown"}:
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned no bibliographic assertion")
    elif cap == Capability.CITATION_GRAPH:
        if any(not isinstance(node.get("id"), str) for node in result.nodes) or any(
            not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str)
            for edge in result.edges
        ):
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned invalid graph evidence")
    elif cap == Capability.EDITORIAL_CHECK:
        if any(not isinstance(check, dict) for check in result.checks):
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned invalid editorial checks")
    elif cap == Capability.SITE_MAP:
        if any(not isinstance(url, str) or not url for url in result.urls):
            raise ProviderError(ErrorKind.TRANSIENT, "Adapter returned invalid mapped URLs")
