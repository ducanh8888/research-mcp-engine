"""One conservative boundary for untrusted provider and account error text."""

from __future__ import annotations

import json
from typing import Any

from research_engine.providers.base import ProviderError


def _values(value: Any):
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _values(item)


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
    text = str(error)
    sensitive = tuple(_values(credentials or {}))
    if any(secret in text for secret in sensitive) or any(marker in text.lower() for marker in (
        "token=", "api_key=", "access_token=", "authorization:", "bearer ", "?key=", "?code=",
    )):
        return f"{kind}: upstream error"
    # URLs and JSON often embed credentials in nested or encoded values.
    if "http://" in text or "https://" in text or text.lstrip().startswith(("{", "[")):
        return f"{kind}: upstream error"
    try:
        json.dumps(text)
    except (TypeError, ValueError):
        return f"{kind}: upstream error"
    return f"{kind}: {text[:180]}" if len(text) <= 180 else f"{kind}: upstream error"
