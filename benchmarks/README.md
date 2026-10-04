# Optional reranking benchmark

Run from the repository root after installing the CPU extra:

```bash
.venv/bin/pip install -e '.[local-rerank]'
.venv/bin/python scripts/benchmark_rerank.py --repetitions 3
```

The first run downloads `Xenova/ms-marco-MiniLM-L-6-v2` into
`~/.cache/research-engine/rerank`. The benchmark refuses to report success when
the reranker fails or falls back to RRF.

## Measured CPU result

[The recorded run](results/local_cpu.json) used fastembed 0.8.1, ONNX CPU
execution, two inference threads, eight queries, six candidates per query,
and three warm repetitions:

| Metric | Plain RRF | CPU rerank |
| --- | ---: | ---: |
| Mean nDCG@5 | 0.653 | 0.965 |
| Mean reciprocal rank | 0.333 | 1.000 |

Warm reranking latency was 60 ms median and 85 ms p95. The 5.6 second cold
run included downloading and loading the model. Mean overlap of the first
three results was 0.458.

## Interpretation

The cases are authored integration fixtures with manual ordinal relevance
labels, not live provider results or a representative research evaluation.
They confirm real model execution and reproducible measurement. Reranking
remains disabled by default; evaluate your own queries before enabling it.

Infinity, Jina, Cohere and Voyage request shapes and failure behavior are
verified by transport fixtures. This artifact does not claim live calls to
those HTTP services.

Deferred `weighted_rrf`, `hierarchical_rrf`, `fuzzy_relationships`,
`near_duplicate_links` and `version_links` options are also disabled by
default. Relationships preserve distinct IDs and provenance; they do not
collapse preprints, journal articles or conflicting evidence.
