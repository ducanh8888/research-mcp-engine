"""Licensed DuckDuckGo HTML parser adapted from vvzvlad/research-mcp @11f297d.

Source: src/providers/duckduckgo.py. The MIT license and attribution are retained
in LICENSE.research-mcp. No challenge solving or automatic retry is performed.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import lxml.etree
import lxml.html

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError
from research_engine.server.schemas import Result

from ._common import domain_query, hits, limit, options, query


DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
DDG_REGION_ANY = "wt-wt"
MIN_INTERVAL_SECONDS = 45.0
_DDG_REGIONS = {"en": "us-en", "ru": "ru-ru", "de": "de-de", "fr": "fr-fr", "es": "es-es", "it": "it-it"}


def _class_xpath(token: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {token} ')"


def _ddg_region(value: str) -> str:
    code = value.strip().replace("_", "-").lower()
    return _DDG_REGIONS.get(code.split("-")[0], DDG_REGION_ANY)


def _unwrap_href(href: str) -> str:
    # Decode only the redirect wrapper, preserving escapes inside the target URL.
    values = parse_qs(urlparse(href).query).get("uddg")
    return (values[0] if values else href).strip()


def parse_results(html: str) -> list[dict[str, str]]:
    """Distinguish organic results, genuine zero results, blocks and parser drift."""
    try:
        doc = lxml.html.fromstring(html)
    except lxml.etree.ParserError as exc:
        raise ProviderError(ErrorKind.TRANSIENT, "DuckDuckGo returned empty or unparseable HTML") from exc
    out: list[dict[str, str]] = []
    rows_present = False
    unusable_href = False
    for row in doc.xpath(f"//*[{_class_xpath('result')}]"):
        links = row.xpath(f".//a[{_class_xpath('result__a')}]")
        if not links:
            continue
        rows_present = True
        if row.xpath(f"self::*[{_class_xpath('result--ad')}]"):
            continue
        url = _unwrap_href(links[0].get("href") or "")
        if not url.startswith(("http://", "https://")):
            unusable_href = True
            continue
        snippets = row.xpath(f".//*[{_class_xpath('result__snippet')}]")
        out.append({"title": links[0].text_content().strip(), "url": url,
                    "snippet": snippets[0].text_content().strip() if snippets else ""})
    if out:
        return out
    if unusable_href:
        raise ProviderError(ErrorKind.TRANSIENT, "DuckDuckGo result redirect format is incompatible")
    if rows_present or doc.xpath(f"//*[{_class_xpath('no-results')}]"):
        return []
    lower = html.lower()
    if any(marker in lower for marker in ("captcha", "anomaly.js", "ratelimit", "verify you are human")):
        raise ProviderError(ErrorKind.RATE_LIMITED, "DuckDuckGo returned a challenge or rate-limit page",
                            retry_after=480)
    raise ProviderError(ErrorKind.TRANSIENT, "DuckDuckGo HTML did not contain a recognizable result page")


class DuckDuckGoProvider(Provider):
    name = "duckduckgo"
    keyless = True
    capabilities = frozenset({Capability.WEB_SEARCH})

    def __init__(self) -> None:
        # Check and assignment contain no await, so the local asyncio slot is atomic.
        self._last_call = float("-inf")

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.WEB_SEARCH:
            raise ProviderError(ErrorKind.BAD_REQUEST, "DuckDuckGo HTML supports web search only",
                                block_capability=False)
        text = query(req)
        count = limit(req, 30)
        opts = options(ctx, req)
        language = req.get("language", opts.get("language"))
        body = {"q": domain_query(text, req, opts),
                "kl": _ddg_region(str(language)) if language else DDG_REGION_ANY}
        if req.get("page"):
            try:
                page = int(req["page"])
            except (ValueError, TypeError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "DuckDuckGo page must be an integer",
                                    block_capability=False) from exc
            if page != 1:
                raise ProviderError(ErrorKind.BAD_REQUEST, "DuckDuckGo exposes the first result page only",
                                    block_capability=False)
        now = time.monotonic()
        delay = MIN_INTERVAL_SECONDS - (now - self._last_call)
        if delay > 0:
            raise ProviderError(ErrorKind.RATE_LIMITED, "DuckDuckGo local cooldown is active", retry_after=delay)
        self._last_call = now
        endpoint = str(ctx.options.get("base_url", DDG_ENDPOINT))
        response = await ctx.request("POST", endpoint, data=body, retries=0,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchEngine/0.1)",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        if response.status_code in (202, 429):
            raise ProviderError(ErrorKind.RATE_LIMITED, f"DuckDuckGo rate limited (HTTP {response.status_code})",
                                retry_after=480, status_code=response.status_code)
        if response.is_error:
            raise ProviderError(ErrorKind.TRANSIENT, f"DuckDuckGo returned HTTP {response.status_code}",
                                status_code=response.status_code)
        return Result(hits=hits(parse_results(response.text), ctx, maximum=count))
