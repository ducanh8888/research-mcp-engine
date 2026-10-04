# Research Engine

Private MCP for research retrieval, verification, conservative merge and ranking.
Current code baseline: `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`.
`main` is frozen; current work is on `research-specialists`.

The repository has runtime, provider, admin, auth, storage, jobs/cache/merge code and fixtures.
The OmniRoute bridge and normalized hosted-MCP adapters are still pending.
No end-to-end private-release acceptance is claimed.

## Current direction

OmniRoute owns covered generic search/fetch/API rerank operations and their account pools.
Research Engine owns specialist providers, specialist multi-account selection, provenance,
handles, typed evidence results, exact dedup, RRF and async specialist jobs.

Existing direct commodity adapters remain at the checkpoint; retirement follows verified
bridge cutover. Public ingress and historical Deferred machinery are outside the current queue.

## Documentation

| File | Purpose |
|---|---|
| [Requirements](docs/requirements-notes.md) | Current owner scope |
| [Design v4](docs/design.md) | Runtime contracts and operation ownership |
| [Roadmap](docs/roadmap.md) | Baseline evidence, ordered work and acceptance |
| [Required cleanup](docs/cleanup.md) | Source-grounded removals/replacements and closure checks |
| [Code audit](docs/code-audit.md) | Additional main defects, isolated reproductions and repair boundaries |
| [Deployment](docs/deployment.md) | Baseline local/Compose commands and pending checks |
| [Research](docs/research.md) | Current conclusions and historical evidence index |
| [Benchmark](benchmarks/README.md) | Recorded eight-case local rerank measurement and limits |

## Local setup

Python ≥3.12:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/research-engine init --config config.yaml
.venv/bin/research-engine serve --config config.yaml
```

Bootstrap secrets are written to ignored private runtime files. Follow the deployment guide
before configuring providers, remote clients or Docker; the checked-in tailnet IP is host-specific.
For development with uv, use the frozen-lock commands in [AGENTS.md](AGENTS.md).

## Attribution

Historical code references include `vvzvlad/research-mcp` @11f297d and
`R0Wi/mcp-gateway` @59c1efd. See the existing
[web-layer notice](src/research_engine/providers/web/LICENSE.research-mcp).
Complete copied-code/license inventory is P0 work; reference to a repository is not a claim
that its entire implementation was copied. OmniRoute is an upstream integration dependency.
