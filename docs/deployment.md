# Configuration and deployment

The supported MCP transport is **Streamable HTTP** on `/mcp`; there is no stdio mode or client-facing OAuth authorization server. Run a single process with SQLite WAL and persistent local data. Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for a local installation; Docker Compose is optional.

## First run (loopback)

From the repository root, on a fresh checkout or with an existing runtime that you intend to preserve:

```bash
uv sync --frozen --extra dev
cp -n .env.example .env
uv run --frozen research-engine init --config config.yaml
uv run --frozen research-engine serve --config config.yaml
```

`init` creates `config.yaml`, `data/engine.db`, an encryption key, an admin session key, and `data/bootstrap.json` with one-time admin credentials and an MCP client Bearer token. Newly written secret/config files use mode 0600; protect the data directory and database with host filesystem permissions. It does not overwrite an existing configuration; back up the database and matching encryption key together. Treat the bootstrap file as a secret because it contains both the password and a client token. `config.example.yaml` documents the YAML fields but has a placeholder hash, so run `init` rather than trying to serve directly from that example.

`init` reads nonempty keys from `.env` or the environment and imports them into the encrypted account store. After editing `.env`, run `uv run --frozen research-engine import-env --config config.yaml` to import updated keys. `OMNI_ROUTE_API_URL` must be an OmniRoute API root ending in `/v1` or `/api/v1`; `OMNI_ROUTE_API_KEY` is the connection credential. Configure commodity provider accounts in OmniRoute, not in this gateway. Direct specialist accounts, provider options, enabled routes, and reranking can be managed in `/admin` after login. Empty optional values in `.env.example` are not imported.

The default bootstrap listens only on `127.0.0.1:8765`, with `public_base_url` and trusted origins for local HTTP. Check liveness and MCP discovery without using provider credits:

```bash
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json
```

To call a provider deliberately, use `--tool paper_search --arguments '{"query":"open access reproducibility","limit":3}'` with the same script. A request can be partial or unavailable if an upstream is not configured, eligible, or reachable. The script prints responses, so never pass sensitive research queries or unredacted provider material when sharing its output.

## Client connection

Point an MCP client that supports **Streamable HTTP and custom Bearer headers** at `http://127.0.0.1:8765/mcp`. Set its `Authorization: Bearer <client-token>` header from a securely stored token, not a URL query argument. The private `data/bootstrap.json` is a local one-time secret; create a separate token for each application with:

```bash
uv run --frozen research-engine token create --config config.yaml 'my-client'
```

The token appears once on standard output; store it privately. Revoke by numeric ID with `research-engine token revoke --config config.yaml <id>`. The server validates tokens on each MCP request and rejects unknown/revoked tokens. `/admin` is a separate password-protected SQLAdmin interface with CSRF-checked mutations.

## Docker Compose

`Dockerfile` installs the frozen production lock into a pinned image. Compose uses a read-only root, dropped capabilities, no-new-privileges, a tmpfs for temporary files, and persistent `./data` and read-only `./config.yaml` mounts. By default only loopback is published:

```bash
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json
```

Initialize first so the volume-mounted runtime files exist. The container listens on `0.0.0.0:8765` internally; the host publish is `${RESEARCH_ENGINE_BIND_HOST:-127.0.0.1}:8765:8765`. Change `RESEARCH_ENGINE_BIND_HOST` only to a **locally owned** address reachable by your own TLS proxy/tunnel and restrict access with host/network controls. Do not publish the admin dashboard or bearer MCP endpoint directly to an untrusted network. Match `RESEARCH_ENGINE_UID` and `RESEARCH_ENGINE_GID` to the owner of mode-0600 runtime files (default 1000). A container restart must retain the same `config.yaml`, `data/`, SQLite WAL, and key files.

## HTTPS and upstream account OAuth

The repository does not configure or operate a TLS proxy, tunnel, DNS, or firewall. If you add your own HTTPS ingress, set the ignored runtime `config.yaml` `public_base_url` to your HTTPS origin and `trusted_origins` to the origins you deliberately allow. Preserve the existing password hash, keys, accounts, routes, and data. The upstream OAuth Connect callback is exactly `<public_base_url>/admin/oauth/callback`; register that exact redirect URI with the provider and start Connect from an authenticated admin session. Session cookies are Secure when the configured public base URL is HTTPS. An external provider's OAuth consent screen may require account-owner action. OAuth for an upstream account does not replace the MCP client's Bearer token authentication.

Never enable proxy-header trust or a new auth proxy merely because a tunnel exists; first check the real public Host/Origin, redirects, cookies, and callback. The server performs MCP Host and optional Origin validation. If a client is remote, use HTTPS and a dedicated client token; do not place the bootstrap JSON on that machine.

## Backup and upgrades

Keep SQLite, WAL, blobs, `config.yaml`, encryption key, and session secret together. Use SQLite's online backup API while running, or stop the service before copying files. Restore into a separate runtime directory before replacing a working database. Do not run multiple replicas against one SQLite directory. For an upgrade, stop the service, run `uv run --frozen research-engine migrate --config config.yaml`, rebuild the image if used, restart, then run health and discovery checks. The matching encryption key is required to read stored provider credentials.

## Security and limitations

- `.env`, `config.yaml`, and `data/` are ignored by Git and excluded from the Docker build context; do not commit tokens, copied logs, or database exports.
- A `fresh` request bypasses the engine cache but cannot force every upstream service to bypass its own cache. A failing source yields `partial` coverage when peers succeed; some provider operations require paid entitlements.
- Direct and hosted research adapters have different account/plan requirements. Default routes do not imply every provider is immediately configured or authorized.
- This repository does not promise a hosted service, managed uptime, independent multi-client production validation, or a privacy policy for external providers. Review each provider's data-handling terms before sending queries.
