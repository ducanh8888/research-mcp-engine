# Research Engine — Design (v2)

Status: **draft v2**, 2026-09-30. Replaces v1 (2026-09-29, see git history).
Inputs: [requirements-notes.md](requirements-notes.md), [research.md](research.md), `research/01…07`.
Audience: the owner and whoever implements the engine.

**Why v2.** v1 imported most of the control plane of an LLM model router (OmniRoute/9router): policy language,
spending strategy, 3-scope state machine with severity ordering, breaker profiles, health scheduler, replay
experimentation, durable distributed-style jobs. Model calls are largely substitutable; research providers are not
(Scite, Firecrawl, Elicit, Undermind do different things). v2 keeps the provider abstraction and routing core and
cuts the control plane to what the stated workload needs. Everything cut is listed in §16 with the evidence that
would justify adding it back.

**Reuse principle (clarified).** Maximize reuse of *code that fits the minimal design* (adapters, HTTP/SSRF/PDF
layers, OAuth, storage crypto, UI components). Do not import abstractions because a source repo has them.

---

## 0. Summary

One self-hosted MCP endpoint that routes capability-typed requests to providers and accounts, fails over on
errors, merges results and returns compact ranked evidence with provenance. It never interprets evidence.

| # | Decision | § |
|---|---|---|
| D1 | Python 3.12, **FastMCP `==4.0.10`** (mcp `>=2.2,<3`), FastAPI outer app, single process, asyncio | 3 |
| D2 | App shell = fork of `R0Wi/mcp-gateway`; provider layer seeded from `vvzvlad/research-mcp` | 2 |
| D3 | **One MCP tool per capability (16)** + `get_job` on `/mcp`; ChatGPT `search`/`fetch` only on `/mcp/compat` | 4 |
| D4 | Routing = per capability: `fanout` or `sequential` over an ordered provider list; accounts tried in priority order | 6 |
| D5 | Account availability = 3 independent fields (credential, cooldown, blocked capabilities); no global severity | 7 |
| D6 | Errors classified **inside each adapter** into one small typed error set | 7.3 |
| D7 | Merge = normalize → canonical ID/URL → exact-key dedup → **plain RRF (k=60)**; optional rerank replaces order of top-N | 8 |
| D8 | Deadline per call → **partial result**, not a job. Jobs only for inherently async capabilities | 9 |
| D9 | SQLite (WAL), plain repositories; query + document cache with TTL | 10, 11 |
| D10 | Config: bootstrap YAML for server/auth only; providers/accounts/routing live in DB, edited in admin | 12 |
| D11 | Admin: accounts + OAuth connect, status/quota, request log + replay, routing editor — own API vocabulary | 13 |
| D12 | Existing host, existing Cloudflare tunnel + Caddy, hostname `research.ducanh.cloud` | 15 |
| D13 | No engine-enforced ToS restrictions; ToS is the owner's call (requirements, 2026-09-29) | 14 |

Non-goals: hypothesis generation, research prioritization, knowledge admission, campaign opening, scientific
decisions, final source acceptance, **evidence interpretation or preference ranking**. No LLM in the engine.

---

## 1. Architecture

```
ChatGPT · claude.ai · Claude Code · agentRT
        │ HTTPS  research.ducanh.cloud  (existing cloudflared → Caddy)
        ▼
research-engine (one process)
 ├─ /mcp  /mcp/compat         FastMCP 4, both protocol eras, OAuth 2.1 AS (mcp-gateway) or personal token
 ├─ /oauth/*                  upstream OAuth connect + CIMD client document
 ├─ /admin/api/* + /admin/    admin API + SPA
 ├─ tools ─► router: routing entry → accounts → execute (fanout | sequential) with deadline
 │            providers: http | mcp_oauth | local adapters (each classifies its own errors)
 │            merge: normalize → canonicalize → dedup → RRF → [rerank]
 │            jobs: async capabilities only
 │            cache: query, document
 └─ storage: SQLite (WAL) + blobs/, secrets Fernet-encrypted
        │
 REST: Exa · Firecrawl · Tavily · Brave/Serper · Jina Reader · GitHub · OpenAlex · Crossref · S2 · arXiv · Scite REST
 MCP+OAuth: Undermind · Scite · Elicit · Consensus (REST key preferred)
```

