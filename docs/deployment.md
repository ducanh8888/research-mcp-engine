# Private deployment and validation

This guide follows [design v4](design.md); phase acceptance is tracked in
[roadmap.md](roadmap.md). The current `research-specialists` branch runs one
process with SQLite WAL, encrypted secrets, handles/jobs, MCP `/mcp` and sqladmin
`/admin`. It implements an explicit-provider OmniRoute bridge and direct specialist
gaps. Some live provider and remote-client checks remain pending.

Set `OMNI_ROUTE_API_URL` to the gateway's `/v1` or `/api/v1` root and
`OMNI_ROUTE_API_KEY` in the ignored `.env` before `init` or `import-env`. Import
stores one encrypted `omniroute` connection account, not commodity account
copies. Manage provider keys and account pools for search/fetch/API rerank in
OmniRoute. Operator-edited connection options are not overwritten by import.

## Prerequisites

- Python 3.12 or later and uv for local setup and the smoke client.
- Docker with the Compose plugin for the container deployment. The image installs
  production dependencies from `uv.lock` using uv; Docker build requires access
  to the pinned uv image and locked package artifacts.
- A local tailnet interface owning `100.66.213.111` for the checked-in Compose
  bindings. Update that explicit binding if this host moves to another tailnet IP.
- Provider credentials in the ignored `.env` file, or accounts created in admin.

The container listens on all of its internal interfaces. Compose publishes only
`127.0.0.1:8765` and `100.66.213.111:8765` on the host. Keep those explicit host
addresses when changing ports. Remote tailnet ACLs and the host firewall remain
separate controls; validate them from another machine.

## First run

Run commands from the repository root. Use the existing `.env`, or create it
without replacing an existing file:

```bash
cp -n .env.example .env
uv sync --frozen --extra dev
uv run --frozen research-engine init --config config.yaml
```

`init` creates the runtime configuration, encryption key, admin session secret,
admin password hash, database, and initial client token. Configured environment
keys seed encrypted provider accounts. Read the one-time password and token from
the ignored `data/bootstrap.json` locally. The example YAML is a field reference;
its placeholder hash is not a runnable configuration.

Runtime paths in `config.yaml` resolve relative to that file. The default relative
`data` paths work both locally and in Compose, which mounts the same directory at
`/app/data`. Keep runtime files owned by the user that runs the process. Compose
uses UID/GID 1000 by default; set `RESEARCH_ENGINE_UID` and `RESEARCH_ENGINE_GID`
in `.env` if the directory owner differs.

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json
```

The image uses the committed `uv.lock` at build time (`uv sync --frozen`,
runtime-only, non-editable installation) and contains the CLI, packaged Python
modules, and smoke client. A lock refresh is a separate reviewed change; do not regenerate it to work
around a build failure. The smoke client runs locally against the container and
only checks health and tool discovery unless `--tool` is explicitly given.

Open `http://127.0.0.1:8765/admin` locally, or
`http://100.66.213.111:8765/admin` from an allowed tailnet peer. Log in with the
bootstrap admin credentials. Account setup, routing, upstream adapter settings,
client-token revocation, request inspection, job inspection, and cache operations
use the admin interface.

For a local process, use:

```bash
uv run --frozen research-engine serve --config config.yaml
```

The default local process binds `127.0.0.1`. A deliberate tailnet bind is:

```bash
uv run --frozen research-engine serve --config config.yaml --host 100.66.213.111 --port 8765
```

## MCP smoke checks

The smoke client checks `/health`, initializes the MCP HTTP transport, lists tools,
and requires `web_search` and `web_read`. Supply a token through
`RESEARCH_ENGINE_TOKEN`, a private raw-token file, or `data/bootstrap.json`.
Avoid putting tokens in URL query strings or command arguments.

To perform an explicit live search:

```bash
uv run --frozen python scripts/mcp_smoke.py \
  --url http://127.0.0.1:8765/mcp \
  --token-file data/bootstrap.json \
  --tool web_search \
  --arguments '{"query":"MCP research provenance","limit":3}'
```

Use a returned URL or handle as the read target:

```bash
uv run --frozen python scripts/mcp_smoke.py \
  --token-file data/bootstrap.json \
  --tool web_read \
  --arguments '{"target":"https://modelcontextprotocol.io/"}'
```

For a remote peer, copy only the issued token to a private file on that peer and
run the same client with `--url http://100.66.213.111:8765/mcp`. Test discovery,
search, and handle reads after a service restart. The bootstrap file contains an
admin password as well as the initial token; use a dedicated client token for a
remote machine.

The public tool schema is intentionally stable when some providers have no usable
accounts. Inspect each call's coverage and status; successful discovery does not
prove that all provider calls succeeded.

## Credentials, callbacks, and client tokens

Provider keys live in the encrypted account store after bootstrap. Empty example
values do not create a credentialed account. Manage subsequent changes in admin.
Client tokens are stored as hashes, and revocation takes effect on subsequent MCP
transport requests. Maintain separate client tokens per machine or application.

