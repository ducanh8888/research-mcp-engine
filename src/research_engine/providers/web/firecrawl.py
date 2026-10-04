"""Firecrawl v2 search, extraction, mapping and recoverable native crawl jobs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from research_engine.providers.base import Capability, CallContext, ErrorKind, JobUpdate, Provider, ProviderError
from research_engine.server.schemas import Document, Result

from ._common import api_key, checked_json, document, hits, limit, options, query


class FirecrawlProvider(Provider):
    name = "firecrawl"
    capabilities = frozenset({Capability.WEB_SEARCH, Capability.NEWS_SEARCH, Capability.DEVELOPER_SEARCH,
                              Capability.REPO_SEARCH, Capability.WEB_READ, Capability.SITE_MAP,
                              Capability.SITE_CRAWL})
    poll_interval_s = 5
    options = {"max_crawl_pages": 25, "max_depth": 2}

    @staticmethod
    def _base(ctx: CallContext) -> str:
        return str(ctx.options.get("base_url", "https://api.firecrawl.dev/v2")).rstrip("/")

    @staticmethod
    def _headers(ctx: CallContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key(ctx)}", "Content-Type": "application/json"}

    @staticmethod
    def _usage(data: dict[str, Any]) -> dict[str, Any]:
        return {key: data[key] for key in ("creditsUsed", "total", "completed", "expiresAt") if key in data}

    async def _request(self, ctx: CallContext, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return checked_json(await ctx.request(method, self._base(ctx) + path,
                                              headers=self._headers(ctx), **kwargs), ctx)

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        opts = options(ctx, req)
        if cap == Capability.WEB_READ:
            url = str(req.get("url", ""))
            await ctx.validate_url(url)
            body = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
            data = await self._request(ctx, "POST", "/scrape", json=body)
            page = data.get("data")
            if not isinstance(page, dict):
                raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned no scrape data")
            metadata = page.get("metadata") or {}
            if metadata.get("statusCode", 200) >= 400:
                raise ProviderError(ErrorKind.TARGET, "Firecrawl target returned an HTTP error",
                                    block_capability=False)
            final_url = str(metadata.get("url") or metadata.get("sourceURL") or url)
            if final_url != url:
                await ctx.validate_url(final_url)
            result = document(page.get("markdown"), final_url, ctx)
            result.usage = self._usage(data)
            result.raw = {"metadata": metadata}
            return result
        if cap == Capability.SITE_MAP:
            url = str(req.get("url", ""))
            await ctx.validate_url(url)
            body = {"url": url, "limit": limit(req, 1000, 100),
                    "includeSubdomains": bool(opts.get("include_subdomains", False))}
            if req.get("query") or opts.get("search"):
                body["search"] = req.get("query") or opts["search"]
            data = await self._request(ctx, "POST", "/map", json=body)
            links = data.get("links")
            if not isinstance(links, list):
                raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned an invalid map")
            records = [item for item in links if isinstance(item, dict)]
            urls = [item.get("url") if isinstance(item, dict) else item for item in links]
            urls = list(dict.fromkeys(item for item in urls if isinstance(item, str)
                                      and item.startswith(("https://", "http://"))))
            return Result(urls=urls, records=records, usage=self._usage(data))
        if cap == Capability.SITE_CRAWL:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Use start_job for Firecrawl crawl",
                                block_capability=False)
        count = limit(req, 100)
        if cap in {Capability.DEVELOPER_SEARCH, Capability.REPO_SEARCH}:
            try:
                passages = int(req.get("passages", opts.get("passages", 1)))
            except (ValueError, TypeError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Firecrawl passages must be an integer",
                                    block_capability=False) from exc
            if not 1 <= passages <= 5:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Firecrawl passages must be between 1 and 5",
                                    block_capability=False)
            body: dict[str, Any] = {"query": query(req), "k": count, "passages": passages}
            kind = req.get("kind")
            kinds = {"docs": "doc", "doc": "doc", "issue": "issue", "issues": "issue",
                     "pull_request": "pull_request", "repo": "readme", "readme": "readme"}
            types = opts.get("types")
            if cap == Capability.REPO_SEARCH:
                types = ["readme"]
            elif kind:
                if kind not in kinds:
                    raise ProviderError(ErrorKind.BAD_REQUEST, "Firecrawl does not support this developer kind",
                                        block_capability=False)
                types = [kinds[kind]]
            if types:
                if not isinstance(types, list) or not set(types) <= {"doc", "issue", "pull_request", "readme"}:
                    raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid Firecrawl developer types",
                                        block_capability=False)
                body["types"] = types
            for field in ("repos", "sources", "skills", "language", "topic", "license", "min_stars",
                          "max_stars", "archived", "fork"):
                if field in req or field in opts:
                    body[field] = req.get(field, opts.get(field))
            if body.get("sources") and types and "doc" not in types:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Firecrawl sources filter requires doc type",
                                    block_capability=False)
            data = await self._request(ctx, "POST", "/search/developer", json=body)
            rows = data.get("results")
            if isinstance(rows, list):
                rows = [{**row, "title": row.get("title") or row.get("url", ""),
                         "snippet": "\n".join(item.get("text", "") for item in row.get("passages", [])
                                               if isinstance(item, dict))}
                        for row in rows if isinstance(row, dict)]
            return Result(hits=hits(rows, ctx, maximum=count), usage=self._usage(data),
                          raw={"sources": data.get("sources", [])})
        if cap not in {Capability.WEB_SEARCH, Capability.NEWS_SEARCH}:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Unsupported Firecrawl capability",
                                block_capability=False)
        source = "news" if cap == Capability.NEWS_SEARCH else "web"
        body = {"query": query(req), "limit": count, "sources": [source]}
        for local, remote in (("include_domains", "includeDomains"), ("exclude_domains", "excludeDomains"),
                              ("country", "country"), ("freshness", "tbs")):
            value = req.get(local, opts.get(remote))
            if value:
                if local == "freshness":
                    value = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y",
                             "pd": "qdr:d", "pw": "qdr:w", "pm": "qdr:m", "py": "qdr:y"}.get(value, value)
                body[remote] = value
        data = await self._request(ctx, "POST", "/search", json=body)
        payload = data.get("data")
        if isinstance(payload, dict):
            if source not in payload:
                raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl omitted the requested result source")
            rows = payload[source]
        else:
            rows = payload
        return Result(hits=hits(rows, ctx, maximum=count), usage=self._usage(data))

    async def start(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> str:
        if cap != Capability.SITE_CRAWL:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Firecrawl only starts crawl jobs",
                                block_capability=False)
        url = str(req.get("url", ""))
        await ctx.validate_url(url)
        opts = options(ctx, req)
        try:
            depth = int(req.get("depth", opts.get("max_depth", 2)))
        except (TypeError, ValueError) as exc:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Crawl depth must be an integer",
                                block_capability=False) from exc
        if not 0 <= depth <= 10:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Crawl depth must be between 0 and 10",
                                block_capability=False)
        page_req = {"limit": req.get("max_pages", req.get("limit", opts.get("max_crawl_pages", 25)))}
        body: dict[str, Any] = {
            "url": url, "limit": limit(page_req, 100), "maxDiscoveryDepth": depth,
            "allowExternalLinks": False, "allowSubdomains": bool(opts.get("allow_subdomains", False)),
            "ignoreRobotsTxt": False, "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        }
        for local, remote in (("include_paths", "includePaths"), ("exclude_paths", "excludePaths")):
            value = req.get(local, opts.get(local))
            if value:
                body[remote] = value
        try:
            data = await self._request(ctx, "POST", "/crawl", json=body, retries=0)
        except ProviderError as exc:
            if exc.kind == ErrorKind.TRANSIENT:
                exc.ambiguous_start = True
            raise
        except (httpx.RequestError, TimeoutError, OSError) as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl crawl submission outcome is unknown",
                                ambiguous_start=True) from exc
        ref = data.get("id")
        if not isinstance(ref, str) or not ref:
            raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned no recoverable crawl id",
                                ambiguous_start=True)
        return ref

    async def poll(self, ctx: CallContext, ref: str) -> JobUpdate:
        path = "/crawl/" + quote(ref, safe="")
        response = await ctx.request("GET", self._base(ctx) + path, headers=self._headers(ctx))
        if response.status_code in {404, 410}:
            return JobUpdate("failed", error="Firecrawl crawl id is missing or expired")
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.is_error and isinstance(data, dict) and data.get("status") in {
                "failed", "cancelled", "canceled"}:
            status = data["status"]
            return JobUpdate("failed" if status == "failed" else "cancelled",
                             error=str(data.get("error") or "Firecrawl crawl failed"))
        data = checked_json(response, ctx)
        status = data.get("status")
        if status in {"scraping", "running", "pending", "queued"}:
            return JobUpdate("running", poll_after_s=self.poll_interval_s)
        if status in {"failed", "cancelled", "canceled"}:
            return JobUpdate("cancelled" if status in {"cancelled", "canceled"} else "failed",
                             error=str(data.get("error") or "Firecrawl crawl failed"))
        if status != "completed":
            raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned an unknown crawl status")
        rows = data.get("data", [])
        if not isinstance(rows, list):
            raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned invalid crawl data")
        rows = list(rows)
        next_url = data.get("next")
        pages = 0
        base = urlsplit(self._base(ctx))
        seen: set[str] = set()
        while next_url:
            parsed = urlsplit(str(next_url))
            if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or parsed.path.rstrip("/") != (
                    base.path + path).rstrip("/"):
                raise ProviderError(ErrorKind.TARGET, "Firecrawl returned an unsafe pagination URL",
                                    block_capability=False)
            if next_url in seen or pages >= 100:
                raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl crawl pagination did not terminate")
            seen.add(next_url)
            page = checked_json(await ctx.request("GET", next_url, headers=self._headers(ctx)), ctx)
            if not isinstance(page.get("data"), list):
                raise ProviderError(ErrorKind.TRANSIENT, "Firecrawl returned invalid crawl page")
            rows.extend(page["data"])
            next_url = page.get("next")
            pages += 1
        documents = []
        failed_pages = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("markdown"), str):
                continue
            metadata = row.get("metadata") or {}
            url = str(metadata.get("url") or metadata.get("sourceURL") or "")
            if not url.startswith(("https://", "http://")) or not row["markdown"].strip():
                continue
            if metadata.get("statusCode", 200) >= 400:
                failed_pages.append({"url": url, "reason": "target_http_error",
                                     "status_code": metadata["statusCode"]})
                continue
            try:
                await ctx.validate_url(url)
            except ProviderError as exc:
                if exc.kind not in {ErrorKind.TARGET, ErrorKind.BAD_REQUEST}:
                    raise
                failed_pages.append({"url": url, "reason": "unsafe_source_url"})
                continue
            documents.append(Document(url=url, text=row["markdown"], source=self.name, kind="page"))
        return JobUpdate("completed", result=Result(documents=documents, usage=self._usage(data),
                                                   raw={"upstream_job_id": ref, "failed_pages": failed_pages}))

    async def cancel(self, ctx: CallContext, ref: str) -> bool:
        response = await ctx.request("DELETE", self._base(ctx) + "/crawl/" + quote(ref, safe=""),
                                     headers=self._headers(ctx), retries=0)
        if response.status_code in {404, 410}:
            return False
        if response.status_code == 204:
            return True
        return checked_json(response, ctx).get("success") is True
