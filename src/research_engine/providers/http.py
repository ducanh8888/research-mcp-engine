"""Deadline-aware raw HTTP, preserving status, headers and quota bodies for adapters.

Transient retry strategy adapted from vvzvlad/research-mcp @11f297d _http.py.
Quota and credential failures are deliberately returned without retries.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from research_engine.providers.base import ErrorKind, ProviderError


async def request(client: httpx.AsyncClient, method: str, url: str, *, deadline: float,
                  retries: int = 1, max_bytes: int = 16 * 1024 * 1024, **kwargs) -> httpx.Response:
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request deadline exceeded")
        try:
            async with asyncio.timeout_at(deadline):
                async with client.stream(method, url, timeout=kwargs.pop("timeout", remaining),
                                         follow_redirects=False, **kwargs) as streamed:
                    chunks, total = [], 0
                    async for chunk in streamed.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProviderError(ErrorKind.TRANSIENT, "Provider response exceeds the size limit")
                        chunks.append(chunk)
                    response = httpx.Response(streamed.status_code, headers=streamed.headers,
                                              content=b"".join(chunks), request=streamed.request)
            credit_body = any(marker in response.text.lower() for marker in
                              ("out of credits", "credits exhausted", "insufficient credits", "quota exceeded"))
            if response.status_code not in {408, 500, 502, 503, 504} or credit_body or attempt == retries:
                return response
        except httpx.RequestError as error:
            if attempt == retries:
                raise ProviderError(ErrorKind.TRANSIENT, "Provider connection failed") from error
        delay = min(0.25 * 2 ** attempt, max(0, deadline - time.monotonic()))
        await asyncio.sleep(delay)
    raise ProviderError(ErrorKind.TRANSIENT, "Provider request failed")