The encryption key and admin session secret are stable deployment files. Changing
the key without migrating encrypted account data prevents those accounts from
being decrypted. Restoring a database requires the matching encryption key.

Upstream OAuth connections have their own callback requirements. Configure an
HTTPS `public_base_url` and matching trusted origin; the implemented callback is
`/admin/oauth/callback`, on the same origin as admin Connect. A hosted provider
must accept the exact redirect URI before OAuth setup is called complete. Follow the provider's account and plan
requirements. OAuth initialization and refresh checks remain provider-specific;
plain MCP bearer authentication on this private server does not establish them.

## Persistence, backup, and upgrade

Compose persists the SQLite database and WAL, blobs, encryption key, session
secret, and bootstrap file through `./data`. A container replacement should retain
that directory and `config.yaml`. Do not run several service replicas against this
SQLite deployment.

Use SQLite's online backup API, or stop the service before copying database files.
A running database copy must include a consistent WAL snapshot. A minimal local
online backup for the default paths is:

```bash
mkdir -p data/backups
uv run --frozen python - <<'PY'
import sqlite3
with sqlite3.connect('data/engine.db') as source:
    with sqlite3.connect('data/backups/engine.db') as destination:
        source.backup(destination)
PY
```

Back up blobs, the matching encryption key, configuration, and session secret to
a private destination too. Treat the bootstrap file as a separate credential
artifact. Test restore using a separate runtime directory before replacing the
working database.

Run migration and upgrade as a single writer:

```bash
docker compose stop
uv run --frozen research-engine migrate --config config.yaml
docker compose build
docker compose up -d
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json
```

Jobs record their owner, upstream reference, checkpoint, retry times, and partial
result. A restart should recover eligible jobs. A previously cancelled job should
stay cancelled. Verify job recovery with the deterministic tests and relevant live
provider checks before relying on an upstream job's recovery behavior.

## Validation record (2026-10-06)

Commands below ran on this host with Python 3.12 and the committed lock. The
private `data/`, `config.yaml` and `.env` are not committed. A local tailnet URL
request is not an independent peer check; fixtures are not live entitlements.

| Check | Observed result |
|---|---|
| Lock, lint, fixtures | `uv sync --frozen --extra dev`, `uv lock --check`, `uv run --frozen pytest -q` (353 passed after absolute-deadline regression, dependency deprecation warnings), `uv run --frozen ruff check src tests scripts`, `git diff --check` passed after the redaction change |
| Container and private HTTP | `docker compose config --quiet`, `docker compose build`, `docker compose up -d`, `docker compose ps`: healthy on loopback and `100.66.213.111`. Locked runtime package versions match FastMCP 4.0.10, MCP 2.3.0, SQLAlchemy 2.1.3, HTTPX 0.28.1; image contains attribution notice |
| MCP discovery/auth | `scripts/mcp_smoke.py --token-file data/bootstrap.json` initialized/listed 17 tools; direct HTTP absent/invalid bearer returned 401; fixture tests cover revoked token, Origin and Host rejection |
| OmniRoute | Installed local container package 3.8.51; private service URL configured from existing `.env`. Authenticated `/v1/search` catalog lists 20 IDs, not account entitlements. Explicit duckduckgo-free and exa-search live MCP search returned results with upstream provider and transport provenance. Brave/Serper requests returned HTTP 400 no configured upstream credential. Jina Reader live `web_read` returned source text/handle; provider mismatch, 401/429/quota/cache semantics covered in fixtures, not live account balancing |
| Direct specialists | Live MCP paper search succeeded with OpenAlex, Crossref and arXiv on a later fresh retry; S2 returned 429. Crossref resolved a DOI through `paper_metadata`, GitHub `repo_search` succeeded, Firecrawl `site_map` returned two URLs |
| Jobs/restart | One bounded Firecrawl `site_crawl` started, container restarted and `get_job` returned completed persisted URLs/documents. Ownership/cancel/unknown-start covered in fixtures, not live second-client check |
| Rerank | OmniRoute Jina API rerank returned validated rankings using existing key; engine ordinary-search wiring and default-off verified in fixtures and local config. Enabling rerank on the live service was denied, so live MCP rerank remains pending. Local CPU evidence remains the recorded eight-case artifact, not a representative evaluation |
| Hosted OAuth/MCP | Scite/Elicit normalized search fixtures and locked SDK OAuth/concurrency fixtures pass; eligible authenticated tool schemas/calls, HTTPS callback/CIMD and Undermind remain pending |
| Clients and network | MCP calls to the local tailnet bind succeeded; independent peer, actual Claude Code/agentRT remote client and off-tailnet denial remain unverified. A Claude Code CLI agent run without per-action approval was denied; do not retry it indirectly |

See [roadmap.md](roadmap.md) for acceptance and [cleanup.md](cleanup.md) for
remaining closure criteria. Do not treat pending remote/client/hosted checks as PASS.
