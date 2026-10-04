"""GitHub REST search and reads, with per-account quota reporting.

Search result fields follow the public REST contract and mcp-omnisearch's
GitHub adapter shape. HTTP retries, account selection and fallback belong to
the shared transport/router; this adapter never retries a failed request.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from research_engine.providers.base import Capability, CallContext, ErrorKind, Provider, ProviderError, Request
from research_engine.server.schemas import Document, Hit, Result


class GitHubProvider(Provider):
    name = "github"
    keyless = True
    capabilities = frozenset({Capability.REPO_SEARCH, Capability.DEVELOPER_SEARCH, Capability.WEB_READ})
    options = {"base_url": "https://api.github.com", "api_version": "2026-03-10", "search_type": "hybrid"}

    @staticmethod
    def _token(ctx: CallContext) -> str | None:
        return next((str(ctx.credentials[key]) for key in ("token", "api_key", "access_token", "pat")
                     if ctx.credentials.get(key)), None)

    def _headers(self, ctx: CallContext, *, raw: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github.text-match+json",
            "X-GitHub-Api-Version": str(ctx.options.get("api_version", self.options["api_version"])),
            "User-Agent": "research-engine/0.1",
        }
        if token := self._token(ctx):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _reset(response: httpx.Response) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(response.headers["x-ratelimit-reset"]), UTC)
        except (KeyError, ValueError, OverflowError, OSError):
            return None

    @classmethod
    def _quota(cls, response: httpx.Response) -> dict[str, Any]:
        try:
            remaining = float(response.headers["x-ratelimit-remaining"])
        except (KeyError, ValueError):
            remaining = None
        reset = cls._reset(response)
        return {
            "quota_remaining": remaining,
            "quota_reset_at": reset.isoformat() if reset else None,
            "usage": {"requests": 1, "rate_limit_resource": response.headers.get("x-ratelimit-resource")},
        }

    @classmethod
    def _raise_status(cls, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        text = response.text.casefold()
        limited = status == 429 or (status == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
            or "secondary rate limit" in text
            or "rate limit exceeded" in text
        ))
        if limited:
            reset = cls._reset(response)
            retry_after = None
            if value := response.headers.get("retry-after"):
                try:
                    retry_after = max(1.0, float(value))
                except ValueError:
                    try:
                        retry_after = max(1.0, parsedate_to_datetime(value).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
            if retry_after is None and reset:
                retry_after = max(1.0, reset.timestamp() - time.time())
            raise ProviderError(ErrorKind.RATE_LIMITED, "GitHub rate limit reached",
                                retry_after=retry_after or 60.0, reset_at=reset, status_code=status)
        kind = {
            401: ErrorKind.AUTH, 403: ErrorKind.AUTH, 404: ErrorKind.TARGET,
            402: ErrorKind.PLAN, 422: ErrorKind.BAD_REQUEST,
        }.get(status, ErrorKind.TRANSIENT if status >= 500 else ErrorKind.BAD_REQUEST)
        raise ProviderError(kind, f"GitHub returned HTTP {status}", status_code=status)

    async def _get(self, ctx: CallContext, path: str, *, raw: bool = False,
                   params: dict[str, Any] | None = None) -> httpx.Response:
        base = str(ctx.options.get("base_url", self.options["base_url"])).rstrip("/")
        response = await ctx.request("GET", f"{base}{path}", headers=self._headers(ctx, raw=raw), params=params)
        self._raise_status(response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "GitHub returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(ErrorKind.TRANSIENT, "GitHub returned an unexpected response")
        return data

    async def call(self, cap: Capability, req: Request, ctx: CallContext) -> Result:
        if cap == Capability.WEB_READ:
            return await self._read(req, ctx)
        if cap not in {Capability.REPO_SEARCH, Capability.DEVELOPER_SEARCH}:
            raise ProviderError(ErrorKind.PLAN, "GitHub does not support this capability")
        query = str(req.get("query") or "").strip()
        if not query:
            raise ProviderError(ErrorKind.BAD_REQUEST, "GitHub search requires a query")
        mode = "issues" if cap == Capability.DEVELOPER_SEARCH else str(req.get("mode", "repos"))
        if mode not in {"repos", "code", "issues"}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported GitHub search mode")
        if mode == "code" and not self._token(ctx):
            raise ProviderError(ErrorKind.PLAN, "GitHub code search requires an authenticated account",
                                block_capability=False)
        if language := req.get("language"):
            query += f" language:{language}"
        if mode == "repos" and req.get("min_stars") is not None:
            query += f" stars:>={int(req['min_stars'])}"
        for repo in req.get("repos") or []:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid GitHub repository qualifier")
            query += f" repo:{repo}"
        params: dict[str, Any] = {"q": query, "per_page": min(100, max(1, int(req.get("limit", 10))))}
        if mode == "repos":
            if sort := ctx.options.get("sort"):
                if sort not in {"stars", "forks", "help-wanted-issues", "updated"}:
                    raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported GitHub repository sort")
                params["sort"] = sort
        if mode == "issues":
            search_type = str(ctx.options.get("search_type", "hybrid" if self._token(ctx) else "lexical"))
            if search_type not in {"lexical", "semantic", "hybrid"}:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported GitHub issue search type")
            if search_type != "lexical" and not self._token(ctx):
                search_type = "lexical"
            if search_type != "lexical":
                params["search_type"] = search_type
        endpoint = "repositories" if mode == "repos" else mode
        response = await self._get(ctx, f"/search/{endpoint}", params=params)
        data = self._json(response)
        if not isinstance(data.get("items"), list):
            raise ProviderError(ErrorKind.TRANSIENT, "GitHub search response has no items array")
        hits = [self._hit(item, mode, rank, ctx) for rank, item in enumerate(data["items"], 1)]
        raw = {key: data[key] for key in ("total_count", "incomplete_results", "search_type",
                                        "lexical_fallback_reason") if key in data}
        return Result(hits=hits, truncated=bool(data.get("incomplete_results")), raw=raw, **self._quota(response))

    @staticmethod
    def _hit(item: Any, mode: str, rank: int, ctx: CallContext) -> Hit:
        if not isinstance(item, dict) or not isinstance(item.get("html_url"), str):
            raise ProviderError(ErrorKind.TRANSIENT, "GitHub returned a malformed search item")
        matches = item.get("text_matches") or []
        fragments = [str(match["fragment"]) for match in matches
                     if isinstance(match, dict) and match.get("fragment")]
        metadata: dict[str, Any] = {"search_type": mode, "provider_score": item.get("score")}
        if mode == "repos":
            title = str(item.get("full_name") or "")
            snippet = item.get("description") or ""
            identifier = title
            metadata.update(repository=title, language=item.get("language"),
                            stars=item.get("stargazers_count"), forks=item.get("forks_count"),
                            topics=item.get("topics") or [], archived=item.get("archived"),
                            pushed_at=item.get("pushed_at"), license=item.get("license"))
        elif mode == "code":
            repo = (item.get("repository") or {}).get("full_name", "")
            path = str(item.get("path") or item.get("name") or "")
            title = f"{repo}/{path}"
            snippet = " ... ".join(fragments[:2]) or path
            identifier = item["html_url"]
            metadata.update(repository=repo, file_path=path, file_name=item.get("name"))
        else:
            title = str(item.get("title") or "")
            snippet = " ... ".join(fragments[:2]) or item.get("body") or ""
            identifier = item["html_url"]
            repository_url = str(item.get("repository_url") or "")
            metadata.update(repository=repository_url.split("/repos/", 1)[-1],
                            number=item.get("number"), state=item.get("state"),
                            is_pull_request="pull_request" in item, updated_at=item.get("updated_at"))
        return Hit(provider="github", account=ctx.account_id, rank=rank, title=title,
                   url=item["html_url"], ids={"github": identifier}, snippet=str(snippet)[:2000],
                   published=item.get("created_at"), raw=metadata, **metadata)

    async def _read(self, req: Request, ctx: CallContext) -> Result:
        target = str(req.get("target") or req.get("url") or "")
        if target.startswith("gh:"):
            target = "https://github.com/" + target[3:]
        parsed = urlsplit(target)
        if parsed.scheme != "https" or parsed.hostname not in {"github.com", "raw.githubusercontent.com"}:
            raise ProviderError(ErrorKind.TARGET, "Target is not a supported GitHub URL")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ProviderError(ErrorKind.TARGET, "Invalid GitHub URL authority")
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        if len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2]):
            raise ProviderError(ErrorKind.TARGET, "Invalid GitHub repository URL")
        if any(part in {".", ".."} or "\x00" in part or "\\" in part for part in parts):
            raise ProviderError(ErrorKind.TARGET, "Invalid GitHub file path")
        owner, repo = parts[:2]
        root = f"/repos/{owner}/{repo}"
        path = f"{root}/readme"
        params: dict[str, Any] = {}
        kind = "page"
        if parsed.hostname == "raw.githubusercontent.com":
            parts = parts[:2] + ["blob"] + parts[2:]
        if len(parts) > 2:
            if parts[2] in {"issues", "pull"} and len(parts) == 4 and parts[3].isdigit():
                response = await self._get(ctx, f"{root}/issues/{parts[3]}")
                issue = self._json(response)
                text = str(issue.get("title") or "") + "\n\n" + str(issue.get("body") or "")
                document = Document(url=target.split("?", 1)[0].split("#", 1)[0], text=text,
                                    source="github", kind="page")
                return Result(document=document, **self._quota(response))
            if parts[2] not in {"blob", "tree"} or len(parts) < 4:
                raise ProviderError(ErrorKind.TARGET, "Unsupported GitHub page type")
            params["ref"] = parts[3]
            file_path = "/".join(parts[4:])
            if parts[2] == "tree":
                file_path = f"{file_path}/README.md" if file_path else ""
            elif not file_path:
                raise ProviderError(ErrorKind.TARGET, "GitHub file URL has no file path")
            if file_path:
                path = f"{root}/contents/{quote(file_path, safe='/')}"
                kind = "page" if file_path.lower().endswith((".md", ".rst", ".txt")) else "code"
        response = await self._get(ctx, path, raw=True, params=params)
        try:
            text = response.content.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError("binary content")
        except UnicodeError as exc:
            raise ProviderError(ErrorKind.TARGET, "GitHub target contains binary content") from exc
        url = "https://github.com/" + "/".join(quote(part, safe="") for part in parts)
        return Result(document=Document(url=url, text=text, source="github", kind=kind), **self._quota(response))


PROVIDERS = [GitHubProvider]
