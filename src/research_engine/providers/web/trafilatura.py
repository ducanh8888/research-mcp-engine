"""Extract source HTML locally with trafilatura; all fetches use the URL guard."""

from __future__ import annotations

import asyncio
from typing import Any

import trafilatura

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import document


class TrafilaturaProvider(Provider):
    name = "trafilatura"
    keyless = True
    capabilities = frozenset({Capability.WEB_READ})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.WEB_READ:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Trafilatura supports HTML reads only",
                                block_capability=False)
        url = str(req.get("url", ""))
        await ctx.validate_url(url)
        response = await ctx.request("GET", url, max_bytes=4 * 1024 * 1024,
                                     headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"})
        if response.is_error:
            raise ProviderError(ErrorKind.TARGET, f"Target returned HTTP {response.status_code}",
                                status_code=response.status_code, block_capability=False)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type == "application/pdf" or response.content.startswith(b"%PDF-"):
            raise ProviderError(ErrorKind.TARGET, "Use the PDF reader for this document",
                                block_capability=False)
        final_url = str(response.url)
        if final_url != url:
            await ctx.validate_url(final_url)
        if content_type in {"text/plain", "text/markdown"}:
            return document(response.text, final_url, ctx)
        async with asyncio.timeout(ctx.remaining()):
            text = await asyncio.to_thread(trafilatura.extract, response.text, url=final_url,
                                          output_format="txt", include_comments=False, include_tables=True,
                                          favor_precision=True)
        return document(text, final_url, ctx)
