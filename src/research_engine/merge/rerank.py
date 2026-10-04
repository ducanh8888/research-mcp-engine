"""Optional rerank backends; a failed service always preserves the fused order.

Jina document limits, index validation and partial-ranking padding are adapted
from vvzvlad/research-mcp src/rerank.py @11f297d (MIT, copyright 2026 vvzvlad).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BACKENDS = {
    "infinity": {"endpoint": "http://127.0.0.1:7997/rerank", "model": "BAAI/bge-reranker-v2-m3"},
    "jina": {"endpoint": "https://api.jina.ai/v1/rerank", "model": "jina-reranker-v2-base-multilingual", "key_env": "JINA_API_KEY"},
    "cohere": {"endpoint": "https://api.cohere.com/v2/rerank", "model": "rerank-v3.5", "key_env": "COHERE_API_KEY"},
    "voyage": {"endpoint": "https://api.voyageai.com/v1/rerank", "model": "rerank-2.5", "key_env": "VOYAGE_API_KEY"},
}
LOCAL_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


def _config(settings: Any) -> dict[str, Any]:
    if settings is None:
        return {}
    if isinstance(settings, Mapping):
        return dict(settings.get("rerank", settings))
    if hasattr(settings, "rerank"):
        return _config(settings.rerank)
    if hasattr(settings, "model_dump"):
        return _config(settings.model_dump())
    raise TypeError("rerank settings must be a mapping or Settings object")


def _documents(items: list[dict[str, Any]], max_chars: int) -> list[str]:
    return [
        ("\n".join(str(item.get(name) or "") for name in ("title", "snippet")).strip()
         or str(item.get("url") or item.get("handle") or "Untitled evidence"))[:max_chars]
        for item in items
    ]


def _validated_order(ranking: Any, count: int) -> list[int]:
    if not isinstance(ranking, list) or not ranking:
        raise ValueError("Reranker response must contain a nonempty ranking list")
    order: list[int] = []
    seen: set[int] = set()
    for entry in ranking:
        if not isinstance(entry, Mapping):
            raise ValueError("Invalid reranker result item")
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
            raise ValueError("Invalid reranker document index")
        for field in ("relevance_score", "score"):
            if field in entry and (isinstance(entry[field], bool) or not isinstance(entry[field], (int, float)) or not math.isfinite(entry[field])):
                raise ValueError("Invalid reranker relevance score")
        if index not in seen:
            seen.add(index)
            order.append(index)
    order.extend(index for index in range(count) if index not in seen)
    return order


@lru_cache(maxsize=2)
def _local_model(model: str, cache_dir: str | None, threads: int):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(model_name=model, cache_dir=cache_dir, threads=threads, providers=["CPUExecutionProvider"])


def _local_ranking(query: str, documents: list[str], options: Mapping[str, Any]) -> list[dict[str, Any]]:
    model = _local_model(
        str(options.get("model") or LOCAL_MODEL), options.get("cache_dir"), int(options.get("threads", 2)),
    )
    scores = list(model.rerank(query, documents))
    if len(scores) != len(documents):
        raise ValueError("Local reranker returned an incomplete score list")
    ranking = [{"index": index, "score": float(score)} for index, score in enumerate(scores)]
    return sorted(ranking, key=lambda result: (-result["score"], result["index"]))


async def _http_ranking(
    backend: str, query: str, documents: list[str], options: Mapping[str, Any], client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    defaults = _BACKENDS[backend]
    key_env = str(options.get("api_key_env") or defaults.get("key_env") or "")
    key = options.get("api_key") or (os.environ.get(key_env) if key_env else None)
    if backend != "infinity" and not key:
        raise ValueError(f"Missing API key for {backend} reranking")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": options.get("model") or defaults["model"], "query": query,
        "documents": documents, "top_n": len(documents),
    }
    if backend == "jina":
        payload["return_documents"] = False
    response = await client.post(
        str(options.get("endpoint") or defaults["endpoint"]), json=payload, headers=headers,
        timeout=float(options.get("timeout_s", 8)),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, Mapping):
        raise ValueError("Malformed reranker response")
    return data.get("data" if backend == "voyage" else "results")


async def rerank(
    query: str, items: list[dict[str, Any]], settings: Any = None,
    *, options: Mapping[str, Any] | None = None, client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Reorder only the top N; preserve the tail and every original evidence item.

    Off by default. Optional ``diagnostics`` receives operational status without
    credentials, query text, or provider payloads. Cancellation is propagated.
    """
    config = _config(settings)
    config.update(options or {})
    diagnostics = config.get("diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    backend = str(config.get("backend", "infinity")).lower()
    diagnostics.update({"backend": backend, "status": "disabled"})
    if not config.get("enabled", False) or len(items) < 2:
        return list(items)
    try:
        count = min(int(config.get("top_n", 40)), len(items))
        if count < 2:
            return list(items)
        if count > 200:
            raise ValueError("Rerank top_n cannot exceed 200")
        max_chars = int(config.get("max_document_chars", 1000))
        if not 1 <= max_chars <= 10000:
            raise ValueError("Invalid reranker document size limit")
        timeout = float(config.get("timeout_s", 8))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid reranker timeout")
        documents = _documents(items[:count], max_chars)
        async with asyncio.timeout(timeout):
            if backend in {"fastembed", "local"}:
                ranking = await asyncio.to_thread(_local_ranking, query, documents, config)
            elif backend in _BACKENDS:
                if client is not None:
                    ranking = await _http_ranking(backend, query, documents, config, client)
                else:
                    async with httpx.AsyncClient(follow_redirects=False) as owned_client:
                        ranking = await _http_ranking(backend, query, documents, config, owned_client)
            else:
                raise ValueError("Unknown reranker backend")
        order = _validated_order(ranking, count)
        diagnostics.update({"status": "applied", "documents": count})
        return [items[index] for index in order] + items[count:]
    except Exception as exc:
        diagnostics.update({"status": "fallback", "error_type": type(exc).__name__})
        log.warning("Rerank backend %s failed (%s); keeping fused order", backend, type(exc).__name__)
        return list(items)
