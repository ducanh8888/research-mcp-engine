# 02 — mcp-gateway deep-read + FastMCP vs python-sdk

Status: research input for the design doc. Date: 2026-09-29.
Sources read: `R0Wi/mcp-gateway` @ `59c1efd` (2026-09-23, "upgrade to FastMCP 4 (MCP SDK v2, 2026-07-28 protocol)");
wheels `fastmcp==4.0.10`, `fastmcp-slim==4.0.10`, `fastmcp-tasks==4.0.10`, `mcp==2.2.0`, `mcp-types==2.2.0`, `httpx2==2.13.1`
(unpacked and read). Paths below: `gw/` = `src/mcp_gateway/` of mcp-gateway; `fm/` = `fastmcp/`; `sdk/` = `mcp/`.

Licensing: mcp-gateway has **no LICENSE file**; `pyproject.toml:7` says `license = { text = "MIT" }`. Author permission
granted; credit in README anyway. FastMCP (repo now `PrefectHQ/fastmcp`) is Apache-2.0 (wheel metadata). `mcp` SDK is MIT.

---

## 1. mcp-gateway architecture

~3,000 LOC Python (`gw/*.py`), Svelte 5 UI (~1.1k LOC), 14 test files that run real uvicorn servers (not mocks), incl. a
second gateway acting as an OAuth-protected upstream (`tests/test_upstream_oauth.py`).

### 1.1 Assembly (`gw/app.py`, 181 lines)
- `create_app()` (`app.py:61-181`): `Storage` → `GatewayOAuthProvider` → `BackendManager` → one FastMCP `Client` per enabled
  backend (`app.py:72-76`) → `build_gateway()` → `mcp.http_app(path="/mcp")` (`app.py:82`).
- Outer app is **FastAPI**; the FastMCP ASGI app is **mounted as catch-all at `/`** (`app.py:179`). So `/mcp`, `/authorize`,
  `/token`, `/register`, `/revoke`, `/.well-known/*` are served by FastMCP/SDK handlers; only `/auth/api/*`, `/oauth/*`,
  `/ui/*`, `/healthz` are FastAPI routes (`web.py`).
- Lifespan (`app.py:92-104`) enters `mcp_app.lifespan`, runs a 15-min purge loop (`app.py:40, 84-90`).
- HTTP middleware (`app.py:126-165`): per-IP rate limit on `POST /register` (20/60s, since DCR lives inside the mount and has
  no hook), security headers (X-Frame-Options DENY, CSP, no-referrer, nosniff), `Cache-Control: no-store` on `/auth/api`.
- `app.state` replaced with typed `GatewayState` (`state.py:28-38`); read via `get_state(request)`.
- Logging configured in package `__init__` before fastmcp import (`__init__.py:17-31`); fastmcp's handler stripped (`app.py:33-35`).
- `cli.py`: `run | check | hash-password | migrate | rotate-key`; uvicorn with `proxy_headers=True`,
  `forwarded_allow_ips=config.server.trusted_proxy_ips` (`cli.py:~175-176`).

### 1.2 Aggregation (`gw/gateway.py`, 136 lines)
- `FastMCP(name="MCP Gateway", auth=provider, mask_error_details=True, icons=…)` (`gateway.py:86-99`).
- One local tool `gateway_status` (`gateway.py:113-125`).
- For each backend: `create_proxy(client, mask_error_details=True)` then `mcp.mount(proxy, namespace=name)` →
  `github_create_issue` style names (`gateway.py:127-133`). Down/unconnected backend only drops its own tools
  (FastMCP `provider_error_strategy="warn"`).
- **No-token-passthrough guard** `_require_no_header_forwarding` (`gateway.py:53-77`): FastMCP proxies force
  `TransportOptions.forward_incoming_headers=True`; `NoForwardStreamableHttpTransport` (`upstream.py:60-87`) overrides it to
  `False` in `connect_session`; startup raises if a client uses another transport or if the fastmcp field disappears.

### 1.3 Client-facing OAuth AS (`gw/oauth_server.py`, 438 lines)
Subclasses `fastmcp.server.auth.auth.OAuthProvider` (which itself wraps SDK `sdk/server/auth/*` handlers).
- Ctor (`oauth_server.py:82-102`): `base_url = issuer_url = public_url`; DCR enabled with any scope; revocation on;
  `CIMDClientManager(enable_cimd=True, allowed_redirect_uri_patterns=…)` from `fm/server/auth/cimd.py`.
