"""TTL caches for complete queries and question-independent source documents."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from research_engine.storage.blobs import BlobStore
from research_engine.storage.db import Database, DocumentCache, QueryCache, utcnow


def cache_key(capability: str, args: dict[str, Any]) -> str:
    normalized = {k: v for k, v in args.items()
                  if k not in {"fresh", "deadline_s", "wait_s"} and not k.startswith("_")}
    if isinstance(normalized.get("query"), str):
        normalized["query"] = " ".join(normalized["query"].split())
    payload = json.dumps([capability, normalized], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class Cache:
    def __init__(self, db: Database, blobs: BlobStore):
        self.db, self.blobs = db, blobs

    def get_query(self, capability: str, args: dict[str, Any]) -> dict[str, Any] | None:
        with self.db.session() as session:
            row = session.get(QueryCache, cache_key(capability, args))
            if row is None or row.expires_at <= utcnow():
                if row is not None:
                    session.delete(row)
                return None
            return dict(row.payload)

    def put_query(self, capability: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
        if payload.get("status") != "complete":
            return
        ttl = 3600 if capability in {"web_search", "news_search"} else 86400
        with self.db.session() as session:
            session.merge(QueryCache(key=cache_key(capability, args), payload=payload,
                                     expires_at=utcnow() + timedelta(seconds=ttl)))

    def get_document(self, capability: str, target: str) -> dict[str, Any] | None:
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
        text = document.get("text")
        if not isinstance(text, str):
            return
        key = cache_key(capability, {"target": target, "cursor": None})
        ref = self.blobs.put(text.encode())
        metadata = {k: v for k, v in document.items() if k != "text"}
        with self.db.session() as session:
            session.merge(DocumentCache(key=key, blob_ref=ref, metadata_json=metadata,
                                        expires_at=utcnow() + timedelta(days=7)))

    def clear(self) -> dict[str, int]:
        with self.db.session() as session:
            query_count = session.execute(delete(QueryCache)).rowcount
            document_count = session.execute(delete(DocumentCache)).rowcount
        return {"queries": query_count, "documents": document_count}
