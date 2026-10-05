"""TTL caches, bounded cache maintenance and historical retention reports."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from research_engine.storage.blobs import BlobStore
from research_engine.storage.db import Database, DocumentCache, Job, QueryCache, RequestRow, utcnow

# Expired cache is removed; historical request/job ages are reported only.
# An operator must approve a separate data-retention deletion policy.
REQUEST_RETENTION = timedelta(days=30)
JOB_RETENTION = timedelta(days=90)
MAINTENANCE_INTERVAL_S = 3600
MAINTENANCE_BATCH = 500
BLOB_GRACE_S = 3600


def cache_key(capability: str, args: dict[str, Any]) -> str:
    normalized = {k: v for k, v in args.items()
                  if k not in {"fresh", "deadline_s", "wait_s"}
                  and (not k.startswith("_") or k == "_route_identity")}
    if isinstance(normalized.get("query"), str):
        normalized["query"] = " ".join(normalized["query"].split())
    payload = json.dumps([capability, normalized], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class Cache:
    def __init__(self, db: Database, blobs: BlobStore):
        self.db, self.blobs = db, blobs
        self._last_maintenance = 0.0
        self.maintain()

    def _periodic_maintenance(self) -> None:
        if time.monotonic() - self._last_maintenance >= MAINTENANCE_INTERVAL_S:
            self.maintain()

    def get_query(self, capability: str, args: dict[str, Any]) -> dict[str, Any] | None:
        self._periodic_maintenance()
        with self.db.session() as session:
            row = session.get(QueryCache, cache_key(capability, args))
            if row is None or row.expires_at <= utcnow():
                if row is not None:
                    session.delete(row)
                return None
            return dict(row.payload)

    def put_query(self, capability: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
        self._periodic_maintenance()
        if payload.get("status") != "complete":
            return
        ttl = 3600 if capability in {"web_search", "news_search"} else 86400
        with self.db.session() as session:
            session.merge(QueryCache(key=cache_key(capability, args), payload=payload,
                                     expires_at=utcnow() + timedelta(seconds=ttl)))

    def get_document(self, capability: str, target: str) -> dict[str, Any] | None:
        self._periodic_maintenance()
        key = cache_key(capability, {"target": target, "cursor": None})
        with self.db.session() as session:
            row = session.get(DocumentCache, key)
            if row is None or row.expires_at <= utcnow():
                if row is not None:
                    session.delete(row)
                return None
            try:
                return {**row.metadata_json, "text": self.blobs.read_text(row.blob_ref)}
            except (OSError, ValueError):
                session.delete(row)
                return None

    def put_document(self, capability: str, target: str, document: dict[str, Any]) -> None:
        self._periodic_maintenance()
        text = document.get("text")
        if not isinstance(text, str):
            return
        key = cache_key(capability, {"target": target, "cursor": None})
        metadata = {k: v for k, v in document.items() if k != "text"}
        # Prevent local maintenance from sweeping a ref before its DB write commits.
        with self.blobs.lock:
            ref = self.blobs.put(text.encode())
            with self.db.session() as session:
                session.merge(DocumentCache(key=key, blob_ref=ref, metadata_json=metadata,
                                            expires_at=utcnow() + timedelta(days=7)))

    def clear(self, kind: str = "all") -> dict[str, int]:
        """Clear selected caches, reclaiming only blobs no longer referenced by any document."""
        if kind not in {"all", "query", "document"}:
            raise ValueError("Choose query, document, or all caches")
        counts = {"queries": 0, "documents": 0, "blobs": 0}
        with self.blobs.lock:
            with self.db.session() as session:
                if kind in {"all", "query"}:
                    counts["queries"] = session.execute(delete(QueryCache)).rowcount
                removed_refs = set()
                if kind in {"all", "document"}:
                    removed_refs = set(session.scalars(select(DocumentCache.blob_ref)))
                    counts["documents"] = session.execute(delete(DocumentCache)).rowcount
            # Recheck after commit, including refs in other remaining cache rows.
            with self.db.session() as session:
                keep = set(session.scalars(select(DocumentCache.blob_ref)))
            for ref in removed_refs - keep:
                self.blobs.delete(ref)
                counts["blobs"] += 1
        return counts

    def maintain(
        self, *, dry_run: bool = False, now: datetime | None = None,
        limit: int = MAINTENANCE_BATCH,
    ) -> dict[str, int]:
        """Prune bounded expired cache rows/orphans and report historical candidates.

        Requests and jobs are never deleted automatically. Their age windows
        are reporting thresholds only, pending an explicitly approved retention
        policy. Startup and periodic cache traffic run one bounded pass; repeat
        if more than one batch of expired cache data has accumulated.
        """
        if limit < 1:
            raise ValueError("Maintenance needs a positive batch size")
        now = now or utcnow()
        if now.tzinfo is None:
            raise ValueError("Maintenance time must be timezone-aware")
        now = now.astimezone(UTC)
        counts = {"queries": 0, "documents": 0, "requests": 0, "jobs": 0, "blobs": 0}
        with self.blobs.lock:
            with self.db.session() as session:
                conditions = (
                    ("queries", QueryCache, QueryCache.key, QueryCache.expires_at < now),
                    ("documents", DocumentCache, DocumentCache.key, DocumentCache.expires_at < now),
                    ("requests", RequestRow, RequestRow.id,
                     RequestRow.status != "running", RequestRow.finished_at < now - REQUEST_RETENTION),
                    ("jobs", Job, Job.id, Job.status.in_(("completed", "failed", "cancelled")),
                     Job.updated_at < now - JOB_RETENTION),
                )
                for name, model, key, *filters in conditions:
                    candidates = list(session.scalars(select(key).where(*filters).limit(limit)))
                    counts[name] = len(candidates)
                    if candidates and not dry_run and name in {"queries", "documents"}:
                        session.execute(delete(model).where(key.in_(candidates)))
            with self.db.session() as session:
                # On dry-run, all original refs are still present. Expired rows
                # selected for deletion are excluded to give a useful estimate.
                if dry_run:
                    expiring = set(session.scalars(select(DocumentCache.key).where(
                        DocumentCache.expires_at < now).limit(limit)
                    ))
                    keep = set(session.scalars(select(DocumentCache.blob_ref).where(
                        DocumentCache.key.not_in(expiring)
                    )))
                else:
                    keep = set(session.scalars(select(DocumentCache.blob_ref)))
            counts["blobs"] = self.blobs.cleanup(
                keep, max_age_s=BLOB_GRACE_S, now=now.timestamp(),
                dry_run=dry_run, limit=limit,
            )
        if not dry_run:
            self._last_maintenance = time.monotonic()
        return counts
