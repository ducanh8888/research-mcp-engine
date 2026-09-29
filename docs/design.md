# Research Engine — Design (v3)

Status: **draft v3**, 2026-09-30. v3 = v2 re-targeted to a private server for local researchers (Claude Code /
agentRT on tailnet machines). v1/v2 in git history.
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
| D2 | Skeleton = **`vvzvlad/research-mcp`**; from `R0Wi/mcp-gateway` copy only the upstream OAuth client, encrypted token store, crypto, migrations runner | 2 |
| D3 | **One MCP tool per capability (16) + `get_job`** on `/mcp` (Streamable HTTP). No compat surface | 4 |
| D4 | Routing = per capability: `fanout` or `sequential` over an ordered provider list; accounts tried in priority order | 6 |
| D5 | Account availability = 3 independent fields (credential, cooldown, blocked capabilities); no global severity | 7 |
| D6 | Errors classified **inside each adapter** into one small typed error set | 7.3 |
| D7 | Merge = normalize → canonical ID/URL → exact-key dedup → **plain RRF (k=60)**; optional rerank replaces order of top-N | 8 |
| D8 | Deadline per call → **partial result**, not a job. Jobs only for inherently async capabilities | 9 |
| D9 | SQLite (WAL); query + document cache with TTL | 10, 11 |
| D10 | Config: bootstrap YAML for server only; providers/accounts/routing/client tokens live in DB, edited in admin | 12 |
| D11 | Admin web UI = **sqladmin** views + custom actions (test, connect OAuth, reset, replay) | 13 |
| D12 | This machine is the **server** holding provider credentials; clients on other machines connect over **Tailscale** to `100.66.213.111:8765`; **per-client bearer tokens**; Docker compose | 15 |
| D13 | Undermind CIMD client document hosted on a small **public repo + GitHub Pages** | 5.2 |
| D14 | No engine-enforced ToS restrictions; ToS is the owner's call | 14 |

Non-goals: hypothesis generation, research prioritization, knowledge admission, campaign opening, scientific
decisions, final source acceptance, **evidence interpretation or preference ranking**. No LLM in the engine.

---

## 1. Architecture

```
tailnet machines: Claude Code sessions · agentRT            (researchers run locally)
        │  HTTP  100.66.213.111:8765/mcp   Authorization: Bearer <client token>
        ▼
server (this machine) — research-engine container (one process)
 ├─ /mcp                   FastMCP 4 Streamable HTTP, both protocol eras, bearer-token auth
 ├─ /admin                 sqladmin UI (login) + custom actions
 ├─ /oauth/callback        upstream OAuth connect (admin's browser)
 ├─ tools ─► router: routing entry → accounts → execute (fanout | sequential) with deadline
 │            providers: http | mcp_oauth | local adapters (each classifies its own errors)
 │            merge: normalize → canonicalize → dedup → RRF → [rerank]
 │            jobs: async capabilities only       cache: query, document
 └─ storage: SQLite (WAL) + blobs/, secrets Fernet-encrypted
        │  outbound internet
 REST: Exa · Firecrawl · Tavily · Brave/Serper · Jina Reader · GitHub · OpenAlex · Crossref · S2 · arXiv · Scite REST
 MCP+OAuth: Undermind · Scite · Elicit · Consensus (REST key preferred)

 public (GitHub Pages, separate public repo): client-metadata.json  ← fetched by Undermind's AS (CIMD)
```

No inbound public exposure: no Cloudflare tunnel, no Caddy, no OAuth authorization server for clients.

---

## 2. Repository layout and provenance