---

## 2. Repository layout and provenance

```
research-engine/
├─ pyproject.toml  config.example.yaml  docker-compose.yml  THIRD_PARTY_NOTICES.md
├─ src/research_engine/
│  ├─ server/        app.py, mcp_server.py, tools.py, schemas.py, instructions.md
│  ├─ auth/          oauth_server.py, users.py, ratelimit.py, web.py, tokens.py (personal tokens)
│  ├─ providers/     base.py, registry.py, errors.py, http.py, url_guard.py, pdf.py,
│  │                 web/*.py, scholar/*.py, dev/*.py, mcp/{client,oauth,token_store}.py + per-provider modules
│  ├─ router/        routing.py, execute.py, accounts.py
│  ├─ merge/         normalize.py, canonical.py, dedup.py, fusion.py, rerank.py
│  ├─ jobs/          runner.py
│  ├─ cache.py
│  ├─ storage/       db.py, crypto.py, blobs.py, repos.py, migrations/
│  ├─ admin/         api.py
│  ├─ config.py  cli.py
├─ ui-auth/          Svelte login/consent (mcp-gateway)
├─ ui-admin/         React SPA built from 9router components
└─ tests/
```

Module names follow the agreed repo boundary where they carry real code; `normalization/canonicalization/dedup/
fusion/reranking` are one `merge/` package until any of them grows enough to split. `health/` is not a package:
availability is three fields on the account (§7).

| Our module | Seeded from | Mode |
|---|---|---|
| `server/app.py`, `auth/*`, `storage/crypto.py`, migrations 0001–0002, `config.py`, `cli.py`, `ui-auth/` | mcp-gateway @59c1efd | copy + modify |
| `providers/mcp/*` | mcp-gateway `upstream.py` | copy, key by account, fix OAuth lock (§5.4) |
| `providers/http.py`, `url_guard.py`, `pdf.py`, web adapters, read cascade | research-mcp @11f297d | copy + modify |
| `ui-admin/` components (tables, modals, OAuth modal, cooldown timer, log detail) | 9router @f01fb90 (+ a few OmniRoute components) | copy, rebind to our API |
| query-operator parsing | mcp-omnisearch | port (only if P1 needs it) |

---

## 3. Runtime

- FastMCP 4.0.10 pinned exactly; bump only with the e2e suite (inherited from mcp-gateway) green.
- One uvicorn worker. Provider I/O async; CPU work (PDF parse) in `anyio.to_thread`.
- FastMCP ASGI mounted catch-all inside FastAPI (as mcp-gateway). `json_response` off: SSE keepalive every 15 s keeps
  Cloudflare from 524ing.
- Not used: FastMCP proxy/mount/transforms, `fastmcp-tasks`.

---

## 4. MCP surface

### 4.1 `/mcp` — native tools

All tools: `title`, `readOnlyHint: true`, `openWorldHint: true`, `outputSchema` + `structuredContent` + one compact text
block; descriptions state when to use the tool and its limits (first ~2,000 chars).

