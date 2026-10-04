#!/usr/bin/env python3
"""Measure a real reranker against a small, authored retrieval smoke set.

Run after installing ``.[local-rerank]``. This is a reproducible integration
benchmark, not a production relevance evaluation or a default-enable gate.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import math
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from research_engine.merge import merge_hits
from research_engine.merge.rerank import LOCAL_MODEL, rerank


def ndcg(labels: list[int], ideal_labels: list[int], count: int = 5) -> float:
    def dcg(values):
        return sum((2 ** relevance - 1) / math.log2(index + 2) for index, relevance in enumerate(values[:count]))

    ideal = dcg(sorted(ideal_labels, reverse=True))
    return dcg(labels) / ideal if ideal else 0.0


def mrr(labels: list[int]) -> float:
    return next((1 / (index + 1) for index, label in enumerate(labels) if label == 2), 0.0)


def percentile(values: list[float], proportion: float) -> float:
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * proportion) - 1)]


async def benchmark(args) -> dict:
    cases = json.loads(args.cases.read_text())
    options = {
        "enabled": True, "backend": args.backend, "model": args.model,
        "threads": args.threads, "timeout_s": args.timeout, "top_n": 40,
        "cache_dir": str(args.cache_dir),
    }
    if args.endpoint:
        options["endpoint"] = args.endpoint
    prepared = []
    for case in cases:
        fused = merge_hits([
            {"provider": "benchmark", "rank": index + 1, "title": doc["title"], "snippet": doc["text"],
             "url": f"https://benchmark.invalid/{case['id']}/{doc['id']}"}
            for index, doc in enumerate(case["documents"])
        ], limit=None).items
        prepared.append((case, fused, {doc["id"]: doc["relevance"] for doc in case["documents"]}))
    cold_started = time.perf_counter()
    diagnostics = {}
    await rerank(prepared[0][0]["query"], prepared[0][1], {**options, "diagnostics": diagnostics})
    cold_ms = (time.perf_counter() - cold_started) * 1000
    if diagnostics.get("status") != "applied":
        raise RuntimeError(f"Real reranking was unavailable: {diagnostics}")
    latencies = []
    rows = []
    for repetition in range(args.repetitions):
        for case, fused, grades in prepared:
            diagnostics = {}
            started = time.perf_counter()
            ranked = await rerank(case["query"], fused, {**options, "diagnostics": diagnostics})
            elapsed = (time.perf_counter() - started) * 1000
            if diagnostics.get("status") != "applied":
                raise RuntimeError(f"Real reranking fell back: {diagnostics}")
            latencies.append(elapsed)
            if repetition:
                continue
            baseline_ids = [item["url"].rsplit("/", 1)[-1] for item in fused]
            reranked_ids = [item["url"].rsplit("/", 1)[-1] for item in ranked]
            baseline_labels = [grades[identifier] for identifier in baseline_ids]
            reranked_labels = [grades[identifier] for identifier in reranked_ids]
            rows.append({
                "id": case["id"], "query": case["query"],
                "baseline": baseline_ids, "reranked": reranked_ids, "labels": grades,
                "ndcg_at_5_baseline": ndcg(baseline_labels, list(grades.values())),
                "ndcg_at_5_reranked": ndcg(reranked_labels, list(grades.values())),
                "mrr_baseline": mrr(baseline_labels), "mrr_reranked": mrr(reranked_labels),
                "overlap_at_3": len(set(baseline_ids[:3]) & set(reranked_ids[:3])) / 3,
            })
    return {
        "kind": "authored-smoke-set", "measured_at": datetime.now(timezone.utc).isoformat(),
        "backend": args.backend, "model": args.model, "threads": args.threads,
        "python": platform.python_version(), "platform": platform.platform(),
        "fastembed_version": importlib.metadata.version("fastembed") if args.backend == "fastembed" else None,
        "query_count": len(cases), "documents_per_query": [len(case["documents"]) for case in cases],
        "warm_repetitions": args.repetitions, "cold_start_ms": round(cold_ms, 3),
        "warm_latency_ms_p50": round(statistics.median(latencies), 3),
        "warm_latency_ms_p95": round(percentile(latencies, 0.95), 3),
        "mean_ndcg_at_5_baseline": statistics.mean(row["ndcg_at_5_baseline"] for row in rows),
        "mean_ndcg_at_5_reranked": statistics.mean(row["ndcg_at_5_reranked"] for row in rows),
        "mean_mrr_baseline": statistics.mean(row["mrr_baseline"] for row in rows),
        "mean_mrr_reranked": statistics.mean(row["mrr_reranked"] for row in rows),
        "mean_overlap_at_3": statistics.mean(row["overlap_at_3"] for row in rows),
        "limitations": "Eight authored cases, six documents each, ordinal manual labels, no live provider candidates. Does not establish production relevance or justify enabling reranking by default.",
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="fastembed", choices=["fastembed", "infinity", "jina", "cohere", "voyage"])
    parser.add_argument("--model", default=LOCAL_MODEL)
    parser.add_argument("--endpoint")
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/rerank_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/local_cpu.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "research-engine" / "rerank")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1 or args.threads < 1:
        parser.error("repetitions and threads must be positive")
    result = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