```
research-mcp-engine/                     (private)
├─ pyproject.toml  config.example.yaml  docker-compose.yml  .env.example  THIRD_PARTY_NOTICES.md
├─ src/research_engine/
│  ├─ server/     app.py (FastAPI + FastMCP mount + sqladmin), tools.py, schemas.py, auth.py (bearer tokens), instructions.md
│  ├─ providers/  base.py, registry.py, errors.py, http.py, url_guard.py, pdf.py,
│  │              web/*.py, scholar/*.py, dev/*.py, mcp/{client,oauth,token_store}.py + per-provider modules
│  ├─ router/     routing.py, execute.py, accounts.py
│  ├─ merge/      normalize.py, canonical.py, dedup.py, fusion.py, rerank.py
│  ├─ jobs/       runner.py
│  ├─ admin/      views.py (sqladmin ModelViews + actions), oauth_connect.py
│  ├─ storage/    db.py (SQLAlchemy models), crypto.py, blobs.py, migrations/
│  ├─ cache.py  config.py  cli.py
└─ tests/

research-engine-client/                  (public, GitHub Pages)
└─ client-metadata.json                  CIMD document for upstream OAuth (no secrets)
```

| Our module | Seeded from | Mode |
|---|---|---|
| server skeleton, `providers/http.py`, `url_guard.py`, `pdf.py`, web adapters, read cascade, settings/config errors, tests | research-mcp @11f297d | copy + modify (becomes the base) |
| `providers/mcp/{client,oauth,token_store}.py`, `storage/crypto.py`, migrations runner | mcp-gateway @59c1efd (`upstream.py`, `storage.py`, `db_migrations.py`) | copy, key by account, fix OAuth lock (§5.4) |
| `admin/views.py` | sqladmin (BSD-3) | dependency |
| query-operator parsing | mcp-omnisearch | port (only if needed) |

Not taken from mcp-gateway: OAuth authorization server, DCR/CIMD server side, login/consent UI, proxy/mount aggregation.
Not taken from 9router/OmniRoute: UI (sqladmin instead); only small algorithms already reflected in §7.

---

## 3. Runtime

- FastMCP 4.0.10 pinned exactly; bump only with the e2e suite (inherited from mcp-gateway) green.
- One uvicorn worker. Provider I/O async; CPU work (PDF parse) in `anyio.to_thread`.
- FastAPI outer app: FastMCP ASGI mounted at `/mcp`, sqladmin at `/admin`, OAuth callback at `/oauth/callback`.
- Listen on `0.0.0.0:8765` inside the container; compose publishes it only on `127.0.0.1` and the tailnet IP.
- Not used: FastMCP proxy/mount/transforms, `fastmcp-tasks`.

---

## 4. MCP surface

### 4.1 `/mcp` — native tools

Consumers: Claude Code (tool search loads definitions on demand; results ≤ 25k tokens, warning at 10k) and agentRT.
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

### 4.2 Output

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
- **mcp_oauth**: one FastMCP `Client` per account, tokens encrypted per account. Client registration: DCR where the
  upstream supports it (Scite, Elicit, Consensus); **CIMD for Undermind** with `client_id` =
  `https://ducanh8888.github.io/research-engine-client/client-metadata.json`. `redirect_uris` in that document:
  `http://100.66.213.111:8765/oauth/callback` and `http://127.0.0.1:8765/oauth/callback` (the admin's browser completes
  the flow on the tailnet). Exact-`state` callback routing from mcp-gateway.
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

One entry per capability, stored in DB, edited in admin:

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

- `config.yaml`: listen address/port, data dir, encryption key reference, admin user (password hash). Nothing else.
- Providers, accounts, secrets, routing entries and client tokens live in the DB, edited in the admin UI. First start
  creates the default routing entries (§6.2). `cli export` / `cli import` are for backup only.

---

## 13. Admin

**sqladmin** mounted at `/admin` (session login with the single admin user from config), reachable only on the tailnet:

| View | Model | Custom actions |
|---|---|---|
| Accounts | `accounts` (+ encrypted secret field, write-only) | **Test** (one cheap call), **Connect OAuth** (redirect to upstream authorize), **Reset** (clear cooldown/blocked) |
| Providers | `providers` (enabled, option overrides) | — |
| Routing | `routing` (mode, ordered provider list) | validation on save (provider implements capability) |
| Client tokens | `client_tokens` (label, hash, created, last used, revoked) | **Create** (shows token once), **Revoke** |
| Requests | `requests` + inline `attempts` (read-only) | **Replay** (re-run with current config, cache bypass; opens the new request next to the original) |
| Jobs | `jobs` (read-only) | **Cancel** |

Availability fields and latest quota are columns on the Accounts list. No JSON admin API beyond what the actions need.