| Tool | Capability | Mode | Input |
|---|---|---|---|
| `web_search` | WEB_SEARCH | fanout | `query`, `limit`, `domains?`, `date_from/to?` |
| `news_search` | NEWS_SEARCH | fanout | `query`, `limit`, `recency?` |
| `web_read` | WEB_READ | sequential | `target` (URL or handle), `fresh?`, `cursor?` |
| `site_map` | SITE_MAP | sequential | `url`, `limit?` |
| `site_crawl` | SITE_CRAWL | job | `url`, `limit?`, `max_depth?`, `include/exclude_paths?` |
| `paper_search` | PAPER_SEARCH | fanout | `query`, `limit`, `year_from/to?`, `filters?` (passed to providers that support them) |
| `paper_read` | PAPER_READ | sequential | `target` (handle/DOI/arXiv/PMID/URL), `question?`, `cursor?` |
| `paper_metadata` | PAPER_METADATA | sequential | `ids[]` or `citation` string |
| `paper_related` | PAPER_RELATED | fanout | `seeds[]`, `mode` (`similar|citing|cited`) |
| `citation_verify` | CITATION_VERIFY | fanout | `citation`, `claim?` |
| `citation_graph` | CITATION_GRAPH | fanout | `seeds[]`, `direction`, `depth ≤ 2` |
| `editorial_check` | EDITORIAL_CHECK | fanout | `ids[]` |
| `systematic_review` | SYSTEMATIC_REVIEW | job | `question`, `criteria[]`, provider options |
| `deep_literature_search` | DEEP_LITERATURE_SEARCH | job | `goal` |
| `developer_search` | DEVELOPER_SEARCH | fanout | `query`, `repos?`, `language?` |
| `repo_search` | REPO_SEARCH | fanout | `query`, `language?`, `min_stars?`, `mode?` (`repos|code`) |
| `get_job` | — | local | `job_id`, `wait_s ≤ 40` |

17 tools. `SITE_INTERACT` is deferred (§16): browser actions are outside search/retrieve/verify/merge/rank.
There is no separate "expand handle" tool: reads go through `web_read`/`paper_read`, metadata through
`paper_metadata`, all of which accept handles.

The engine does **not** re-classify requests: the consumer chose the capability by choosing the tool.

### 4.2 `/mcp/compat` — ChatGPT/OpenAI deep-research schema

Two tools only, same backend, different representation:
- `search(query)` → runs the routing entry `compat_search` (a normal provider list, default = the `web_search` and
  `paper_search` providers in one fanout) → `{results: [{id: handle, title, url}]}`.
- `fetch(id)` → read by handle (paper handle → `paper_read`, otherwise `web_read`) → `{id, title, text, url, metadata}`.

No classifier, no special routing logic. ChatGPT developer mode uses `/mcp`; OpenAI API deep research and company
knowledge use `/mcp/compat`. Each path has its own protected-resource metadata.

### 4.3 Output

```json
{
  "status": "complete | partial",
  "items": [{ "handle": "doi:10.1038/s41586-020-2286-9", "title": "…", "url": "https://doi.org/…",
              "snippet": "…", "year": 2020, "venue": "Nature", "authors": ["…"],
              "providers": [{"p": "openalex", "rank": 1}, {"p": "s2", "rank": 2}] }],
  "coverage": { "ok": ["openalex", "s2", "exa"], "failed": [{"p": "undermind", "reason": "timeout"}],
                "skipped": [{"p": "scite", "reason": "cooling until 2026-10-01"}] },
  "request_id": "r_…"
}
```

- Fields are what providers returned (merged per §8.4), plus `providers` (who returned it, at which rank). No
  computed quality signals; `len(providers)` is visible to the consumer as-is.
- Default `limit` 8 (max 25); target ≤ ~8k tokens. Read tools page by `cursor` (~20k chars per page).
- `status: partial` when the deadline cut off some providers (§9.1).
- Errors are tool errors: `{error: {code, message, retry_after?}}` with codes `INVALID_INPUT`,
  `NO_PROVIDER_AVAILABLE` (with per-provider reasons), `NOT_FOUND`, `INTERNAL`.

---

## 5. Providers

### 5.1 Interface

```python
class Provider(Protocol):
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    async def execute(self, ctx: CallContext, cap: Capability, req: Request) -> Result: ...
    # async capabilities only:
    async def start(self, ctx, cap, req) -> str: ...          # returns upstream ref
    async def poll(self, ctx, ref: str) -> JobUpdate: ...     # status + partial/complete result
    poll_interval_s: ClassVar[float] = 15
```

`CallContext` = http client, resolved account credential, deadline. `Result` = `hits: list[Hit]` (rank preserved,
upstream fields kept: ids, url, title, snippet, authors, year, venue, dates, raw score) or `document` (text, title,
url, `next_cursor`). Adapters raise `ProviderError` (§7.3).

### 5.2 Transports

