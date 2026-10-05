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

## Owner directive — evidence coverage

Optimize research requests for evidence coverage and result quality, not fallback.

For every search-like capability (`web_search`, `news_search`, `paper_search`,
`paper_related`, `developer_search`, `repo_search`), execute all enabled, configured,
semantically independent providers in the route concurrently within one deadline.
Never use first-success-wins or provider fallback chains.

Count each real upstream provider once. Never treat direct + OmniRoute access to the
same provider/operation as separate evidence. Covered commodity operations use the
explicit OmniRoute provider; do not silently fall back to the frozen direct adapter
if OmniRoute fails. Specialist sources remain direct.

Merge all successful results with provenance → conservative exact dedup → plain RRF →
validated rerank only when enabled. One provider failing or timing out only reduces
coverage; preserve successful peers and return partial.

Keep multi-account failover inside a provider for auth/rate/quota availability only.
Accounts never become independent search votes.

Preserve sequential execution only where the operation itself requires it: fetching
the same source (`web_read`/`paper_read`), resolving unresolved metadata IDs, or starting
exactly one async job to avoid duplicate work. `citation_verify`, `citation_graph`,
and `editorial_check` fan out to applicable providers and aggregate source assertions;
do not RRF them.

Do not add fallback architecture, provider chains, scoring policy, classifiers, or
other control-plane machinery. One MCP search request should gather the strongest
available independent evidence set in that single request.

This instruction overrides earlier search-provider fallback language. Apply the open
C33 correction in [cleanup](docs/cleanup.md) and its [acceptance checks](docs/roadmap.md#evidence-coverage-correction).
Existing implementation and test counts do not establish compliance with this directive.

## Scope

Defer direct development for search/fetch/API rerank operations already covered by OmniRoute.
Use one small explicit-provider bridge. Build research-specialist gaps and their multi-account
selection: priority, round-robin, quota-aware. Commodity accounts stay upstream in OmniRoute.

Reuse compatible code, preserve attribution and avoid importing a model router's control plane.
Deferred items are not an automatic follow-on queue. Remove/replace marked shortcuts using
their closure criteria; preserve account availability retries, sequential source/metadata
progression, OAuth and restart semantics.

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
