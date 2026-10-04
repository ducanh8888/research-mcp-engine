"""Shared scholarly shapes; source assertions are never claim interpretations."""

from __future__ import annotations

import asyncio
import html
import re
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote

from research_engine.providers.base import Document, ErrorKind, Hit, ProviderError, Result

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"{}]+", re.I)
ARXIV_RE = re.compile(r"(?:(?:arxiv:|arxiv\.org/(?:abs|pdf|html)/|10\.48550/arxiv\.))?((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v\d+)?)", re.I)


def doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = DOI_RE.search(unquote(value))
    if not match:
        return None
    identifier = match.group(0).rstrip(".,;").lower()
    while identifier.endswith(")") and identifier.count(")") > identifier.count("("):
        identifier = identifier[:-1]
    return identifier


def arxiv_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = unquote(value).removesuffix(".pdf")
    match = ARXIV_RE.fullmatch(value) or (ARXIV_RE.search(value) if "arxiv" in value.lower() else None)
    return match.group(1).lower() if match else None


def strip_tags(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    return " ".join(html.unescape(text).split())


def input_ids(req: dict[str, Any]) -> list[str]:
    values = req.get("ids") or req.get("seeds") or []
    if isinstance(values, str):
        values = [values]
    if not values:
        value = req.get("target") or req.get("citation")
        if isinstance(value, dict):
            value = value.get("doi") or value.get("id") or value.get("title")
        if value:
            values = [value]
    return list(dict.fromkeys(str(value) for value in values))


def limit(req: dict[str, Any], default: int = 8) -> int:
    return max(1, min(int(req.get("limit", default)), 100))


def metadata_record(requested_id: str, hit: Hit, provider: str) -> dict[str, Any]:
    return {"requested_id": requested_id, "id": hit.id, "ids": hit.ids,
            "metadata": hit.model_dump(exclude_none=True), "providers": [provider],
            "sources": [{"provider": provider, "id": hit.id, "url": hit.url}]}


def metadata_result(items: list[tuple[str, Hit | None]], provider: str) -> Result:
    return Result(records=[metadata_record(key, hit, provider) for key, hit in items if hit],
                  not_found=[key for key, hit in items if hit is None],
                  per_id_coverage={key: {"found": hit is not None, "providers": [provider]}
                                   for key, hit in items})


def normal_text(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def bibliographic(req: dict[str, Any], hit: Hit | None, provider: str) -> Result:
    """Exact identifiers or explicit fields match; rank/score never establishes a match."""
    if hit is None:
        return Result(verification={"bibliographic": "unknown", "sources": []})
    citation = req.get("citation") or req.get("target") or ""
    matched: list[str] = []
    mismatch: list[str] = []
    if isinstance(citation, dict):
        for field in ("title", "year", "doi"):
            if citation.get(field) is None:
                continue
            actual = hit.ids.get("doi") if field == "doi" else getattr(hit, field)
            if actual is None:
                continue
            expected = doi(citation[field]) if field == "doi" else str(citation[field])
            same = normal_text(str(expected)) == normal_text(str(actual))
            (matched if same else mismatch).append(field)
    else:
        text = str(citation).strip()
        identifier = doi(text)
        expected_arxiv = arxiv_id(text)
        identifier_only = text.lower() in {identifier or "", "doi:" + (identifier or ""),
            "https://doi.org/" + (identifier or ""), "http://doi.org/" + (identifier or "")}
        if identifier_only and hit.ids.get("doi") == identifier:
            matched.append("doi")
        elif expected_arxiv and hit.ids.get("arxiv") == expected_arxiv:
            matched.append("arxiv")
        if hit.title and normal_text(hit.title) in normal_text(text):
            matched.append("title")
    verdict = "mismatch" if mismatch else "match" if matched else "unknown"
    return Result(verification={"bibliographic": verdict, "sources": [{"provider": provider,
        "id": hit.id, "ids": hit.ids, "title": hit.title, "year": hit.year,
        "matched_fields": matched, "mismatched_fields": mismatch}]})


def retry_after(headers: Any) -> float | None:
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError):
            return None


async def json_request(ctx: Any, method: str, url: str, *, not_found: bool = False,
                       **kwargs: Any) -> dict[str, Any] | list[Any] | None:
    response = await ctx.request(method, url, **kwargs)
    status = response.status_code
    if status == 404 and not_found:
        return None
    if status >= 400:
        body = response.text.lower()
        exhausted = any(word in body for word in ("insufficient_quota", "credits exhausted", "credit balance",
                         "quota exceeded", "used all included", "payment required"))
        plan = any(word in body for word in ("api_access_denied", "upgrade", "plan does not", "subscription required"))
        kind = (ErrorKind.EXHAUSTED if status == 402 or exhausted else ErrorKind.PLAN if plan else
                ErrorKind.RATE_LIMITED if status == 429 or status == 503 and response.headers.get("Retry-After") else
                ErrorKind.AUTH if status in {401, 403} else ErrorKind.TARGET if status == 404 else
                ErrorKind.TRANSIENT if status >= 500 else ErrorKind.BAD_REQUEST)
        raise ProviderError(kind, f"{ctx.provider} returned HTTP {status}",
                            retry_after=retry_after(response.headers), status_code=status)
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider} returned invalid JSON") from exc
    if not isinstance(data, (dict, list)):
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider} returned an unexpected JSON shape")
    return data


def abstract_document(hit: Hit, provider: str) -> Result:
    if not hit.snippet:
        raise ProviderError(ErrorKind.TARGET, "No open full text or abstract is available")
    return Result(document=Document(url=hit.url, text=hit.snippet, source=provider, kind="abstract"),
                  raw={"id": hit.id, "ids": hit.ids, "fulltext": False})


async def pdf_document(ctx: Any, url: str, hit: Hit, provider: str) -> Result:
    """Guarded PDF helper is shared with the rest of the engine."""
    await ctx.validate_url(url)
    from research_engine.providers.pdf import read_pdf
    document = await read_pdf(ctx, url)
    document.source = provider
    document.kind = "fulltext"
    return Result(document=document, raw={"id": hit.id, "ids": hit.ids, "fulltext": True})


def graph_edge(source: Hit | dict[str, Any], target: Hit | dict[str, Any], provider: str,
               **attrs: Any) -> dict[str, Any]:
    source_id = source.id if isinstance(source, Hit) else source["id"]
    target_id = target.id if isinstance(target, Hit) else target["id"]
    return {"source": source_id, "target": target_id, "relation": "cites", "provider": provider, **attrs}


def graph_node(hit: Hit, provider: str) -> dict[str, Any]:
    return {"id": hit.id, "ids": hit.ids, "title": hit.title, "year": hit.year,
            "providers": [provider]}


class RequestSpacing:
    """One process-wide bucket per endpoint, including all accounts (arXiv ToU)."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def request(self, ctx: Any, method: str, url: str, **kwargs: Any) -> Any:
        async with self.lock:
            wait = max(0.0, self.last + self.interval - time.monotonic())
            if wait >= ctx.remaining():
                raise ProviderError(ErrorKind.RATE_LIMITED, "Provider request spacing exceeds deadline",
                                    retry_after=wait)
            if wait:
                await asyncio.sleep(wait)
            self.last = time.monotonic()
            return await ctx.request(method, url, **kwargs)