- **http**: research-mcp `_http.py` (retry transient errors, never retry 402/429, credit-body markers), extended to
  return status/headers so adapters can classify. Raw HTTP for Firecrawl (SDK drops `creditsUsed`) and Tavily (SDK
  maps 429/432/433 to misleading exceptions); Exa raw HTTP with explicit `contents`.
- **mcp_oauth**: one FastMCP `Client` per account, `NoForwardStreamableHttpTransport`, CIMD → DCR, tokens encrypted
  per account.
- **local**: trafilatura, pypdf.

### 5.3 Provider set v1

| Group | Providers |
|---|---|
| Web/news | Exa, Firecrawl, Tavily, Brave and/or Serper, Jina Reader (keyless), trafilatura |
| Academic | OpenAlex, Crossref, Semantic Scholar, arXiv, Scite public REST; Undermind (MCP), Consensus (REST key), Scite (MCP), Elicit (MCP/REST) |
| Developer | GitHub, Firecrawl developer search, Exa |

Hosted-MCP adapters map each capability to specific upstream tools (config-extendable). Consensus/Undermind return
Markdown → per-adapter parsers with fixture tests. Undermind: one dedicated workspace; `sample_abstract` templated from
the query; cite keys resolved to DOIs with one batched `get_paper_info`.

### 5.4 Required fix

SDK `OAuthClientProvider` holds its lock across the whole upstream request, serializing parallel calls on one OAuth
account. Subclass so the lock covers only token init/refresh/401 re-auth; keep a per-account refresh mutex; test N
parallel calls ≈ max latency.

### 5.5 Upstream tool compatibility

On connect and at startup, the adapter checks that the upstream `tools/list` contains the tools and parameters it maps.
A mismatch is shown in admin as "adapter incompatible" and the adapter's calls fail with a clear error. This is **not**
part of availability state (§7).

---

## 6. Routing

### 6.1 Routing entry

One entry per capability (and `compat_search`), stored in DB, edited in admin:

```yaml
paper_search:  {mode: fanout,     providers: [openalex, s2, undermind, consensus, scite, elicit]}
web_read:      {mode: sequential, providers: [firecrawl, exa, jina, tavily, trafilatura]}
```

- `fanout`: call every available provider in the list in parallel; merge (§8).
- `sequential`: try providers in order; first success wins.
- A provider may appear only if it implements the capability (validated on save).

That is the whole routing language. No nested chains, use-modes, pinning, weights, groups or per-step params.
Provider-specific request defaults (e.g. Firecrawl crawl `limit`, Exa `type`) live in the adapter, overridable per
provider in admin.

### 6.2 Default entries (v1)

| Capability | Mode | Providers |
|---|---|---|
| WEB_SEARCH | fanout | Exa, Brave/Serper, Tavily, Firecrawl search |
| NEWS_SEARCH | fanout | Brave/Serper news, Tavily news, Exa (news) |
| WEB_READ | sequential | Firecrawl, Exa contents, Jina Reader, Tavily extract, trafilatura |
| SITE_MAP | sequential | Firecrawl, Tavily |
| SITE_CRAWL | job | Firecrawl, Tavily |
| PAPER_SEARCH | fanout | OpenAlex, S2, Undermind, Consensus, Scite, Elicit, arXiv |
| PAPER_METADATA | sequential | OpenAlex, Crossref, S2, arXiv |
| PAPER_READ | sequential | arXiv, OpenAlex OA PDF, Scite `read_fulltext`, Undermind `read_pdfs` (when `question`) |
| PAPER_RELATED | fanout | S2, OpenAlex, Undermind |
| CITATION_VERIFY | fanout | Crossref, OpenAlex, Scite (REST tallies + MCP) |
| CITATION_GRAPH | fanout | OpenAlex, S2, Scite |
| EDITORIAL_CHECK | fanout | Crossref, OpenAlex, Scite REST |
| DEEP_LITERATURE_SEARCH | job | Undermind, Elicit |
| SYSTEMATIC_REVIEW | job | Elicit |
| DEVELOPER_SEARCH | fanout | Firecrawl developer, GitHub issues, Exa |
| REPO_SEARCH | fanout | GitHub, Firecrawl developer |