---

## 14. Operating policy

ToS assessment is the owner's (requirements, 2026-09-29). The engine enforces no ToS-derived restrictions; every
provider and account is configuration. Engineering defaults that remain and are configurable: client-side rate
limiters (§7.4), Firecrawl crawl/map caps in the adapter, upstream tool mapping per adapter.

---

## 15. Deployment

- Server = this machine (56 cores, 125 GB RAM, no GPU), also running `knowledge-server-infra` and OmniRoute; it holds all
  provider credentials. Clients are other tailnet machines (`ducanh-1`, `ducanh`, …) running Claude Code / agentRT.
- Docker compose project `research-engine`: one `engine` service (python:3.12-slim, non-root, volume `/data`),
  `ports: ["100.66.213.111:8765:8765", "127.0.0.1:8765:8765"]`, restart `unless-stopped`, CPU/memory limits so it cannot
  starve RAGFlow/Elasticsearch. No dependency on `knowledge-server-infra`.
- Client auth: `Authorization: Bearer <token>`; tokens created in admin, stored hashed, revocable; `requests` records
  which token called. Client setup:
  `claude mcp add --transport http research http://100.66.213.111:8765/mcp --header "Authorization: Bearer <token>"`.
- Tailnet is the network boundary; bearer tokens identify and revoke clients. Plain HTTP inside the tailnet (WireGuard
  encrypts transport).
- `research-engine-client` public repo with GitHub Pages serving `client-metadata.json` (static, no secrets).
- `/healthz` liveness.

Security: bearer token on `/mcp`; admin login on `/admin`; secrets Fernet-encrypted and redacted from logs; SSRF guard
on engine-side fetches (resolved-IP pinning, response-size cap).

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
| Public ingress (tunnel), OAuth authorization server, `/mcp/compat` (`search`/`fetch`) for ChatGPT/claude.ai | a consumer outside the tailnet (claude.ai, ChatGPT, OpenAI deep research) is needed |
| React admin UI (9router) | sqladmin becomes a real limitation |
| Perplexity, Parallel, Linkup, crawl4ai, Bright Data, DuckDuckGo adapters | a key is provisioned / a gap in coverage |

---

## 17. Roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Walking skeleton** | research-mcp as base; FastAPI + FastMCP pin; bearer-token auth; SQLite models (providers, accounts, routing, client_tokens, requests, attempts); `web_search` (Exa + Brave/Serper) and `web_read` (Firecrawl → Jina → trafilatura) with RRF + exact dedup; docker compose on tailnet; minimal sqladmin (accounts, tokens, requests) | Claude Code on another tailnet machine calls both tools and gets merged results; forced 429 on one provider shows failover in `coverage` |
| **P1 Web + dev** | Remaining web/news/dev providers and tools; limiters; query/doc cache; Replay action; Routing view | All web/dev tools work; replay works |
| **P2 Academic open** | OpenAlex, Crossref, S2, arXiv, Scite REST; ID canonicalization + handles; `paper_*`, `citation_graph`, `citation_verify`, `editorial_check` | DOI/arXiv dedup across providers on fixtures |
| **P3 Hosted MCP + jobs** | Upstream OAuth per account (+ lock fix, CIMD public repo); Undermind, Consensus, Scite MCP, Elicit adapters; jobs + runner; `deep_literature_search`, `systematic_review`, `site_crawl`, `site_map`; Connect OAuth action | Undermind deep search completes through `get_job` across a restart |
| **P4 Rerank (optional)** | Reranker interface; backend choice (Infinity shared cluster, API) + benchmark | Benchmark recorded; decision documented |

---

## 18. Credits (for README)

`R0Wi/mcp-gateway` @59c1efd (author permission), `vvzvlad/research-mcp` @11f297d (MIT, author permission),
`decolua/9router` @f01fb90 (MIT), `diegosouzapw/OmniRoute` @666ea59 (MIT), `spences10/mcp-omnisearch` (MIT),
`exa-labs/exa-mcp-server`, `firecrawl/firecrawl-mcp-server` (MIT), FastMCP (Apache-2.0), MCP Python SDK (MIT).