- Clients: `GatewayClient(ProxyDCRClient)` with lenient `validate_scope` (single-identity AS; scopes informational)
  (`:61-76`). `get_client` loads from SQLite, refreshes CIMD docs older than 1h, fetches unknown URL-shaped client_ids via
  CIMD (`:109-139`). `register_client` stores DCR registration (`:141-149`).
- **Authorize = park + redirect** (`:153-173`): saves a transaction (client, redirect_uri, state, PKCE challenge, scopes,
  RFC 8707 `resource`) with 600s TTL, returns `/ui/authorize?txn=…`. Svelte `Authorize.svelte` drives login
  (`POST /auth/api/login`, bcrypt, signed cookie) and consent (`POST /auth/api/consent`) → `complete_authorization`
  (`:194-232`) issues a 32-byte code (SHA-256 stored, 300s) and builds the redirect.
- Tokens (`:254-345`): opaque `token_urlsafe(43)` access (default 3600s) + refresh (30d), both stored as SHA-256 hashes;
  code is single-use (`:293-296`); refresh **rotation** revokes the old refresh token (`:337`); `mark_client_used` exempts a
  client from the 24h unused-DCR purge.
- `load_access_token` (`:347-359`) is what FastMCP's bearer middleware calls on every MCP request.
- Route patching `get_routes` (`:368-438`): `/token` gets `PrivateKeyJWTClientAuthenticator` (CIMD clients with
  `private_key_jwt`); AS metadata sets `client_id_metadata_document_supported=true`, adds `private_key_jwt`/`none`;
  `/.well-known/openid-configuration` alias. PRM (RFC 9728) + `WWW-Authenticate: Bearer resource_metadata=…` come from FastMCP.
- **Gap:** no RFC 9207 `iss` in the authorization response (`construct_redirect_uri(..., code, state)` at `:232`) and
  `authorization_response_iss_parameter_supported` is not set (field exists: `sdk/shared/auth.py:230`). 2026-07-28 makes
  clients validate `iss`; ChatGPT uses its stable callback `https://chatgpt.com/connector_platform_oauth_redirect` only when
  the AS supports RFC 9207, else per-connector `https://chatgpt.com/connector/oauth/{id}`. Small MODIFY (§2).

### 1.4 Upstream OAuth client (`gw/upstream.py`, 688 lines)
Backend auth types (`config.py:118-145`): `none | bearer | headers | oauth` (+ optional static `client_id/secret`, `scopes`,
`prefer_dcr`).
- `build_client` (`upstream.py:310-330`): lower-cased static headers; for oauth a cached per-backend
  `JsonTokenOAuthClientProvider`; always `NoForwardStreamableHttpTransport`; `Client(transport)` (default `mode="auto"` →
  negotiates 2026-07-28 or falls back to handshake era).
- `_build_oauth_provider` (`:332-391`): redirect `{public_url}/oauth/callback`; registration precedence
  **static client → CIMD (if https public_url and not prefer_dcr; client_id = `{public_url}/oauth/client-metadata.json`) →
  DCR fallback** (SDK automatic). The CIMD doc is served by `web.py:171-178` (`client_name: "MCP Gateway"`, `:393-404`).
- `JsonTokenOAuthClientProvider` (`:109-160`): forces `Accept: application/json` on token/refresh (GitHub); `_initialize`
  restores `token_expiry_time` after restart so the SDK **refreshes proactively** instead of hitting 401 → interactive flow.
- `DbTokenStorage` (`:163-207`): SDK `TokenStorage` over `upstream_data(backend,key)`; persists absolute `expires_at`,
  decays `expires_in` on read; static client info never overwritten.