### 6.3 Execution

- Each call has one deadline (`max_wait_s`, default 40, clamp 5–50; hosts cut at ~60 s).
- **fanout**: start all available providers; at the deadline return what has arrived (`status: partial` if any
  provider is still running; stragglers are cancelled). No quorum/grace tuning in v1.
- **sequential**: per-provider timeout = remaining deadline; on error or empty result, next provider. For reads,
  "empty" is decided by the adapter (no text, known block/placeholder page), not by a global character threshold.
- Within a provider, accounts are tried in priority order; an account error that is not about the request (§7.3)
  moves to the next account.
- A provider with no available account is skipped and reported in `coverage.skipped` with the reason.

---

## 7. Accounts and availability

### 7.1 Model

```
provider ── account (credential, priority, enabled, quota_group?)
```

- `quota_group` (optional string): accounts sharing a limit (Exa/Firecrawl keys of one team) cool down together.
- Any number of accounts per provider.

### 7.2 Availability — three independent fields per account

| Field | Values | Set by | Cleared by |
|---|---|---|---|
| `credential` | `ok` · `needs_auth` · `disabled` | 401 after refresh fails / admin | reconnect, new key, admin |
| `cooldown_until` + `cooldown_reason` | timestamp + `rate_limited`/`exhausted`/`errors` | 429, quota exhaustion, repeated transient errors | time passes; admin reset |
| `blocked_capabilities` | set of capabilities + reason | plan/entitlement errors (e.g. Elicit `api_access_denied`, Consensus `feature_not_allowed`) | admin "re-check" |

An account is usable for capability C iff `enabled ∧ credential = ok ∧ now ≥ cooldown_until ∧ C ∉ blocked`.
There is no combined state enum and no severity order.

### 7.3 Error classification (in the adapter)

Each adapter maps its provider's responses to one of:

```python
class ProviderError(Exception):
    kind: Literal["rate_limited", "exhausted", "auth", "plan", "transient", "bad_request", "target"]
    retry_after: float | None      # seconds, from headers or body
    reset_at: datetime | None      # quota reset if known (e.g. Scite text, billing period end)
```

| kind | Effect |
|---|---|
| `rate_limited` | cooldown until `retry_after` (default 60 s, doubling on repeats, cap 15 min); try next account |
| `exhausted` | cooldown until `reset_at` (default: 1st of next month UTC for monthly plans, next UTC midnight for daily); try next account |
| `auth` | `credential = needs_auth`; try next account |
| `plan` | add capability to `blocked_capabilities`; try next account |
| `transient` | after 3 consecutive on the account: cooldown 30 s doubling (cap 10 min); try next account |
| `bad_request` | return error to consumer; no state change; no other account |
| `target` | this URL/item failed (404, bot protection, paywall); no state change; sequential mode tries next provider |

Shared helpers live in `providers/http.py` (status + credit markers, `Retry-After` parsing), but the decision is the
adapter's: it knows that Tavily 432 means exhausted, Perplexity 401 may mean out-of-credit, Scite reports exhaustion
as tool-error text. Per-provider facts: research/04, research/05 §9.

A success resets the consecutive-error count. Any account that errors transiently on every call simply stays in
cooldown; that covers what a provider circuit breaker would do.

### 7.4 Limits and quota

- **Client-side limiters**, in memory, per account (or `quota_group`), seeded from documented limits where exceeding
  them causes blocks: arXiv 1 req / 3 s (single connection), S2 1 rps, GitHub search 30/min (code 10/min), Crossref
  from its `x-rate-limit-*` headers, Jina keyless 20 RPM. Configurable; not persisted.
- **Quota**: where a provider reports remaining quota (OpenAlex headers, GitHub headers, Firecrawl credit-usage,
  Tavily usage, Elicit `get_usage`), the adapter records the **latest** value and reset time on the account. Shown in
  admin; when remaining is 0, the account goes to `exhausted` cooldown. No time series.
- No background health scheduler. Availability changes only on real calls, on admin "test", and on connect.

### 7.5 Account choice

Priority order, first usable account. (Round-robin is deferred, §16.)

