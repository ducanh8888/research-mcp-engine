# Private deployment and validation

This guide records the baseline CLI/Compose path and follows [design v4](design.md).
Current work and acceptance are in [roadmap.md](roadmap.md). The baseline implements
one process, SQLite WAL, encrypted secrets, handles/jobs, MCP `/mcp` and sqladmin
`/admin`; application/container/live acceptance is not established by this documentation pass.

The OmniRoute bridge is not implemented at the frozen checkpoint. Commodity keys
below apply to existing direct adapters only; after verified cutover, manage those
keys/accounts in OmniRoute and only the bridge service credential in the engine.
Do not invent a bridge environment variable or advertise it as configurable before it ships.

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

## Validation record

The first two entries are prior implementation reports retained for context; they
were not rerun in the 2026-10-05 documentation pass. No command output from that
pass establishes deployment acceptance.

Update this table with commands actually executed in the deployment environment.
Fixture tests verify local behavior and do not establish hosted-provider or remote
client compatibility.

| Check | Reported baseline validation / remaining check |
|---|---|
| Compose syntax | `docker compose config --quiet` passed locally |
| Smoke client CLI | `scripts/mcp_smoke.py --help` passed in Python 3.12 with FastMCP 4.0.10 |
| Container build and health | Pending implementation integration |
| HTTP MCP discovery and calls | Pending implementation integration |
| Missing/revoked tokens and bad Origin/Host | Pending HTTP tests |
| Failover and restart handle read | Pending HTTP tests |
| Live REST providers | See separately recorded live checks; not implied by fixture results |
| Hosted MCP initialize/list/call and OAuth refresh | Pending eligible account checks |
| Another tailnet peer: discovery and calls | Pending reachable peer and private token |
| Claude Code and agentRT remote MCP | Pending actual client execution; unavailable agentRT stays pending |
| Off-tailnet port denial | Pending external network check |

The [definition of done](roadmap.md#definition-of-done) requires relevant phase
exit criteria, including actual remote client checks. A local fixture pass alone
does not establish private-release acceptance.
