"""One conservative boundary for untrusted provider and account error text."""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import ProviderError


def safe_error(error: ProviderError, credentials: dict[str, Any] | None = None) -> str:
    """Keep the classified kind and generic status, never echo upstream prose.

    Upstream messages can contain nested OAuth state, URL query secrets, a
    credential unknown to this account, or a complete HTTP response body.
    Adapter-authored prose is not a security boundary. Internal diagnostics
    retain the exception class through the request attempt kind.
    """
    kind = error.kind.value
    if error.status_code is not None:
        return f"{kind}: upstream HTTP {error.status_code}"
    # Classification is local; the message is not. Even an unknown token,
    # encoded URL or JSON fragment can appear in provider-supplied prose.
    return f"{kind}: upstream error"