---

## 8. Merge

### 8.1 Normalize

Adapter hits → `Hit{provider, account, rank, title, url, ids, snippet, authors, year, venue, published, raw}`.

### 8.2 Canonicalize

- **IDs**: extract and normalize DOI (lowercase, publisher-URL patterns), arXiv (versionless), PMID, PMCID, OpenAlex,
  S2 from provider fields and URLs. Own module with table-driven tests (no single library is complete; research/07 §2).
- **URL**: `url-normalize` + tracker-parameter removal + fragment drop + sorted query + key-only `https`/`www.`/
  trailing-slash folding.
- **Handle** = first available of `doi:` › `arxiv:` › `pmid:` › `pmcid:` › `openalex:` › `s2:` › `gh:owner/repo[/…]` ›
  `url:<b32(sha256(canon_url))[:20]>`. Stored with an alias table so a handle stays resolvable if a better ID is
  learned later.

### 8.3 Dedup

Two hits are the same item iff they share any normalized strong ID or the same canonical URL. Merge by key set (simple
dict union). Hits without IDs from scholarly providers (e.g. Consensus Free has no DOI) get **one resolver lookup**
(OpenAlex title search / Crossref `query.bibliographic`, accept only an exact normalized-title + year match) to obtain
a DOI before dedup; otherwise they stay separate.

### 8.4 Merged fields

First non-empty value in a fixed provider order per field (e.g. title: Crossref › OpenAlex › S2 › arXiv › provider);
`providers[]` keeps every contributing hit's provider and rank.

### 8.5 Fusion

Plain RRF over one ranked list per provider: `score = Σ 1/(60 + rank)`. Ties → better best-rank. No weights, groups or
per-account aggregation (one account answers per provider per call).

### 8.6 Rerank (optional, off by default)

`Reranker.rerank(query, docs) -> order`. If enabled for a capability, the reranker reorders the top-N (default 40)
fused items and that order is final — no second fusion stage. Backends: Infinity HTTP (shared cluster), Jina API
(existing adapter), Voyage, Cohere. Introduced in P4 after a benchmark (§17).

### 8.7 No implicit enrichment

Search tools return provider-supplied fields only. Editorial status, citation counts, OA status etc. are obtained by
calling `editorial_check`, `paper_metadata`, `citation_verify` explicitly. No consumer ranking preferences
(`recency_weight`, `demote_retracted`) in the engine.

---

## 9. Deadlines and jobs

### 9.1 Ordinary calls

Deadline → partial result (`status: partial`, `coverage` shows who did not finish). No job is created; the consumer
can call again (the cache holds whatever finished).

### 9.2 Async capabilities

`site_crawl`, `deep_literature_search`, `systematic_review`:
- The tool calls `provider.start()`, stores a job row `{id, capability, provider, account, upstream_ref, status,
  result_ref, created_at}`, and waits up to `max_wait_s` for completion; otherwise returns `{status: "running",
  job_id, poll_after_s}`.
- One in-process runner loop calls `provider.poll(ref)` at the provider's `poll_interval_s` for running jobs and stores
  the result.
- `get_job(job_id, wait_s)` returns current status/result, waiting up to `wait_s`.
- On restart, running jobs continue polling from their stored `upstream_ref`.
- No leases, requeue or worker pool; one process, one loop.

---

## 10. Cache

| Cache | Key | TTL |
|---|---|---|
| Query | capability + normalized args | web/news 1 h, paper search/related/graph 1 d, repo/dev 1 d |
| Document | handle or canonical URL + cursor | 7 d; bypassed by `fresh` |

Admin has "clear cache". Changing routing does not invalidate entries automatically (TTLs are short); a manual clear is
the tool for that. Request coalescing is deferred (§16).

---

## 11. Storage

- One SQLite file, WAL, `busy_timeout`. Use whatever access style mcp-gateway's code already uses (sync `sqlite3`
  behind a lock, offloaded to a thread) — no async-driver or Postgres preparation.
- Blobs (documents, raw payloads) under `data/blobs/` with TTL cleanup.
- Secrets: mcp-gateway Fernet envelope encryption.

