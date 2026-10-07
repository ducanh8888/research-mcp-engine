# Development and verification

Use Python 3.12+ and [uv](https://docs.astral.sh/uv/). The lockfile is committed; do not regenerate it solely to work around a local install problem. From a clean checkout:

```bash
uv sync --frozen --extra dev
uv lock --check
uv run --frozen pytest -q
uv run --frozen ruff check src tests scripts
git diff --check
uv build
```

There is no configured type checker; Ruff is the repository lint check. CI also builds the frozen Docker image and verifies that installed package versions/assets match the lock. Fixture tests use isolated databases/transports and should not spend provider credits. The optional local rerank extra downloads a model and has a separate [limited benchmark](../benchmarks/README.md); it is not a standard test prerequisite.

For an isolated local server, run `research-engine init --config config.yaml` once and `research-engine serve --config config.yaml` with `uv run --frozen` as in [deployment](deployment.md). `scripts/mcp_smoke.py --token-file data/bootstrap.json` checks health and authenticated discovery; passing `--tool` intentionally makes a provider call. Never paste bootstrap credentials or API responses containing sensitive queries into a public issue.

Relevant directories:

| Path | Responsibility |
|---|---|
| `src/research_engine/server/` | FastAPI/FastMCP mount, MCP Bearer auth, public tool schemas |
| `src/research_engine/router/` | request validation, routing, account selection/limits, coverage, redaction |
| `src/research_engine/providers/` | explicit OmniRoute bridge, academic, web/local, developer and hosted adapters |
| `src/research_engine/merge/` | canonical IDs, conservative clustering, RRF, optional rerank |
| `src/research_engine/admin/` | SQLAdmin operations and upstream account Connect |
| `src/research_engine/jobs/`, `storage/`, `cache.py` | persisted jobs, migrations/encrypted state, caching |
| `tests/` | transport/fixture tests of public MCP behavior and provider contracts |

A provider class being registered is not sufficient evidence of live access. When changing a route, exercise real MCP calls in a safe environment and distinguish fixture passes, live upstream results, and actual external MCP clients. Preserve attribution in [NOTICE](../NOTICE) and the retained web-layer license. See [architecture](architecture.md), [providers](providers.md), and [routing and fusion](routing-and-fusion.md).
