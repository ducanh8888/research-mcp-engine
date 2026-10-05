"""Elicit v2 paper search and durable reports/systematic-review polling.

Official machine-readable contract: https://elicit.com/api/v2/openapi.json.
Report narratives never become source documents. Completed jobs use ordered paper
CSV or RIS exports, preserving the provider's identifiers and provenance.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from uuid import UUID

import httpx

from research_engine.providers.base import Capability, CallContext, ErrorKind, Hit, JobUpdate, Provider, ProviderError, Result
from research_engine.providers.scholar.common import doi, json_request, limit, strip_tags, year_range
from research_engine.providers.scholar.consensus_api import api_key

BASE = "https://elicit.com/api/v2"
MAX_EXPORT_BYTES = 10 * 1024 * 1024


def _hit(paper: dict[str, Any], ctx: CallContext, rank: int, session_id: str | None = None) -> Hit:
    ids: dict[str, str] = {}
    if identifier := doi(paper.get("doi")):
        ids["doi"] = identifier
    if paper.get("pmid"):
        ids["pmid"] = str(paper["pmid"])
    if paper.get("elicitId"):
        ids["elicit"] = str(paper["elicitId"])
    urls = paper.get("urls") or []
    url = (urls[0] if isinstance(urls, list) and urls else paper.get("url")) or ""
    if not url and "doi" in ids:
        url = f"https://doi.org/{ids['doi']}"
    authors = paper.get("authors") or []
    if isinstance(authors, str):
        authors = [name.strip() for name in authors.split(";") if name.strip()]
    authors = [str(author.get("name", "")) if isinstance(author, dict) else str(author) for author in authors]
    year = paper.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    raw = {**paper, "evidence_kind": "paper_metadata", "snippet_kind": "abstract"}
    if session_id:
        raw.update(session_id=session_id, provenance="elicit_session_paper_export", rank_kind="export_order")
    return Hit(provider=ctx.provider, account=ctx.account_id, rank=rank,
               id=ids.get("doi") or ids.get("pmid") or ids.get("elicit") or url,
               ids=ids, title=strip_tags(paper.get("title")) or "", url=url,
               snippet=strip_tags(paper.get("abstract")), authors=[author for author in authors if author],
               year=year, venue=paper.get("venue"), raw=raw)


def _csv_papers(text: str) -> list[dict[str, Any]]:
    papers = []
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in (reader.fieldnames or [])}
    if not columns.intersection({"title", "papertitle"}):
        raise ProviderError(ErrorKind.TRANSIENT, "Elicit CSV export has no paper title column")
    for row in reader:
        normalized = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in row.items() if key}
        title = normalized.get("title") or normalized.get("papertitle")
        if not title:
            continue
        # Extraction answers and generated summaries remain raw, never abstract text.
        papers.append({"title": title, "doi": normalized.get("doi"), "pmid": normalized.get("pmid"),
                       "elicitId": normalized.get("elicitid") or normalized.get("paperid"),
                       "abstract": normalized.get("abstract"), "authors": normalized.get("authors") or "",
                       "year": normalized.get("year") or normalized.get("publicationyear"),
                       "venue": normalized.get("journal") or normalized.get("venue"),
                       "url": normalized.get("url") or normalized.get("paperurl"), "export_row": row})
    return papers


def _ris_papers(text: str) -> list[dict[str, Any]]:
    papers = []
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9]{2})  - ?(.*)$", line)
        if not match:
            continue
        tag, value = match.groups()
        if tag == "TY":
            fields = {}
        elif tag == "ER":
            def first(*tags: str) -> str | None:
                return next((fields[tag][0] for tag in tags if fields.get(tag)), None)
            title = first("TI", "T1")
            if title:
                year = first("PY", "Y1")
                papers.append({"title": title, "doi": first("DO"), "authors": fields.get("AU", []),
                               "year": year[:4] if year else None, "url": first("UR"),
                               "abstract": first("AB"), "venue": first("JO", "JF", "T2"), "export_tags": fields})
            fields = {}
        else:
            fields.setdefault(tag, []).append(value)
    if text.strip() and not papers:
        raise ProviderError(ErrorKind.TRANSIENT, "Elicit RIS export contains no valid paper records")
    return papers


class ElicitAPIProvider(Provider):
    name = "elicit_api"
    capabilities = frozenset({Capability.PAPER_SEARCH, Capability.DEEP_LITERATURE_SEARCH, Capability.SYSTEMATIC_REVIEW})
    poll_interval_s = 15

    def _headers(self, ctx: CallContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key(ctx)}"}

    async def call(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> Result:
        if cap != Capability.PAPER_SEARCH:
            raise ProviderError(ErrorKind.PLAN, "Elicit reports and reviews require async jobs")
        query = req.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit query must contain 1 to 2000 characters")
        year_range(req)
        if req.get("year_from") is not None or req.get("year_to") is not None:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit search has no verified publication-year filter")
        body: dict[str, Any] = {"query": query.strip(), "maxResults": limit(req)}
        for option, field in (("search_mode", "searchMode"), ("corpus", "corpus")):
            if ctx.options.get(option):
                body[field] = ctx.options[option]
        if filters := req.get("filters"):
            if not isinstance(filters, dict):
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit filters must be an object")
            body["filters"] = filters
        data = await json_request(ctx, "POST", f"{BASE}/search/papers", headers=self._headers(ctx), json=body)
        if not isinstance(data, dict) or not isinstance(data.get("papers"), list):
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit response omitted papers")
        papers = data["papers"][:limit(req)]
        if any(not isinstance(paper, dict) for paper in papers):
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit returned invalid paper records")
        return Result(hits=[_hit(paper, ctx, rank) for rank, paper in enumerate(papers, 1)],
                      raw={"warnings": data.get("warnings", [])})

    async def start(self, cap: Capability, req: dict[str, Any], ctx: CallContext) -> str:
        kind = {Capability.DEEP_LITERATURE_SEARCH: "reports", Capability.SYSTEMATIC_REVIEW: "systematic-reviews"}.get(cap)
        if kind is None:
            raise ProviderError(ErrorKind.PLAN, "Elicit async capability is unsupported")
        question = req.get("question") or req.get("goal") or req.get("query") or req.get("research_question")
        if not isinstance(question, str) or not question.strip():
            raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit requires a research question")
        body: dict[str, Any] = {"researchQuestion": question.strip(), "isPublic": False}
        if kind == "reports":
            try:
                body.update(maxSearchPapers=min(1000, max(1, int(req.get("max_search_papers", 50)))),
                            maxExtractPapers=min(80, max(1, int(req.get("max_extract_papers", 10)))))
            except (TypeError, ValueError) as exc:
                raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit paper limits must be integers") from exc
        else:
            body["generateReport"] = False
            if req.get("criteria") and not req.get("protocol_details"):
                criteria = req["criteria"]
                if not isinstance(criteria, list):
                    raise ProviderError(ErrorKind.BAD_REQUEST, "Elicit review criteria must be a list")
                body["protocolDetails"] = "\n".join(str(criterion) for criterion in criteria)
            for source, field in (("protocol_details", "protocolDetails"), ("searches", "searches"),
                                  ("abstract_screening", "abstractScreening"), ("fulltext_screening", "fulltextScreening"),
                                  ("extraction", "extraction")):
                if source in req:
                    body[field] = req[source]
        try:
            data = await json_request(ctx, "POST", f"{BASE}/sessions/{kind}", headers=self._headers(ctx),
                                      json=body, retries=0)
        except (httpx.RequestError, TimeoutError) as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit session creation was interrupted; creation is uncertain",
                                ambiguous_start=True) from exc
        except ProviderError as exc:
            if exc.kind == ErrorKind.TRANSIENT or exc.status_code == 408:
                # A 408 does not prove the upstream did not create the session.
                exc.kind = ErrorKind.TRANSIENT
                exc.ambiguous_start = True
            raise
        if not isinstance(data, dict) or not data.get("sessionId"):
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit did not return a session ID", ambiguous_start=True)
        try:
            session_id = str(UUID(data["sessionId"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit returned an invalid session ID", ambiguous_start=True) from exc
        return json.dumps({"kind": kind, "session_id": session_id}, separators=(",", ":"))

    async def poll(self, ctx: CallContext, ref: str) -> JobUpdate:
        try:
            state = json.loads(ref)
            kind, session_id = state["kind"], str(UUID(state["session_id"]))
            if kind not in {"reports", "systematic-reviews"}:
                raise ValueError("unsupported session kind")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ProviderError(ErrorKind.BAD_REQUEST, "Invalid Elicit polling reference") from exc
        data = await json_request(ctx, "GET", f"{BASE}/sessions/{kind}/{session_id}", headers=self._headers(ctx))
        if not isinstance(data, dict):
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit returned an invalid session response")
        status = data.get("status")
        if status == "pausedForInsufficientQuota":
            # The engine persists this running job. Resume is opt-in because it may spend restored credits.
            if ctx.options.get("resume_paused_jobs", False):
                try:
                    await json_request(ctx, "POST", f"{BASE}/sessions/{session_id}/resume", headers=self._headers(ctx))
                except ProviderError as exc:
                    if exc.kind not in {ErrorKind.EXHAUSTED, ErrorKind.RATE_LIMITED, ErrorKind.TRANSIENT}:
                        raise
            return JobUpdate("running", error="Elicit session paused for insufficient quota; restore quota and resume",
                             poll_after_s=60)
        if status in {"processing", "unknown"}:
            return JobUpdate("running", poll_after_s=self.poll_interval_s)
        if status == "failed":
            return JobUpdate("failed", error="Elicit session failed; inspect the provider account for details")
        if status != "completed":
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit returned an unknown session status")
        export_url, export_kind, stage = self._export(data, kind)
        if not export_url:
            if data.get("exportsStatus") == "unavailable":
                return JobUpdate("failed", error="Elicit completed but its paper exports are unavailable")
            return JobUpdate("running", error="Elicit completed; paper exports are still generating", poll_after_s=30)
        # Signed exports may use Elicit's storage host; validate DNS/SSRF policy and never forward the API key.
        await ctx.validate_url(export_url)
        response = await ctx.request("GET", export_url, follow_redirects=False)
        if response.status_code != 200:
            if response.status_code in {403, 404, 429} or response.status_code >= 500:
                return JobUpdate("running", error="Elicit paper export is temporarily unavailable", poll_after_s=30)
            raise ProviderError(ErrorKind.TRANSIENT, "Elicit export download failed")
        if len(response.content) > MAX_EXPORT_BYTES:
            return JobUpdate("failed", error="Elicit paper export exceeds the 10 MiB limit")
        papers = _csv_papers(response.text) if export_kind == "csv" else _ris_papers(response.text)
        result = Result(hits=[_hit(paper, ctx, rank, session_id) for rank, paper in enumerate(papers, 1)],
                        raw={"session_id": session_id, "session_kind": kind, "export_kind": export_kind,
                             "export_stage": stage, "rank_kind": "export_order"})
        return JobUpdate("completed", result=result)

    @staticmethod
    def _export(data: dict[str, Any], kind: str) -> tuple[str | None, str, str]:
        if kind == "systematic-reviews":
            stages = data.get("data") or {}
            for stage in ("extract", "fulltext", "screen", "search"):
                output = stages.get(stage)
                if isinstance(output, dict) and isinstance(output.get("csv"), str):
                    return output["csv"], "csv", stage
            report = stages.get("report") or {}
            if isinstance(report, dict) and report.get("ris"):
                return report["ris"], "ris", "bibliography"
        if isinstance(data.get("risUrl"), str):
            return data["risUrl"], "ris", "bibliography"
        return None, "", ""


ElicitProvider = ElicitAPIProvider