Tables:

| Area | Tables |
|---|---|
| Auth (mcp-gateway) | `meta`, `oauth_clients`, `auth_codes`, `access_tokens`, `refresh_tokens`, `auth_txns`, `revoked_sessions`, `personal_tokens` |
| Providers | `providers` (name, enabled, option overrides), `accounts` (provider, label, priority, enabled, quota_group, credential, cooldown_until, cooldown_reason, blocked_capabilities, quota_remaining, quota_reset_at), `account_secrets` (encrypted) |
| Routing | `routing` (capability, mode, providers[]) |
| Requests | `requests` (tool, args, status, started/finished, coverage, replay_of), `attempts` (request, provider, account, outcome kind, latency, error message) |
| Evidence | `handles` (handle, canonical ids, url, title), `handle_aliases` |
| Cache/Jobs | `query_cache`, `doc_cache`, `jobs` |

Migrations are added per phase, only for tables that phase uses (§17).

---

## 12. Configuration

- `config.yaml`: server (`public_url`, trusted proxy), auth (users, token TTLs, redirect allowlist), storage path,
  encryption key reference. Nothing else.
- Providers, accounts, secrets and routing entries live in the DB, edited in admin. First start creates the default
  routing entries (§6.2). `cli export` / `cli import` exist for backup, not as a second source of truth.

---

## 13. Admin

API (`/admin/api`, session cookie + CSRF), in engine vocabulary:

| Area | Endpoints |
|---|---|
| Providers & accounts | `GET providers`, `PATCH providers/:name`, `GET/POST accounts`, `PATCH/DELETE accounts/:id`, `POST accounts/:id/test`, `POST accounts/:id/reset` (clear cooldown/blocked) |
| OAuth | `GET accounts/:id/connect` → upstream authorize URL; callback `/oauth/callback` |
| Routing | `GET routing`, `PUT routing/:capability` |
| Requests | `GET requests?…`, `GET requests/:id` (args, attempts, result), `POST requests/:id/replay` |
| Jobs | `GET jobs`, `GET jobs/:id` |
| Tokens | `GET/POST/DELETE tokens` (personal tokens for agentRT) |
| Maintenance | `POST cache/clear`, `GET export`, `POST import` |

UI pages (built from 9router components rebound to this API): **Accounts** (list per provider, status fields from
§7.2, quota remaining, test, connect OAuth, reset), **Routing** (ordered provider list + mode per capability),
**Requests** (log list, detail with attempts, Replay button showing the new result next to the original), **Jobs**.
Replay = re-run the stored tool arguments with current config and cache bypass.

---

## 14. Operating policy

ToS assessment is the owner's (requirements, 2026-09-29). The engine enforces no ToS-derived restrictions; every
provider and account is configuration. Engineering defaults that remain and are configurable: client-side rate
limiters (§7.4), Firecrawl crawl/map caps in the adapter, upstream tool mapping per adapter.

---

## 15. Deployment

- Current host (56 cores, 125 GB RAM, no GPU), shared with `knowledge-server-infra` and OmniRoute.
- Own compose project; `engine` container joins the external `web` network; no published host port (optional
  `127.0.0.1` debug port avoiding 80/3000/8080/20128). CPU/memory limits so it cannot starve RAGFlow/Elasticsearch.
- Ingress: add `research.ducanh.cloud` to the existing tunnel → Caddy site block
  `research.ducanh.cloud:80 { reverse_proxy engine:<port> { flush_interval -1 } }` (SSE unbuffered). Changes in
  `knowledge-server-infra` go through its own process.
- Cloudflare: no bot/JS challenges or Access on `/mcp*`, `/.well-known/*`, `/authorize`, `/token`, `/register`,
  `/revoke`, `/oauth/*`; no redirects on the MCP URL; Access on `/admin*`.
- OAuth AS additions to mcp-gateway: RFC 9207 `iss`, `offline_access`, redirect allowlist (ChatGPT stable + per-connector
  callbacks, claude.ai/claude.com callbacks, Claude Code loopback), PRM per path (`/mcp`, `/mcp/compat`).
