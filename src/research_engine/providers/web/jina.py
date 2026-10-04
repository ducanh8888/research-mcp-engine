"""Keyless Jina Reader, with optional account authentication."""

from __future__ import annotations

from typing import Any

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import api_key, checked_json, document


class JinaProvider(Provider):
    name = "jina"
    keyless = True
    capabilities = frozenset({Capability.WEB_READ})

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.WEB_READ:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Jina Reader supports reads only", block_capability=False)
        url = str(req.get("url", ""))
        await ctx.validate_url(url)
        base = str(ctx.options.get("base_url", "https://r.jina.ai")).rstrip("/")
        headers = {"Accept": "application/json", "X-Return-Format": "markdown"}
        key = api_key(ctx, required=False)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        response = await ctx.request("GET", base + "/" + url, headers=headers)
        if response.is_error:
            try:
                error = response.json()
            except ValueError:
                error = {}
            if error.get("name") in {"SecurityCompromiseError", "AssertionFailureError", "BadAttemptError"}:
                raise ProviderError(ErrorKind.TARGET, "Jina rejected or could not retrieve this target",
                                    status_code=response.status_code, block_capability=False)
            checked_json(response, ctx)
        if "json" in response.headers.get("Content-Type", ""):
            data = checked_json(response, ctx)
            payload = data.get("data", data)
            if not isinstance(payload, dict):
                raise ProviderError(ErrorKind.TRANSIENT, "Jina returned invalid reader data")
            if isinstance(data.get("code"), int) and data["code"] >= 400:
                raise ProviderError(ErrorKind.TARGET, "Jina could not read this target", block_capability=False)
            final_url = str(payload.get("url") or url)
            if final_url != url:
                await ctx.validate_url(final_url)
            result = document(payload.get("content"), final_url, ctx)
            result.usage = payload.get("usage") or {}
            result.raw = {"title": payload.get("title")}
            return result
        text = response.text
        if "Markdown Content:" in text:
            text = text.split("Markdown Content:", 1)[1]
        return document(text, url, ctx)
