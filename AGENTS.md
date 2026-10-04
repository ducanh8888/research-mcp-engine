# Repository Guidelines

## Current state and authority

Code baseline: `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`.
`main` is frozen. Work on `research-specialists`; do not advance main without an owner instruction.

Read [requirements](docs/requirements-notes.md), [design v4](docs/design.md),
[roadmap](docs/roadmap.md), [cleanup](docs/cleanup.md) and [code audit](docs/code-audit.md).
Requirements record owner scope; design defines contracts; roadmap owns sequencing/acceptance.
Research/01–07 is historical evidence, not an implementation checklist.

The engine is implemented in part: `src/`, `tests/`, migrations, Docker and CLI exist.
Hosted-MCP normalized adapters and the OmniRoute bridge are missing at baseline.
Do not call a phase done because modules/fixtures exist.

## Scope

Defer direct development for search/fetch/API rerank operations already covered by OmniRoute.
Use one small explicit-provider bridge. Build research-specialist gaps and their multi-account
selection: priority, round-robin, quota-aware. Commodity accounts stay upstream in OmniRoute.

Reuse compatible code, preserve attribution and avoid importing a model router's control plane.
Deferred items are not an automatic follow-on queue. Remove/replace marked shortcuts using
their closure criteria; preserve validated fallback, OAuth and restart semantics.

## Structure

`src/research_engine/`: server, router, providers, merge, jobs, admin, storage, cache/config/CLI.
`tests/`: pytest fixtures/transport tests. `docs/`: current contracts and historical notes.
`scripts/`: MCP smoke and optional rerank benchmark. `benchmarks/`: limited recorded evidence.

## Development commands

From the root with Python ≥3.12 and uv available:

```bash
uv sync --frozen --extra dev
uv run --frozen pytest
uv run --frozen ruff check src tests scripts
git diff --check
```

Local install/run commands are in [deployment.md](docs/deployment.md).
Do not run paid/live calls implicitly as part of a fixture test.
Do not change the lockfile merely to make a local environment pass.

For documentation-only edits, check relative links, referenced paths, consistency and whitespace;
application tests are not required unless executable behavior also changes.
For code edits, run focused meaningful checks plus required phase checks.

## Validation and commits

Record actual commands/results and exact blockers. Separate static source inspection,
fixture tests, recorded artifacts, live providers and actual remote clients.
Missing credentials/eligible plans leave checks pending, not successful.

Keep commits focused; review tracked and new files. Preserve operator routes, data, credentials
and notices during cutover. Python: four-space indentation, snake_case functions/modules,
PascalCase classes; follow existing Ruff configuration.

## Secrets

Never commit API keys, OAuth tokens, bearer tokens, runtime config, encryption/session keys or
bootstrap credentials. Store account secrets through the encrypted store; redact logs/examples.
Client bearer tokens must never be forwarded to an upstream provider or OmniRoute.