- `/healthz` liveness.

Security: no token passthrough (mcp-gateway assertion); SSRF guard on engine-side fetches with resolved-IP pinning and a
response-size cap; secrets encrypted and redacted from logs; admin behind login + Cloudflare Access.

---

## 16. Deferred (add when the trigger is observed)

| Item (from v1) | Add when |
|---|---|
| Per-step routing options: weights, `use` modes, account pinning, nested chains | a real routing need cannot be expressed as an ordered list + mode |
| Round-robin / other account strategies | ≥ 2 accounts of one provider are in use and priority failover wastes quota |
| Provider-level circuit breaker | account cooldowns demonstrably fail to stop hammering a down provider |
| Cost model, per-call budgets, reserves, spend guards | provider spend becomes a real problem visible in usage |
| Quota time series + charts | a question about usage trends cannot be answered from the request log |
| Proactive health checks | stale availability causes user-visible failures |
| Fuzzy dedup (title/author), SimHash/MinHash, syndication relations | logged results show duplicates that IDs/URL miss, at a noticeable rate |
| Preprint ↔ published linking/collapse | duplicates between versions are common in logged scholarly results |
| Weighted/hierarchical RRF, independence groups | an evaluation set shows plain RRF underperforming |
| Implicit enrichment of search results | consumers routinely call `editorial_check`/`paper_metadata` on every search result |
| Durable jobs for ordinary calls, request coalescing | partial results at the deadline are frequent and re-calls are costly |
| Policy versioning, decision traces, replay diff, route simulator | replay + attempt logs are insufficient to understand a routing outcome |
| `SITE_INTERACT` capability | a concrete research workflow needs browser actions |
| Coverage analytics (overlap, facets, gaps) | never by default; maybe an offline analysis script |
| Postgres / multi-process | more than one process or user |
| Prometheus/Grafana metrics | owner asks for it |
| Perplexity, Parallel, Linkup, crawl4ai, Bright Data, DuckDuckGo adapters | a key is provisioned / a gap in coverage |

---

## 17. Roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Walking skeleton** | Fork mcp-gateway; FastMCP pin; RFC 9207; `research.ducanh.cloud` via tunnel + Caddy; tables: providers, accounts, account_secrets, routing, requests, attempts; `web_search` (Exa + Brave/Serper) and `web_read` (Firecrawl → Jina → trafilatura) end-to-end with RRF + exact dedup | ChatGPT (dev mode), claude.ai and Claude Code complete OAuth and get merged results; forced 429 on one provider shows failover in `coverage` |
| **P1 Web + dev** | Remaining web/news/dev providers and tools; limiters; query/doc cache; admin API + minimal Accounts/Requests pages | All web/dev tools work; replay works |
| **P2 Academic open** | OpenAlex, Crossref, S2, arXiv, Scite REST; ID canonicalization + handles; `paper_*`, `citation_graph`, `citation_verify`, `editorial_check`; `/mcp/compat` | DOI/arXiv dedup across providers on fixtures; compat `search`/`fetch` works with OpenAI's schema |
| **P3 Hosted MCP + jobs** | Upstream OAuth per account + lock fix; Undermind, Consensus, Scite MCP, Elicit adapters; jobs table + runner; `deep_literature_search`, `systematic_review`, `site_crawl`, `site_map`; Routing page | Undermind deep search completes through `get_job` across a restart |
| **P4 Rerank (optional)** | Reranker interface; Infinity rerank model (knowledge-server-infra change) + benchmark; enable per capability if it helps | Benchmark recorded; decision documented |

---

## 18. Credits (for README)

`R0Wi/mcp-gateway` @59c1efd (author permission), `vvzvlad/research-mcp` @11f297d (MIT, author permission),
`decolua/9router` @f01fb90 (MIT), `diegosouzapw/OmniRoute` @666ea59 (MIT), `spences10/mcp-omnisearch` (MIT),
`exa-labs/exa-mcp-server`, `firecrawl/firecrawl-mcp-server` (MIT), FastMCP (Apache-2.0), MCP Python SDK (MIT).
