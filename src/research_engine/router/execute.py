"""Capability → provider → account → execution/fallback → normalized evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select

from research_engine.cache import Cache
from research_engine.config import Settings
from research_engine.merge import merge_hits
from research_engine.merge.canonical import canonical_handle, normalize_url
from research_engine.providers.base import (
    ASYNC_CAPABILITIES, SEARCH_CAPABILITIES, CallContext, Capability, ErrorKind,
    Provider, ProviderError, Result,
)
from research_engine.providers.registry import build_registry, default_routes
from research_engine.router.requests import InvalidRequest, for_provider, validate
from research_engine.router.results import validate_result
from research_engine.storage.blobs import BlobStore
from research_engine.storage.crypto import Cipher, SecretStore
from research_engine.storage.db import (
    Account, Attempt, Database, Handle, HandleAlias, ProviderRow, RequestRow, Routing, utcnow,
)


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, coverage: dict | None = None,
                 request_id: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.coverage, self.request_id, self.retry_after = code, coverage, request_id, retry_after

    def payload(self) -> dict[str, Any]:
        value = {"error": {"code": self.code, "message": str(self)}}
        if self.retry_after is not None:
            value["error"]["retry_after"] = self.retry_after
        if self.coverage is not None:
            value["coverage"] = self.coverage
        if self.request_id is not None:
            value["request_id"] = self.request_id
        return value


@dataclass
class Outcome:
    provider: str
    result: Result | None = None
    account_id: int | None = None
    upstream_ref: str | None = None
    reason: str | None = None
    skipped: bool = False
    pending: bool = False


class Engine:
    def __init__(self, settings: Settings, providers: dict[str, Provider] | None = None,
                 *, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.providers = build_registry() if providers is None else providers
        self.db = Database(settings.database_path)
        self.db.initialize(self.providers, default_routes(self.providers))
        self.secrets = SecretStore(self.db, Cipher.from_file(settings.encryption_key_file, self.db))
        self.blobs = BlobStore(settings.blob_dir)
        self.cache = Cache(self.db, self.blobs)
        self.http = http_client or httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                                    limits=httpx.Limits(max_connections=50))
        self._owns_http = http_client is None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_call: dict[str, float] = {}
        from research_engine.providers.mcp.clients import AccountMCPClientManager
        self.mcp_manager = AccountMCPClientManager(self.secrets, self.settings.public_base_url)
        self.jobs = None

    async def start(self) -> None:
        from research_engine.jobs.runner import JobRunner
        self.jobs = JobRunner(self.db, self.providers, self.context, self.finalize_job)
        await self.jobs.start()

    async def stop(self) -> None:
        if self.jobs is not None:
            await self.jobs.stop()
        if self.mcp_manager is not None:
            await self.mcp_manager.close()
        if self._owns_http:
            await self.http.aclose()
        await asyncio.to_thread(self.db.close)

    def _provider_options(self, name: str) -> dict[str, Any]:
        with self.db.session() as session:
            row = session.get(ProviderRow, name)
            return {**self.providers[name].options, **(row.options if row else {})}

    async def context(self, name: str, account_id: int, timeout_s: float = 30) -> CallContext:
        credentials, options = await asyncio.gather(
            asyncio.to_thread(self.secrets.get, account_id),
            asyncio.to_thread(self._provider_options, name),
        )
        return CallContext(self.http, credentials, account_id, name, time.monotonic() + timeout_s,
                           options, self.mcp_manager)

    def _route(self, cap: Capability) -> tuple[str, list[str]]:
        with self.db.session() as session:
            row = session.get(Routing, cap.value)
            if row is None:
                return "fanout", []
            if row.mode not in {"fanout", "sequential"}:
                raise ToolError("INTERNAL", "Configured routing mode is invalid")
            names = list(row.providers)
            if any(name not in self.providers or cap not in self.providers[name].capabilities for name in names):
                raise ToolError("INTERNAL", "Configured route contains an incompatible provider")
            if len(names) != len(set(names)):
                raise ToolError("INTERNAL", "Configured route contains duplicate providers")
            if cap in ASYNC_CAPABILITIES and row.mode != "sequential":
                raise ToolError("INTERNAL", "Async starts require sequential provider routing")
            return row.mode, names

    def _eligible(self, name: str, cap: Capability) -> tuple[list[tuple[int, str | None]], str]:
        with self.db.session() as session:
            provider = session.get(ProviderRow, name)
            if provider is None or not provider.enabled:
                return [], "provider disabled"
            accounts = list(session.scalars(select(Account).where(Account.provider == name)
                                             .order_by(Account.priority, Account.id)))
            usable = [(row.id, row.quota_group) for row in accounts if row.enabled
                      and row.credential == "ok"
                      and (row.cooldown_until is None or row.cooldown_until <= utcnow())
                      and cap.value not in (row.blocked_capabilities or {})]
            return usable, "no usable account"

    def _attempt(self, request_id: str, name: str, account_id: int | None,
                 outcome: str, started: float, error: ProviderError | None = None) -> None:
        with self.db.session() as session:
            session.add(Attempt(request_id=request_id, provider=name, account_id=account_id,
                                outcome=outcome, latency_ms=(time.monotonic() - started) * 1000,
                                kind=error.kind.value if error else None,
                                error=str(error)[:1000] if error else None))

    def _availability(self, account_id: int, cap: Capability, error: ProviderError | None,
                      result: Result | None = None) -> None:
        with self.db.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                return
            if error is None:
                account.transient_failures = 0
                account.credential = "ok"
                if result is not None and result.quota_remaining is not None:
                    account.quota_remaining = result.quota_remaining
                    if result.quota_remaining <= 0:
                        account.cooldown_until = utcnow() + timedelta(hours=1)
                        account.cooldown_reason = "exhausted"
                return
            if error.kind == ErrorKind.AUTH:
                account.credential = "needs_auth"
            elif error.kind == ErrorKind.PLAN and error.block_capability:
                account.blocked_capabilities = {**(account.blocked_capabilities or {}), cap.value: str(error)[:300]}
            elif error.kind in {ErrorKind.RATE_LIMITED, ErrorKind.EXHAUSTED}:
                until = error.reset_at or utcnow() + timedelta(seconds=error.retry_after or
                                                              (60 if error.kind == ErrorKind.RATE_LIMITED else 3600))
                group = [account]
                if account.quota_group:
                    group = list(session.scalars(select(Account).where(Account.quota_group == account.quota_group,
                                                                       Account.provider == account.provider)))
                for member in group:
                    member.cooldown_until, member.cooldown_reason = until, error.kind.value
            elif error.kind == ErrorKind.TRANSIENT:
                account.transient_failures += 1
                if account.transient_failures >= 3:
                    account.cooldown_until = utcnow() + timedelta(seconds=min(300, 30 * 2 **
                                                                             min(account.transient_failures - 3, 4)))
                    account.cooldown_reason = "errors"

    async def _call(self, cap: Capability, args: dict, name: str, request_id: str,
                    deadline: float, *, start: bool = False) -> Outcome:
        try:
            adapter_args = for_provider(cap, args, name)
        except InvalidRequest as error:
            return Outcome(name, reason=f"unsupported_filter: {error}")
        accounts, reason = await asyncio.to_thread(self._eligible, name, cap)
        if time.monotonic() >= deadline:
            return Outcome(name, reason="deadline exceeded", pending=True)
        if not accounts:
            return Outcome(name, reason=reason, skipped=True)
        last_reason = "no usable account"
        for account_id, group in accounts:
            if time.monotonic() >= deadline:
                return Outcome(name, reason="deadline exceeded", pending=True)
            current, _ = await asyncio.to_thread(self._eligible, name, cap)
            if (account_id, group) not in current:
                continue
            began = time.monotonic()
            context = None
            try:
                async with asyncio.timeout_at(deadline):
                    context = await self.context(name, account_id, deadline - began)
                    limiter_key = f"{name}:{group or account_id}"
                    lock = self._locks.setdefault(limiter_key, asyncio.Lock())
                    interval = float(context.options.get("min_interval_s", {
                        "arxiv": 3, "semantic_scholar": 1, "jina": 3, "github": 2,
                    }.get(name, 0)))
                    if interval:
                        async with lock:
                            remaining = self._last_call.get(limiter_key, 0) + interval - time.monotonic()
                            if remaining > 0:
                                await asyncio.sleep(remaining)
                            self._last_call[limiter_key] = time.monotonic()
                    if start:
                        ref = await self.providers[name].start(cap, adapter_args, context)
                        if not isinstance(ref, str) or not ref:
                            raise ProviderError(ErrorKind.TRANSIENT, "Upstream returned no recoverable job reference",
                                                ambiguous_start=True)
                        await asyncio.to_thread(self._availability, account_id, cap, None)
                        await asyncio.to_thread(self._attempt, request_id, name, account_id, "started", began)
                        return Outcome(name, account_id=account_id, upstream_ref=ref)
                    result = await self.providers[name].call(cap, adapter_args, context)
                    validate_result(cap, result)
                    await asyncio.to_thread(self._availability, account_id, cap, None, result)
                    await asyncio.to_thread(self._attempt, request_id, name, account_id, "ok", began)
                    return Outcome(name, result=result, account_id=account_id)
            except TimeoutError:
                error = ProviderError(ErrorKind.TRANSIENT, "Deadline exceeded", ambiguous_start=start)
                await asyncio.to_thread(self._attempt, request_id, name, account_id, "deadline", began, error)
                if start:
                    raise ToolError("NO_PROVIDER_AVAILABLE", "Upstream start outcome is unknown; not retried",
                                    request_id=request_id) from error
                return Outcome(name, reason="deadline exceeded", pending=True)
            except ProviderError as error:
                if context is not None:
                    for secret in context.credentials.values():
                        if isinstance(secret, str) and secret:
                            error.args = (str(error).replace(secret, "[redacted]"),)
                await asyncio.to_thread(self._availability, account_id, cap, error)
                await asyncio.to_thread(self._attempt, request_id, name, account_id, "failed", began, error)
                if start and error.ambiguous_start:
                    raise ToolError("NO_PROVIDER_AVAILABLE", "Upstream start outcome is unknown; not retried",
                                    request_id=request_id) from error
                last_reason = f"{error.kind.value}: {error}"
                # A bad input or missing target is account-independent. Other providers may
                # still resolve the target, but retrying the same provider with another key cannot.
                if error.kind in {ErrorKind.BAD_REQUEST, ErrorKind.TARGET}:
                    break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                classified = ProviderError(ErrorKind.TRANSIENT, f"Adapter error ({type(error).__name__})")
                await asyncio.to_thread(self._availability, account_id, cap, classified)
                await asyncio.to_thread(self._attempt, request_id, name, account_id, "failed", began, classified)
                if start:
                    raise ToolError("NO_PROVIDER_AVAILABLE", "Upstream start outcome is unknown; not retried",
                                    request_id=request_id) from error
                last_reason = str(classified)
        return Outcome(name, reason=last_reason)

    @staticmethod
    def _coverage(outcomes: list[Outcome]) -> dict:
        return {"ok": [o.provider for o in outcomes if o.result is not None or o.upstream_ref],
                "failed": [{"p": o.provider, "reason": o.reason} for o in outcomes
                           if o.reason and not o.skipped],
                "skipped": [{"p": o.provider, "reason": o.reason} for o in outcomes if o.skipped]}

    def _record_request(self, request_id: str, cap: str, args: dict, client_token_id: int | None,
                        *, result: dict | None = None, replay_of: str | None = None) -> None:
        with self.db.session() as session:
            row = session.get(RequestRow, request_id)
            if row is None:
                if result is not None:
                    raise RuntimeError("Recorded request disappeared before finalization")
                row = RequestRow(id=request_id, tool=cap, args=args, client_token_id=client_token_id,
                                 replay_of=replay_of)
                session.add(row)
            if result is not None:
                row.result = result
                row.status = result.get("status", "error")
                row.coverage = result.get("coverage", {})
                row.finished_at = utcnow()

    def _persist_identities(self, merged: Any) -> None:
        with self.db.session() as session:
            for identity in merged.identities:
                handle = identity["handle"]
                row = session.get(Handle, handle)
                if row is None:
                    row = Handle(handle=handle, canonical_ids=identity["ids"],
                                 url=identity.get("url"), title=identity.get("title"))
                    session.add(row)
                    session.flush()
                else:
                    row.canonical_ids = {**row.canonical_ids, **identity["ids"]}
                    row.url = row.url or identity.get("url")
                for alias in identity.get("aliases", []):
                    if alias == handle:
                        continue
                    existing = session.get(HandleAlias, alias)
                    if existing is None:
                        session.add(HandleAlias(alias=alias, target=handle,
                                                ambiguous=alias in merged.ambiguous_aliases))
                    elif existing.target != handle or alias in merged.ambiguous_aliases:
                        existing.ambiguous, existing.target = True, None
            for alias in merged.ambiguous_aliases:
                existing = session.get(HandleAlias, alias)
                if existing is not None:
                    existing.ambiguous, existing.target = True, None

    def _resolve_target(self, target: str) -> tuple[str, str | None]:
        with self.db.session() as session:
            alias = session.get(HandleAlias, target)
            if alias and alias.ambiguous:
                raise ToolError("INVALID_INPUT", "Ambiguous handle; use the returned canonical handle")
            row = session.get(Handle, alias.target if alias else target)
            if row is not None:
                return row.url or next(iter(row.canonical_ids.values()), target), row.handle
        if target.startswith(("url:", "handle:")):
            raise ToolError("NOT_FOUND", "Unknown handle")
        return target, None

    async def _payload(self, cap: Capability, results: list[Result], limit: int = 25) -> dict:
        if cap in SEARCH_CAPABILITIES:
            merged = await asyncio.to_thread(merge_hits, [hit for result in results for hit in result.hits], limit)
            await asyncio.to_thread(self._persist_identities, merged)
            return {"items": [{**item, "h": item["handle"], "t": item["title"], "u": item["url"]}
                              for item in merged.items]}
        if cap in {Capability.WEB_READ, Capability.PAPER_READ}:
            document = next((r.document for r in results if r.document is not None), None)
            return {"document": document.model_dump() if document else None}
        if cap == Capability.PAPER_METADATA:
            records = [row for result in results for row in result.records]
            found = {str(row.get("requested_id", row.get("id", ""))) for row in records}
            missing = list(dict.fromkeys(item for result in results for item in result.not_found if item not in found))
            assertions: dict[str, list[Any]] = {}
            for result in results:
                for key, value in result.per_id_coverage.items():
                    assertions.setdefault(key, []).append(value)
            return {"records": records, "not_found": missing, "per_id_coverage": assertions}
        if cap == Capability.CITATION_GRAPH:
            return {"nodes": [node for result in results for node in result.nodes],
                    "edges": [edge for result in results for edge in result.edges],
                    "truncated": any(result.truncated for result in results)}
        if cap == Capability.EDITORIAL_CHECK:
            return {"checks": [check for result in results for check in result.checks]}
        if cap == Capability.CITATION_VERIFY:
            verdicts = {(r.verification or {}).get("bibliographic", "unknown") for r in results}
            verdict = ("conflict" if {"match", "mismatch"} <= verdicts else
                       "mismatch" if "mismatch" in verdicts else
                       "match" if verdicts == {"match"} else "unknown")
            return {"bibliographic": verdict,
                    "sources": [s for r in results for s in (r.verification or {}).get("sources", [])],
                    "claim_evidence": [s for r in results for s in r.claim_evidence],
                    "citation_tallies": [s for r in results for s in r.citation_tallies]}
        if cap in {Capability.SITE_MAP, Capability.SITE_CRAWL}:
            return {"urls": list(dict.fromkeys(url for result in results for url in result.urls)),
                    "documents": [d.model_dump() for result in results for d in result.documents]}
        return {"items": [], "sources": [result.model_dump(exclude_defaults=True) for result in results]}

    async def finalize_job(self, cap: Capability, result: Result) -> dict:
        validate_result(cap, result)
        return self._bounded(await self._payload(cap, [result]))

    def _bounded(self, payload: dict, request_id: str | None = None) -> dict:
        try:
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, OverflowError) as error:
            raise ToolError("INTERNAL", "Response cannot be serialized", request_id=request_id) from error
        if size > self.settings.max_response_bytes:
            raise ToolError("INTERNAL", "Response exceeds configured size limit", request_id=request_id)
        return payload

    def _cursor(self, handle: str, offset: int) -> str:
        payload = base64.urlsafe_b64encode(json.dumps([handle, offset]).encode()).decode().rstrip("=")
        signature = hmac.new(self.settings.admin_session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{signature}"

    def _page(self, document: dict, cursor: str | None) -> dict:
        handle, offset = document["handle"], 0
        if cursor:
            try:
                payload, signature = cursor.rsplit(".", 1)
                expected = hmac.new(self.settings.admin_session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
                if not hmac.compare_digest(signature, expected):
                    raise ValueError
                bound_handle, offset = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                if bound_handle != handle or not isinstance(offset, int) or offset < 0:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError) as error:
                raise ToolError("INVALID_INPUT", "Invalid or mismatched read cursor") from error
        text = document["text"]
        if offset > len(text):
            raise ToolError("INVALID_INPUT", "Cursor is beyond the source document")
        end = offset + self.settings.document_page_chars
        return {**document, "text": text[offset:end],
                "next_cursor": self._cursor(handle, end) if end < len(text) else None}

    @staticmethod
    def _validate(cap: Capability, args: dict) -> None:
        try:
            validate(cap, args)
        except InvalidRequest as error:
            raise ToolError("INVALID_INPUT", str(error)) from error

    async def execute(self, capability: str | Capability, args: dict[str, Any],
                      client_token_id: int | None = None, *, replay_of: str | None = None) -> dict:
        try:
            cap = Capability(capability)
        except ValueError as error:
            raise ToolError("INVALID_INPUT", "Unknown capability") from error
        try:
            args = validate(cap, args)
        except InvalidRequest as error:
            raise ToolError("INVALID_INPUT", str(error)) from error
        request_id = "r_" + uuid.uuid4().hex
        await asyncio.to_thread(self._record_request, request_id, cap.value, args, client_token_id,
                                replay_of=replay_of)
        try:
            result = await self._execute(cap, args, client_token_id, request_id)
            result = self._bounded(result, request_id)
            await asyncio.to_thread(self._record_request, request_id, cap.value, args, client_token_id, result=result)
            return result
        except asyncio.CancelledError:
            cancelled = ToolError("INTERNAL", "Request cancelled", request_id=request_id)
            task = asyncio.create_task(asyncio.to_thread(self._record_request, request_id, cap.value, args,
                                                         client_token_id, result=cancelled.payload()))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Already-cancelled callers must still await the terminal write.
                await task
            raise
        except TimeoutError as error:
            timeout = ToolError("NO_PROVIDER_AVAILABLE", "Request deadline exceeded", request_id=request_id)
            await asyncio.to_thread(self._record_request, request_id, cap.value, args, client_token_id,
                                    result=timeout.payload())
            raise timeout from error
        except ToolError as error:
            error.request_id = request_id
            await asyncio.to_thread(self._record_request, request_id, cap.value, args, client_token_id,
                                    result=error.payload())
            raise
        except Exception as error:
            internal = ToolError("INTERNAL", f"Engine error ({type(error).__name__})", request_id=request_id)
            await asyncio.to_thread(self._record_request, request_id, cap.value, args, client_token_id,
                                    result=internal.payload())
            raise internal from error

    async def _execute(self, cap: Capability, args: dict, client_token_id: int | None, request_id: str) -> dict:
        read = cap in {Capability.WEB_READ, Capability.PAPER_READ}
        original_target = args.get("target")
        handle = None
        deadline = time.monotonic() + args["deadline_s"]
        if read:
            args["target"], handle = await asyncio.to_thread(self._resolve_target, original_target)
            if not args.get("fresh"):
                cached = await asyncio.to_thread(self.cache.get_document, cap.value, handle or args["target"])
                if cached:
                    return {"status": "complete", "request_id": request_id,
                            "coverage": {"ok": [cached["source"]], "failed": [], "skipped": []},
                            "document": self._page(cached, args.get("cursor"))}
        elif cap not in ASYNC_CAPABILITIES and not args.get("fresh"):
            cached = await asyncio.to_thread(self.cache.get_query, cap.value, args)
            if cached:
                return {**cached, "request_id": request_id}
        mode, names = await asyncio.to_thread(self._route, cap)
        provider_args = {k: v for k, v in args.items() if k != "cursor" or not read}
        outcomes: list[Outcome] = []
        metadata = None
        metadata_attempts: list[tuple[Outcome, list[str]]] = []
        if cap == Capability.PAPER_METADATA and args.get("ids"):
            pending = list(args["ids"])
            records: list[dict] = []
            per_id: dict[str, Any] = {}
            for name in names:
                if not pending:
                    break
                if time.monotonic() >= deadline:
                    outcomes.append(Outcome(name, reason="deadline exceeded", pending=True))
                    metadata_attempts.append((outcomes[-1], pending[:]))
                    break
                attempted = pending[:]
                outcome = await self._call(cap, {**provider_args, "ids": attempted}, name, request_id, deadline)
                outcomes.append(outcome)
                metadata_attempts.append((outcome, attempted))
                if outcome.result is not None:
                    records.extend(outcome.result.records)
                    for key, assertion in outcome.result.per_id_coverage.items():
                        per_id.setdefault(key, []).append(assertion)
                    resolved = {str(r.get("requested_id", r.get("id", ""))) for r in outcome.result.records}
                    pending = [key for key in pending if key not in resolved]
                if outcome.pending:
                    break
            # Keep each provider's Outcome intact; records and per-ID evidence are
            # accumulated independently below, never substituted for attempt coverage.
            metadata = (records, pending, per_id)
        elif mode == "fanout":
            outcomes = list(await asyncio.gather(*(self._call(cap, provider_args, name, request_id, deadline)
                                                   for name in names)))
        else:
            for name in names:
                outcome = await self._call(cap, provider_args, name, request_id, deadline,
                                           start=cap in ASYNC_CAPABILITIES)
                outcomes.append(outcome)
                if outcome.result is not None or outcome.upstream_ref is not None or outcome.pending:
                    break
            selected = {o.provider for o in outcomes}
            outcomes.extend(Outcome(name, reason="not needed after successful fallback", skipped=True)
                            for name in names if name not in selected)
        coverage = self._coverage(outcomes)
        succeeded = [o for o in outcomes if o.result is not None or o.upstream_ref]
        pending = any(o.pending for o in outcomes)
        if not succeeded:
            raise ToolError("NO_PROVIDER_AVAILABLE", "No provider completed this capability", coverage=coverage)
        if cap in ASYNC_CAPABILITIES:
            if not succeeded or self.jobs is None:
                raise ToolError("NO_PROVIDER_AVAILABLE", "No async provider started a recoverable job", coverage=coverage)
            chosen = succeeded[0]
            job = await self.jobs.create(cap, args, chosen.provider, chosen.account_id,
                                         chosen.upstream_ref, client_token_id)
            return {**job.model_dump(), "request_id": request_id, "coverage": coverage}
        payload = await self._payload(cap, [o.result for o in succeeded], args.get("limit", 8))
        if metadata is not None:
            records, unresolved, evidence = metadata
            # An unfinished provider leaves its attempted IDs unknown; it does not
            # invalidate a completed miss for a different ID.
            not_found = []
            per_id_coverage = {}
            for key in args["ids"]:
                assertions = evidence.get(key, [])
                found = any(row.get("found") is True for row in assertions if isinstance(row, dict))
                completed_miss = bool(assertions) and all(isinstance(row, dict) and row.get("found") is False
                                                          for row in assertions)
                unfinished = any(o.result is None and key in attempted for o, attempted in metadata_attempts)
                if key in unresolved and completed_miss and not unfinished:
                    not_found.append(key)
                per_id_coverage[key] = {"found": found, "status": "found" if found else
                                        "not_found" if key in not_found else "unknown",
                                        "sources": assertions}
            payload = {"records": records, "not_found": not_found, "per_id_coverage": per_id_coverage}
        metadata_incomplete = metadata is not None and bool(metadata[1]) and any(
            o.result is None for o, _ in metadata_attempts)
        envelope = {"status": "partial" if pending or metadata_incomplete else "complete", "coverage": coverage,
                    "request_id": request_id, **payload}
        if read and payload.get("document") is not None:
            document = payload["document"]
            if not handle:
                merged = merge_hits([{"provider": document["source"], "url": document["url"],
                                      "ids": {}, "title": ""}])
                await asyncio.to_thread(self._persist_identities, merged)
                handle = merged.items[0]["handle"] if merged.items else canonical_handle({}, document["url"])
            document["handle"] = handle
            await asyncio.to_thread(self.cache.put_document, cap.value, handle, document)
            if normalize_url(args["target"]) != handle:
                await asyncio.to_thread(self.cache.put_document, cap.value, args["target"], document)
            envelope["document"] = self._page(document, args.get("cursor"))
        elif not read and envelope["status"] == "complete":
            await asyncio.to_thread(self.cache.put_query, cap.value, args, envelope)
        return envelope

    async def replay(self, request_id: str) -> dict:
        def load():
            with self.db.session() as session:
                row = session.get(RequestRow, request_id)
                if row is None:
                    raise KeyError(request_id)
                return row.tool, dict(row.args), row.client_token_id
        tool, args, owner = await asyncio.to_thread(load)
        return await self.execute(tool, {**args, "fresh": True}, owner, replay_of=request_id)

    async def test_account(self, account_id: int) -> dict:
        def load():
            with self.db.session() as session:
                row = session.get(Account, account_id)
                if row is None:
                    raise KeyError(account_id)
                row.cooldown_until, row.cooldown_reason = None, None
                row.blocked_capabilities, row.credential = {}, "ok"
                return row.provider
        name = await asyncio.to_thread(load)
        provider = self.providers[name]
        cap = next((c for c in provider.capabilities if c in SEARCH_CAPABILITIES), None)
        if cap is None:
            cap = next((c for c in provider.capabilities if c not in ASYNC_CAPABILITIES), None)
        if cap is None:
            return {"status": "pending", "reason": "An async provider requires a real job for a live check"}
        args = {"query": "research", "limit": 1, "ids": ["10.1038/nature14539"],
                "target": "https://example.com", "citation": "10.1038/nature14539", "seeds": ["10.1038/nature14539"]}
        rid = "r_" + uuid.uuid4().hex
        await asyncio.to_thread(self._record_request, rid, cap.value, args, None)
        context = await self.context(name, account_id)
        try:
            result = await provider.call(cap, args, context)
            await asyncio.to_thread(self._availability, account_id, cap, None, result)
            payload = {"status": "complete", "provider": name, "account_id": account_id}
        except ProviderError as error:
            await asyncio.to_thread(self._availability, account_id, cap, error)
            payload = {"status": "failed", "kind": error.kind.value, "reason": str(error)}
        await asyncio.to_thread(self._record_request, rid, cap.value, args, None, result=payload)
        return payload
