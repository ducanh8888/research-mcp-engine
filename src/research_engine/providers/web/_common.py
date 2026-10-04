"""Small conversion helpers shared by HTTP web adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from research_engine.providers.base import CallContext, ErrorKind, ProviderError
from research_engine.server.schemas import Document, Hit, Result


def api_key(ctx: CallContext, *, required: bool = True) -> str:
    for field in ("api_key", "key", "token", "access_token"):
        value = ctx.credentials.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if required:
        raise ProviderError(ErrorKind.AUTH, f"{ctx.provider}: API key is not configured")
    return ""


def options(ctx: CallContext, req: dict[str, Any]) -> dict[str, Any]:
    configured = ctx.options.get("defaults", {})
    merged = {**ctx.options, **(configured if isinstance(configured, dict) else {})}
    supplied = req.get("provider_options", {})
    if isinstance(supplied, dict):
        scoped = supplied.get(ctx.provider, supplied)
        if isinstance(scoped, dict):
            merged.update(scoped)
    return merged


def query(req: dict[str, Any]) -> str:
    value = req.get("query", req.get("q", ""))
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(ErrorKind.BAD_REQUEST, "A nonempty query is required", block_capability=False)
    return value.strip()


def domain_query(text: str, req: dict[str, Any], opts: dict[str, Any]) -> str:
    """Use documented site operators where an API has no native domain filter."""
    include = req.get("include_domains", opts.get("include_domains", [])) or []
    exclude = req.get("exclude_domains", opts.get("exclude_domains", [])) or []
    if isinstance(include, str):
        include = [include]
    if isinstance(exclude, str):
        exclude = [exclude]
    if include:
        text = f"({text}) (" + " OR ".join(f"site:{item}" for item in include) + ")"
    if exclude:
        text += " " + " ".join(f"-site:{item}" for item in exclude)
    return text


def limit(req: dict[str, Any], maximum: int = 100, default: int = 10) -> int:
    try:
        value = int(req.get("limit", req.get("num_results", req.get("max_results", default))))
    except (TypeError, ValueError) as exc:
        raise ProviderError(ErrorKind.BAD_REQUEST, "limit must be an integer", block_capability=False) from exc
    if value < 1:
        raise ProviderError(ErrorKind.BAD_REQUEST, "limit must be positive", block_capability=False)
    return min(value, maximum)


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return max(0.0, (instant - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def error_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(error_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(
            error_text(value[key])
            for key in ("tag", "code", "type", "message", "error", "detail", "details")
            if key in value
        )
    return ""


def checked_json(
    response: httpx.Response, ctx: CallContext, *, exhausted_statuses: tuple[int, ...] = (402,)
) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    failed = response.is_error or (isinstance(data, dict) and data.get("success") is False)
    if failed:
        text = error_text(data) or response.reason_phrase or "upstream request failed"
        for secret in ctx.credentials.values():
            if isinstance(secret, str) and secret:
                text = text.replace(secret, "[redacted]")
        lower = text.lower()
        status = response.status_code
        exhausted = (
            "no_more_credits", "api_key_budget_exceeded", "team_budget_exceeded",
            "insufficient credits", "out of credits", "no credits", "credits exhausted",
            "not enough credits", "quota exhausted", "monthly quota", "daily quota", "credit limit",
        )
        plan = ("feature_disabled", "upgrade your plan", "not available on your plan", "plan required")
        target = ("prohibited_content", "blocked url", "robots.txt", "not crawlable")
        if status in exhausted_statuses or any(word in lower for word in exhausted):
            kind = ErrorKind.EXHAUSTED
        elif status == 429:
            kind = ErrorKind.RATE_LIMITED
        elif any(word in lower for word in target):
            kind = ErrorKind.TARGET
        elif any(word in lower for word in plan):
            kind = ErrorKind.PLAN
        elif status in (401, 403):
            kind = ErrorKind.AUTH
        elif status in (404, 410):
            kind = ErrorKind.TARGET
        elif 400 <= status < 500:
            kind = ErrorKind.BAD_REQUEST
        else:
            kind = ErrorKind.TRANSIENT
        raise ProviderError(
            kind, f"{ctx.provider}: {text[:500]}", retry_after=retry_after(response),
            status_code=status, block_capability=kind not in (ErrorKind.BAD_REQUEST, ErrorKind.TARGET),
        )
    if not isinstance(data, dict):
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider}: expected a JSON object")
    if not data and response.content.strip() not in (b"{}", b""):
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider}: invalid JSON response")
    return data


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True) if "<" in text else text.strip()


def hits(rows: Any, ctx: CallContext, *, maximum: int | None = None) -> list[Hit]:
    if not isinstance(rows, list):
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider}: invalid result array")
    out: list[Hit] = []
    for rank, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        url = row.get("url") or row.get("link") or row.get("html_url") or metadata.get("sourceURL")
        if isinstance(url, str):
            url = url.strip()
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        snippet = row.get("snippet") or row.get("description") or row.get("content")
        highlights = row.get("highlights")
        if not snippet and isinstance(highlights, list):
            snippet = "\n".join(str(item) for item in highlights if item)
        author = row.get("author")
        authors = row.get("authors") or ([author] if isinstance(author, str) and author else [])
        if isinstance(authors, (str, dict)):
            authors = [authors]
        authors = [str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in authors]
        published = row.get("publishedDate") or row.get("published") or row.get("date") or row.get("age")
        year = int(str(published)[:4]) if published and str(published)[:4].isdigit() else None
        out.append(Hit(
            provider=ctx.provider, account=ctx.account_id, rank=rank,
            title=clean_text(row.get("title") or metadata.get("title")), url=url.strip(),
            snippet=clean_text(snippet) or None, authors=authors, year=year,
            published=str(published) if published else None, raw=row,
        ))
        if maximum and len(out) >= maximum:
            break
    if rows and not out:
        raise ProviderError(ErrorKind.TRANSIENT, f"{ctx.provider}: results contain no usable source URL")
    return out


def document(text: Any, url: str, ctx: CallContext, *, kind: str = "page") -> Result:
    if not isinstance(text, str) or not text.strip():
        raise ProviderError(ErrorKind.TARGET, f"{ctx.provider}: target has no readable content",
                            block_capability=False)
    return Result(document=Document(url=url, text=text.strip(), source=ctx.provider, kind=kind))