- **Connect flow** (interactive, once per backend): UI `GET /oauth/connect/{name}` (session required, `web.py:180-203`) →
  `start_connect` (`:436-503`) cancels stale flow, creates `ConnectFlow` futures (`:277-294`), runs
  `_drive_interactive_reauth` in a task (`:269-274`) using `_ForcedChallengeAuth` (`:210-266`, synthesizes a 401 so discovery →
  registration → authorize always runs even for servers that don't 401 `initialize`). `redirect_handler` (`:408-425`) captures
  the authorize URL + `state` → returned to browser (60s timeout). Upstream AS redirects to `/oauth/callback` (`web.py:205-230`,
  requires admin session) → `deliver_callback` (`:505-533`, **exact state match only**, carries RFC 9207 `iss`) → SDK exchanges
  code (PKCE) → `probe_backend` (`:90-102`, no `ping` on modern era) → `wait_connect_result` (`:535-550`, 300s).
- MCP traffic **never** starts an interactive flow: `redirect_handler` raises `NotConnectedError` outside a connect session.
- Refresh is fully automatic inside the SDK `OAuthClientProvider`.
- `disconnect` (`:552-560`), `backend_status` (`:564-589`), streaming `test_connection` ping → auth → list_tools (`:616-688`).
- Flows are in-memory (single process by design).

### 1.5 Storage + encryption (`gw/storage.py`, 518 lines)
- One `sqlite3` connection, `check_same_thread=False`, guarded by `threading.RLock`; **sync calls from async handlers**;
  **no WAL / busy_timeout pragmas**. Migrations applied in `__init__` (`storage.py:76-83`).
- **Envelope encryption** (`:85-137`): operator `encryption_key` (Fernet key, or passphrase → scrypt n=2^17 with per-DB salt in
  `meta.kdf_salt`) = KEK; random Fernet DEK wrapped in `meta.dek_wrapped`. Wrong key → `EncryptionKeyError` at startup.
  `rotate_key` re-wraps only the DEK (`:197-211`). Legacy-row migration (`:141-195`).
- Encrypted: `oauth_clients.data`, `upstream_data.value`, secrets in `meta` (e.g. session secret, `:233-245`).
  Hashed (SHA-256): access/refresh tokens, auth codes. Plain JSON: txns, code payloads (no secrets).
- Key file support: `MCP_GATEWAY_ENCRYPTION_KEY_FILE` read in-process, not via env (`config.py:180-203`).

### 1.6 DB schema (Alembic, hand-written DDL, no ORM)
`migrations/versions/0001_initial_schema.py` + `0002_session_revocation_and_client_ttl.py`; runner `gw/db_migrations.py`
(wraps the caller's sqlite3 connection in a SQLAlchemy `StaticPool` engine so `:memory:` works).

| table | columns | notes |
|---|---|---|
| `meta` | key PK, value | kdf_salt, dek_wrapped, encrypted secrets |
| `oauth_clients` | client_id PK, data BLOB(enc), is_cimd, created_at, last_used_at (0002) | DCR/CIMD clients |
| `auth_codes` | code_hash PK, data JSON, expires_at | |
| `access_tokens` | token_hash PK, client_id, scopes, subject, resource, expires_at | |
| `refresh_tokens` | token_hash PK, client_id, scopes, subject, resource, expires_at, revoked | |
| `auth_txns` | txn_id PK, data JSON, expires_at | parked authorize requests |
| `upstream_data` | (backend, key) PK, value BLOB(enc), updated_at | key ∈ {`tokens`,`client_info`} |
| `revoked_sessions` | session_id PK, expires_at | logout (0002) |

### 1.7 Other modules
- `users.py`: constant-cost bcrypt `verify_user` (timing-safe across unknown user/plaintext paths), `SessionManager`
  (itsdangerous signed cookie `mcp_gateway_session`, per-login sid, revocation table).
- `web.py`: `/auth/api/{me,txn,login,logout,consent,backends,backends/{n}/disconnect,backends/{n}/test-connection}`,
  `/oauth/{client-metadata.json,connect/{n},callback}`, `/ui/*` SPA serving with correct 404 for missing hashed assets,
  `/healthz`. Test-connection streams **`application/x-ndjson`** (`web.py:163`) — see §7 (Cloudflare buffers it).
- `ratelimit.py`: 43-line in-memory sliding window keyed by IP (login 10/60s, register 20/60s).
- `config.py`: pydantic models, `${VAR}`/`${VAR:-default}` expansion, backends only from YAML.
- `ui/`: Svelte 5 + Vite → `gw/static/ui/`. Pages `Authorize.svelte` (login + consent), `Backends.svelte`
  (connect/disconnect/test), components `Login`, `Banner`, `Logo`, `ConnectionTestModal`, `lib/api.js`.
- `Dockerfile`: node:22-alpine builds UI → python:3.12-slim, non-root uid 10001, `/data` volume, `mcp-gateway run`.

---

## 2. Module map → our repo

Target modules: `server/`, `auth/`, `providers/`, `router/`, `accounts/`, `health/`, `storage/`, `admin/` (API).

| mcp-gateway | Verdict | Target | Change |
|---|---|---|---|
| `__init__.py` (logging bootstrap) | KEEP | `engine/__init__.py` | rename env var; keep "configure before fastmcp import" |
| `app.py` | MODIFY | `server/app.py` | keep FastAPI-outer + FastMCP catch-all mount, security middleware, /register limiter, purge loop; replace backend-client build with `ProviderRegistry`/`AccountPool` startup; add engine lifespan (job workers, cache purge) |
| `gateway.py` | MODIFY (heavy) | `server/mcp_server.py` + `providers/mcp_upstream/guard.py` | delete proxy/mount loop; register capability tools only; move `_require_no_header_forwarding` into upstream client factory as startup assert; `gateway_status` → admin API (optional `engine_status` tool) |
| `oauth_server.py` | KEEP (+small) | `auth/oauth_server.py` | add RFC 9207: `iss` param in `complete_authorization` redirect + `authorization_response_iss_parameter_supported=true` in metadata patch; keep CIMD/DCR/PKCE/rotation as is |
| `users.py` | KEEP | `auth/users.py` | single user fits |
| `ratelimit.py` | KEEP | `auth/ratelimit.py` | anon-endpoint limiter only; upstream quota limiting is `router/` |
| `upstream.py` | MODIFY (split) | `providers/mcp_upstream/{transport,oauth,token_store}.py`, `accounts/connect_flow.py`, `health/probes.py` | key everything by `account_id` instead of backend name (§4); keep `NoForwardStreamableHttpTransport`, `JsonTokenOAuthClientProvider`, `_ForcedChallengeAuth`, `DbTokenStorage`, exact-state callback routing; fix per-account lock serialization (§8 R1); CIMD `client_name` → our name |
| `storage.py` | MODIFY | `storage/db.py`, `storage/crypto.py`, `storage/auth_repo.py`, `storage/accounts_repo.py` | keep envelope encryption, hashing, rotate-key verbatim; add WAL + `busy_timeout`; offload writes via `anyio.to_thread` (or aiosqlite) because cache/jobs/request-log volume ≫ gateway's; new repos for providers/accounts/cache/jobs/handles/request_log behind interfaces |
| `db_migrations.py` + `migrations/` | KEEP | `storage/migrations/` | keep 0001/0002 (and the existence-guard pattern), add 0003+ |
| `config.py` | MODIFY | `config.py` | keep env expansion, key-file; `backends:` → `providers:` seed (DB is source of truth once admin UI edits); add engine sections (routing, fusion, reranker, cache TTLs, jobs) |
| `state.py` | KEEP | `server/state.py` | extend typed state |
| `web.py` | MODIFY | `auth/web.py` (login/consent/txn), `accounts/web.py` (`/oauth/connect/{account_id}`, `/oauth/callback`, CIMD doc), `admin/api.py` | backends endpoints → `/admin/api/providers|accounts|health|requests|policies`; NDJSON → `text/event-stream`; serve OmniRoute-derived SPA at `/admin/`, keep Svelte at `/ui/authorize` |
| `cli.py` | KEEP/MODIFY | `cli.py` | keep run/check/hash-password/migrate/rotate-key; add `accounts list`, `jobs` |
| `ui/` Authorize, Login, Banner, Logo, api.js | KEEP | `ui-auth/` | only OAuth login/consent pages (decision in requirements) |
| `ui/` Backends, ConnectionTestModal | DROP (later) | — | replaced by admin SPA; keep until SPA exists |
| `Dockerfile`, compose | MODIFY | root | two UI builds; add `cloudflared` service; data volume |
| tests: oauth_flow, cimd_client, security_hardening, key_management, db_migrations, upstream_oauth, upstream_connect_flow, upstream_token_refresh, protocol_eras, config_storage | KEEP (adapt names) | `tests/` | the e2e two-server harness in `conftest.py` is very valuable |
| tests: aggregation, connection_test, ui_static | MODIFY | `tests/` | aggregation → "upstream tools never listed/callable"; add account-pool tests |

---

## 3. From "aggregate + namespace passthrough" to "internal upstream providers, capability tools only"

Principle: **don't mount upstreams at all.** Upstream MCP servers become ordinary provider backends called from Python.
Hidden-by-construction beats hidden-by-filter (a `Visibility`/`ToolTransform` over mounted proxies would still run upstream
`tools/list` per listing — `ProxyProvider` caches 300s — and one misconfigured transform leaks tools).

Hook points:
1. `gateway.py:127-133` — delete `create_proxy` + `mcp.mount`. `build_gateway` → `build_mcp_server(engine)` registering only
   capability tools (`search`, `fetch`, `search_scholarly`, `verify_claim`, `get_evidence`, `start_job/get_job`, …).
2. `app.py:72-79` — replace `clients = {name: manager.build_client(...)}` with `AccountPool.load()` that builds one
   FastMCP `Client` per **account** (via the kept `build_client` logic) and hands them to `providers/mcp_upstream.Adapter`.
3. New `providers/mcp_upstream/adapter.py` implements the research-mcp provider interface:
   `await client.call_tool_mcp(name, args, timeout=…, progress_handler=…, meta=…)` (`fm/client/mixins/tools.py:146-171`,
   returns `CallToolResult` incl. `structured_content`, never raises on tool error) → normalize to evidence items.
   Per-provider mapping table: capability → upstream tool name + arg translation + result parser.
4. Keep clients open for the app lifetime (`Client` is reentrant via `nesting_counter`, `fm/client/client.py:227,275`);
   on the modern era each call is an independent POST so there is no session to lose.
5. Keep `_require_no_header_forwarding` as a startup assertion in the client factory (defense in depth; plain `Client` calls
   don't forward inbound headers, but the invariant must not depend on that).
6. `upstream.test_connection`/`probe_backend` → `health/probes.py`; also snapshot upstream `list_tools()` schemas per account
   and store a hash → schema-drift alert in health dashboard (upstream tool renames break our mapping silently otherwise).
7. `FastMCP(instructions=…)` → describe capabilities, not backends. Keep `mask_error_details=True`; map provider failures to
   our own structured error/coverage fields instead of leaking upstream errors.
8. Do **not** import `fastmcp_tasks` in the process that calls upstreams unless intended: its client extension is
   auto-registered on every `Client` (`fastmcp_tasks/__init__.py:17`) and makes `call_tool` transparently poll upstream tasks
   to completion (blocking). Use `call_tool_task` explicitly if/when an upstream offers tasks.

---

## 4. Account pool fit (N OAuth connections to the same upstream)

Today: **one credential set per backend**. Everything is keyed by backend name: `upstream_data(backend,key)` PK,
`DbTokenStorage(storage, backend)`, `BackendManager._oauth_providers[name]`, `_flows_by_backend[name]`, `/oauth/connect/{name}`,
`disconnect(name)`, `backend_status`. Things that already work for N:
- Callback routing is by OAuth `state` (`upstream.py:526`), single `/oauth/callback` → fine for N accounts.
- CIMD client_id is one URL for the whole install; the upstream AS just sees multiple user grants for the same client → fine.
  Static client credentials are per provider → fine. DCR `client_info` can be per account (simplest) or shared per provider
  (2026-07-28: credentials bound to issuer — sharing within one provider/issuer is legal).

Schema change (migration 0003):
```sql
CREATE TABLE providers (id TEXT PRIMARY KEY, kind TEXT NOT NULL,          -- 'mcp' | 'rest'
  url TEXT, auth_type TEXT NOT NULL, config JSON NOT NULL,                 -- scopes, prefer_dcr, mapping version…
  static_client BLOB,                                                      -- enc {client_id, client_secret}
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL, updated_at REAL);
CREATE TABLE accounts (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers(id),
  label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER DEFAULT 100, weight REAL DEFAULT 1,
  plan TEXT, tags JSON, status TEXT,                                        -- ok|rate_limited|exhausted|plan_blocked|auth_error|disconnected
  cooldown_until REAL, last_error TEXT, last_ok_at REAL, created_at REAL, updated_at REAL);
CREATE TABLE account_credentials (account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  key TEXT NOT NULL,                                                       -- tokens | client_info | api_key | headers
  value BLOB NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (account_id, key));
-- data migration: each upstream_data backend → provider + account '<backend>:default'; copy rows; keep upstream_data until 0004.
```
Code changes: every `name`/`backend` key in `upstream.py` → `account_id`; `ConnectFlow.backend` → `account_id`;
routes `/oauth/connect/{account_id}`; `backend_status` → per account; API-key providers (Exa, Firecrawl, Tavily…) store keys in
`account_credentials` (encrypted) instead of YAML env. Quota/usage/cooldown columns (or separate `account_usage` table) feed the
OmniRoute-style router.
Operational caveat: connecting a 2nd account of the same upstream requires logging into a *different* upstream identity in the
browser (private window / upstream logout); UI should warn. Also detect "same upstream subject connected twice" if the upstream
exposes identity (e.g. Undermind `get_orientation` says which account is connected).

---

## 5. FastMCP 4 vs raw python-sdk v2

Versions (PyPI, 2026-09-29): `fastmcp 4.0.10` (2026-09-25; 4.0.0 GA 2026-08-31; 8 patch releases in ~4 weeks, incl. 4.0.6/4.0.7
regressions fixed in 4.0.8), `fastmcp-slim` same version, `fastmcp-tasks 4.0.10` (needs `pydocket`); `mcp 2.2.0`
(2026-09-07; v1 line still maintained: 1.30.0), `mcp-types 2.2.0`. FastMCP 4 requires `mcp>=2,<3`, uses `httpx2`.
FastMCP docs explicitly recommend exact pins (`fastmcp==4.0.0 # Good`, https://gofastmcp.com/development/releases).

| Need | FastMCP 4.0.10 | raw `mcp` 2.2.0 |
|---|---|---|
| Tools w/ structured output | `@mcp.tool` infers output schema from return type; `ToolResult(structured_content=…)` (`fm/tools/base.py:101-124`) | `MCPServer` (`sdk/server/mcpserver/server.py`) also does structured output; lower-level |
| Streamable HTTP | `http_app(path, json_response, stateless_http, event_store, retry_interval)` (`fm/server/mixins/transport.py:372-400`) | `StreamableHTTPSessionManager` (`sdk/server/streamable_http_manager.py`) — FastMCP uses it |
| Both eras (2026-07-28 sessionless + 2025-11-25 handshake) | yes, per-connection negotiation server + client (`Client(mode="auto"/"legacy")`); proven by `tests/test_protocol_eras.py` | yes: modern single-exchange handler `sdk/server/_streamable_http_modern.py`, legacy untouched |
| OAuth AS (DCR, PKCE, RFC 8414/9728/8707) | `OAuthProvider` base + SDK handlers | SDK has handlers/routes/provider protocol (`sdk/server/auth/*`) |
| **CIMD server side + private_key_jwt** | **yes** (`fm/server/auth/cimd.py`, `PrivateKeyJWTClientAuthenticator`) | **no** (SDK register handler even rejects `private_key_jwt`, `sdk/server/auth/handlers/register.py:57`) |
| OAuth client to upstream | SDK `OAuthClientProvider` (+ `fm/client/auth/oauth.py` wrapper w/ key-value token store) | `OAuthClientProvider`, CIMD `client_metadata_url`, client_credentials & identity-assertion extensions (`sdk/client/auth/extensions/`) |
| Proxy / mount | `create_proxy`, `ProxyProvider` (era mirroring, 300s list cache), `mount(namespace=)`, `ClientGroup` (multi-server client, 4.0.1 reentrant) | `ClientSessionGroup` only |
| Middleware | rich: Logging/StructuredLogging, Timing, ErrorHandling/Retry, RateLimiting (token bucket / sliding window), ResponseCaching (TTL, pluggable `AsyncKeyValue` store), ResponseLimiting, AuthMiddleware (`fm/server/middleware/*`) | low-level `ServerMiddleware` protocol only (`sdk/server/context.py:146`), no batteries |
| Tool transform / hiding | `ToolTransform`, `Tool.from_tool`, `Namespace`, `Visibility`, `enable/disable` (`fm/server/transforms/*`, `providers/base.py:604-653`) | none |
| Background tasks | `fastmcp-tasks`: SEP-2663 `io.modelcontextprotocol/tasks` extension, `@mcp.tool(task=True)`, Docket backend `memory://` (not durable) or `redis://`; **modern era only** | extension framework (`sdk/server/extension.py`) + types; no task engine |
| Progress / logging notifications | `ctx.report_progress` (foreground → `notifications/progress`; in task → Docket progress) (`fm/server/context.py:453`) | `ctx.report_progress` (`sdk/server/mcpserver/context.py:113`) |
| Result cache hints (SEP-2549) | `FastMCP(cache_ttl=…)` (`fm/server/caching.py`) | `cache_hints=` |
| Testing | in-memory `Client(mcp)` | in-memory transport |

**Recommendation: FastMCP 4** (`fastmcp==4.0.10` exact pin, `mcp>=2.2,<3`, bump deliberately with the e2e suite).
Reasons: mcp-gateway's AS depends on FastMCP-only pieces (`OAuthProvider`, `CIMDClientManager`, `PrivateKeyJWTClientAuthenticator`,
`TokenHandler`, `ProxyDCRClient`) — raw SDK means re-implementing CIMD + private_key_jwt (the preferred registration in
2026-07-28; DCR now deprecated). Middleware (logging/timing/rate-limit/error) and era negotiation come free. Use FastMCP as the
**front server + upstream client**; do not use its proxy/mount/transform features (§3). Implement query/document cache, upstream
quotas, and jobs in our own `router/`/`storage/` layers — FastMCP's `ResponseCachingMiddleware` caches whole MCP responses
per method/args, not normalized evidence, so it is at most an optional outer layer.
Pinning notes: mcp-gateway pins `fastmcp>=4.0.5,<5` (`pyproject.toml:9`) — tighten to `==`. It relies on semi-private APIs:
`fastmcp.server.auth.oauth_proxy.models.ProxyDCRClient`, `TransportOptions.forward_incoming_headers`, SDK
`OAuthClientProvider._initialize/_refresh_token/_exchange_token_authorization_code/context` — keep the guard tests
(`tests/test_security_hardening.py:370-400`, `tests/test_upstream_token_refresh.py`). Never mix `httpx` and `httpx2`
auth/exception types (CLAUDE.md invariant).

---

## 6. Async job options

Spec facts: 2026-07-28 moved tasks out of core into the `io.modelcontextprotocol/tasks` extension (SEP-2663): `tasks/get`,
`tasks/update`, `tasks/cancel`; `tasks/list` and `tasks/result` gone; server-initiated sampling/elicitation replaced by
MRTR (`resultType: "input_required"`). Handshake-era 2025-11-25 had experimental core tasks (types in
`mcp_types/_v2025_11_25`), which neither SDK nor FastMCP serve. (https://blog.modelcontextprotocol.io/posts/2026-07-28/,
https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx)

| Option | Pros | Cons |
|---|---|---|
| A. MCP tasks ext via `fastmcp-tasks` | standard, client auto-polls, progress via `tasks/get` | modern-era only; host support in ChatGPT/Claude.ai unverified; `memory://` Docket loses tasks on restart (violates "persisted across restarts"); durable needs Redis/Valkey; FastMCP context snapshot machinery is complex |
| B. Progress notifications on a normal call | both eras; also keeps SSE alive; zero state | lost on disconnect; hosts may not render; still bounded by host tool-call timeout |
| C. Poll tools: `start_job` → `job_id`; `get_job(job_id, wait_s≤~25)` | works on every host and era; persisted in our SQLite `jobs` table; resumable workers; maps naturally onto upstream poll tools (Undermind `launch_deep_search`/`inspect_deep_searches`, Elicit reports/`get_report`) | non-standard; the model must remember to poll |

Recommendation: **C as the contract** (SQLite `jobs` table, asyncio worker, resume on startup), **B** for 10–30s synchronous
calls (emit progress while fanning out — also acts as SSE keepalive), and optionally expose the same job store through a custom
`ServerExtension` implementing SEP-2663 later (FastMCP `add_extension`, `fm/server/extensions.py`) rather than adopting Docket.

---

## 7. Cloudflare Tunnel concerns

Facts (Cloudflare docs, fetched 2026-09-29, https://developers.cloudflare.com/fundamentals/reference/connection-limits/):
Proxy **Read** Timeout **125 s** (older docs/blogs say 100 s; configurable **Enterprise only**; → 524), Proxy Write Timeout 30 s,
Proxy Idle 900 s, client keep-alive/HTTP2 idle 400 s, request headers 128 KB, URL 16 KB. The read timeout is time **between bytes**
from origin; an SSE stream with periodic bytes survives. cloudflared **buffers responses unless `Content-Type: text/event-stream`**
(https://developers.cloudflare.com/tunnel/troubleshooting/); quick tunnels (`trycloudflare.com`) buffer SSE even then — use a
**named tunnel**.

Implications:
- **Never set `json_response=True`.** SDK modern SSE path commits `text/event-stream` after the first notification or 15 s and
  sends comment keepalives every 15 s (`sdk/server/_streamable_http_modern.py:1-20,146-147`); legacy path uses sse-starlette
  `EventSourceResponse` (default 15 s ping). JSON mode would send nothing until completion → 524 at 125 s.
- Keep synchronous calls well under host tool timeouts (target ≤ 45–60 s); anything longer → job (§6).
- Admin streaming: `web.py:163` uses `application/x-ndjson` → will be buffered by cloudflared; switch to `text/event-stream`.
- `public_url` = the tunnel hostname (https). All derived URLs (issuer, PRM, `/mcp`, `/oauth/callback`, CIMD client_id) must match
  exactly — `ServerConfig._normalize_public_url` strips trailing `/` (`config.py:78-81`). CIMD requires https (`upstream.py:373`).
- `trusted_proxy_ips`: cloudflared on host → `127.0.0.1` (default); cloudflared container → its IP/CIDR, else rate limits key on
  the tunnel IP and cookies `secure` detection may break. cloudflared sets `X-Forwarded-For`/`Cf-Connecting-Ip`.
- Client redirect URIs to allow (`allowed_client_redirect_uris`): `https://claude.ai/api/mcp/auth_callback`,
  `https://claude.com/api/mcp/auth_callback`, `https://chatgpt.com/connector_platform_oauth_redirect`,
  `https://chatgpt.com/connector/oauth/*`, loopback `http://localhost:*/callback`, `http://127.0.0.1:*/callback` (Claude Code).
  Add RFC 9207 `iss` (§1.3) so ChatGPT uses the stable callback.
- Upstream OAuth callback is browser-driven (admin's browser → `/oauth/callback`), but `/oauth/client-metadata.json` must be
  fetchable **server-to-server** by Scite/Elicit/Undermind/Consensus authorization servers.
- **Cloudflare Access / WAF / Bot Fight Mode / Browser Integrity Check** must not challenge `/mcp`, `/.well-known/*`,
  `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/client-metadata.json` (server-to-server from OpenAI/Anthropic/upstream
  ASes cannot solve JS challenges). Access may protect `/admin/*` (and `/ui/backends`).
- Don't let Cloudflare cache `/.well-known/*` or `/auth/api/*` (defaults don't cache JSON; gateway sets `no-store` on `/auth/api`).

---

## 8. Risks

- **R1 — per-account request serialization (verified in source).** SDK `OAuthClientProvider._auth_flow` holds
  `context.lock` across `response = yield request` (`sdk/client/auth/oauth2.py:606-625`). httpx2 hands the response back to the
  auth flow after headers (`RedirectAwareAuth` doesn't pre-read bodies, `sdk/shared/_httpx_utils.py:202-217`), so the lock is
  held until response headers arrive — on the modern era that is at completion for calls < 15 s. ⇒ parallel calls on one OAuth
  account run ~serially. Fix: subclass so the lock covers only token init/refresh/401 re-auth, not the main request; add a
  concurrency test. (Static-header accounts unaffected.)
- R2 — FastMCP churn + semi-private API reliance (see §5); exact pins + e2e suite on every bump.
- R3 — Sync SQLite on the event loop, single connection, no WAL: fine for auth, not for cache/jobs/request-log. Add WAL,
  busy_timeout, thread offload; keep single-process invariant explicit.
- R4 — In-memory connect flows/rate limits: restart mid-connect loses the flow (acceptable; retry).
- R5 — Host behaviour unknowns: task-extension support, progress rendering, tool-call timeouts in ChatGPT/Claude.ai.
- R6 — Upstream ToS for programmatic/multi-account use of Scite/Elicit/Undermind/Consensus MCP (separate research task).
- R7 — Upstream schema drift (hidden tools; no user sees breakage) → schema-hash health check (§3.6).
- R8 — DCR deprecated in 2026-07-28; some upstream ASes may drop it; CIMD needs our public https URL reachable 24/7
  (home machine downtime = upstream AS cannot refetch our client metadata; refresh tokens still work).
- R9 — Lenient scopes (`GatewayClient.validate_scope`) OK for single user; revisit if scopes ever gate capabilities.
- R10 — No LICENSE file in mcp-gateway; rely on written author permission + pyproject MIT; keep attribution.
- R11 — Refresh-token rotation by an upstream + concurrent refreshes (esp. if R1 fix loosens the lock) can invalidate tokens;
  keep a per-account refresh mutex.
