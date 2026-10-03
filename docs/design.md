# Research Engine — Design (v3)

Status: **draft v3, audited 2026-10-02**. v3 = v2 re-targeted to a private server for local researchers (Claude Code /
agentRT on tailnet machines). v1/v2 in git history. This is a design, not an implemented or live-tested system.
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
| D9 | SQLite (WAL) via SQLAlchemy models/sessions for engine storage and sqladmin; query + document cache with TTL | 10, 11 |
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
- FastAPI outer app: create `mcp_app = mcp.http_app(path="/")`, mount it at `/mcp`, and run `mcp_app.lifespan` in the outer FastAPI lifespan (alongside DB/job startup and shutdown). sqladmin is at `/admin`; the upstream OAuth callback is at `/oauth/callback`. Test that the endpoint is `/mcp`, **not** `/mcp/mcp`.
- Listen on `0.0.0.0:8765` inside the container; compose publishes it only on `127.0.0.1` and the tailnet IP. Check the IP exists before binding; a changed tailnet IP requires updating the compose binding and client URL.
- Require a hashed, non-revoked client bearer token for every `/mcp` transport method, including discovery/list and legacy session requests; reject absent/invalid tokens. Validate an incoming `Origin` when present (403 on an untrusted origin). `/admin` uses its own authenticated session; `/oauth/callback` has separate OAuth state/session validation, not MCP bearer auth. Do not forward a client bearer token upstream.
- Not used: FastMCP proxy/mount/transforms, `fastmcp-tasks`.

---

## 4. MCP surface

### 4.1 `/mcp` — native tools

Consumers: Claude Code (tool search loads definitions on demand; results ≤ 25k tokens, warning at 10k) and agentRT.
All tools: `title`, `openWorldHint: true`, `outputSchema` + `structuredContent` + one compact text
block; descriptions state when to use the tool and its limits (first ~2,000 chars). Ordinary retrieval and `get_job`
use `readOnlyHint: true`; async launch tools create upstream jobs/workspace state and use `readOnlyHint: false`
(`destructiveHint: false`).

| Tool | Capability | Mode | Input |
|---|---|---|---|
| `web_search` | WEB_SEARCH | fanout | `query`, `limit`, `domains?`, `date_from/to?` |
| `news_search` | NEWS_SEARCH | fanout | `query`, `limit`, `recency?` |
| `web_read` | WEB_READ | sequential | `target` (URL or handle), `fresh?`, `cursor?` |
| `site_map` | SITE_MAP | sequential | `url`, `limit?` |
| `site_crawl` | SITE_CRAWL | sequential (async) | `url`, `limit?`, `max_depth?`, `include/exclude_paths?` |
| `paper_search` | PAPER_SEARCH | fanout | `query`, `limit`, `year_from/to?`, `filters?` (passed to providers that support them) |
| `paper_read` | PAPER_READ | sequential | `target` (handle/DOI/arXiv/PMID/URL), `cursor?` |
| `paper_metadata` | PAPER_METADATA | sequential | `ids[]` or `citation` string |
| `paper_related` | PAPER_RELATED | fanout | `seeds[]`, `mode` (`similar|citing|cited`) |
| `citation_verify` | CITATION_VERIFY | fanout | `citation`, `claim?` |
| `citation_graph` | CITATION_GRAPH | fanout | `seeds[]`, `direction`, `depth ≤ 2` |
| `editorial_check` | EDITORIAL_CHECK | fanout | `ids[]` |
| `systematic_review` | SYSTEMATIC_REVIEW | sequential (async) | `question`, `criteria[]`, provider options |
| `deep_literature_search` | DEEP_LITERATURE_SEARCH | sequential (async) | `goal` |
| `developer_search` | DEVELOPER_SEARCH | fanout | `query`, `repos?`, `language?` |
| `repo_search` | REPO_SEARCH | fanout | `query`, `language?`, `min_stars?`, `mode?` (`repos|code`) |
| `get_job` | — | local | `job_id`, `wait_s ≤ 40` |

17 tools. `SITE_INTERACT` is deferred (§16): browser actions are outside search/retrieve/verify/merge/rank.
There is no separate "expand handle" tool: reads go through `web_read`/`paper_read`, metadata through
`paper_metadata`, all of which accept handles.

The engine does **not** re-classify requests: the consumer chose the capability by choosing the tool.

### 4.2 Output

Each tool has a capability-specific `outputSchema`. Ordinary successful calls share `status: complete|partial`,
`coverage: {ok: [], failed: [{p, reason}], skipped: [{p, reason}]}`, and `request_id`. Search/related/repo results
carry `items: [{handle, title, url, snippet?, providers: [{p, rank}], ...provider-supplied fields}]`. For example:

```json
{
  "status": "partial",
  "items": [{ "handle": "doi:10.1038/s41586-020-2286-9", "title": "…", "url": "https://doi.org/…",
              "snippet": "…", "year": 2020, "venue": "Nature", "authors": ["…"],
              "providers": [{"p": "openalex", "rank": 1}, {"p": "s2", "rank": 2}] }],
  "coverage": { "ok": ["openalex", "s2"], "failed": [{"p": "undermind", "reason": "timeout"}],
                "skipped": [{"p": "scite", "reason": "cooling"}] },
  "request_id": "r_…"
}
```

Other result payloads stay small but are **not** coerced into search hits: `web_read`/`paper_read` return
`document: {handle, url, text, next_cursor?, source}` (text or abstract is labelled as such); `site_map` returns
`urls[]`; `paper_metadata(ids[])` returns `records[]` keyed by requested ID plus `not_found[]` and per-ID coverage;
`citation_graph` returns `nodes[]`, `edges[]`, `truncated`; `editorial_check` returns `checks[]` keyed by ID with
notices and source assertions (missing data means unknown, not clear); `citation_verify` returns a bibliographic
`match`/`mismatch`/`unknown` and source metadata, with `claim_evidence[]` **only** when a claim was supplied and a
provider returned attributable claim-level passages. Aggregate Scite REST tallies can be included as labelled
`citation_tallies` but do not verify a particular claim. Async tools and `get_job` return the job envelope in §9.2;
their completed `result` uses the relevant capability payload (e.g. crawl URLs/documents or ranked papers, no
provider-generated narrative interpretation).

- Fields are what providers returned (merged per §8.4), plus `providers` (who returned it, at which rank). No
  computed quality signals; `len(providers)` is visible to the consumer as-is.
- Default search `limit` 8 (max 25); target ≤ ~8k tokens. Read tools page by `cursor` (~20k chars per page).
- `complete` includes a genuinely empty result if at least one provider completed successfully and none is pending;
  report other failed/skipped providers in coverage. `partial` means at least one attempt missed the deadline; return
  completed results/coverage even if the payload is empty. If all providers fail or are unavailable and none is
  pending, return a tool error (`NO_PROVIDER_AVAILABLE` with coverage), not an empty success. A missing individual
  target is `NOT_FOUND` only when definitively absent (or one entry in `not_found[]` for batched metadata).
- Errors are MCP tool errors (`isError: true`) with `{error: {code, message, retry_after?}}` and codes
  `INVALID_INPUT`, `NO_PROVIDER_AVAILABLE`, `NOT_FOUND`, `INTERNAL`. Validate tool inputs before routing; a provider's
  refusal of otherwise valid input appears in `coverage.failed`, not as a global error that hides successful peers.

---

## 5. Providers

### 5.1 Interface

```python
class Provider(Protocol):
    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    async def execute(self, ctx: CallContext, cap: Capability, req: Request) -> Result: ...
    # For a provider with a native async upstream only:
    async def start(self, ctx, cap, req) -> str: ...          # returns recoverable upstream ref
    async def poll(self, ctx, ref: str) -> JobUpdate: ...     # running/completed/failed
    poll_interval_s: ClassVar[float] = 15
```

`CallContext` = http client, resolved account credential, deadline. `Result` has a capability-specific payload
(§4.2): ranked `hits: list[Hit]` for search (upstream rank/fields retained), a `document` for reads, or typed
metadata/graph/verification/map records. Adapters raise `ProviderError` (§7.3). Do not wrap generated upstream QA
or report prose in a raw document or rank it as primary evidence.

### 5.2 Transports

- **http**: research-mcp `_http.py` (retry transient errors, never retry 402/429, credit-body markers), extended to
  return status/headers so adapters can classify. Raw HTTP for Firecrawl (SDK drops `creditsUsed`) and Tavily (SDK
  maps 429/432/433 to misleading exceptions); Exa raw HTTP with explicit `contents`.
- **mcp_oauth**: one FastMCP `Client` per account, tokens encrypted per account. Client registration: DCR where the
  upstream supports it (Scite, Elicit, Consensus); **CIMD for Undermind** with `client_id` =
  `https://ducanh8888.github.io/research-engine-client/client-metadata.json`. Publish **one** matching redirect URI
  in that public document: `https://<server>.<tailnet>.ts.net/oauth/callback` (replace placeholders with the actual
  stable Tailscale HTTPS Serve hostname). Serve that callback to the admin browser over the tailnet only; HTTPS for
  upstream OAuth is separate from the HTTP `/mcp` endpoint used by local tailnet clients. The remote browser's
  `127.0.0.1` is **not** this server, and non-loopback `http://100.66.213.111` is not a valid OAuth redirect.
  Confirm Undermind accepts the exact HTTPS redirect/CIMD URL in a real connect flow before calling P3 done; if
  rejected, leave Undermind unconnected and decide on an alternative separately, rather than exposing `/mcp` or
  `/admin` publicly. Initiate the admin Connect action on the **same HTTPS origin** used by the callback so its
  session cookie returns; route the callback to the existing app. Reuse mcp-gateway's exact-`state` flow, bound to
  the initiating authenticated admin session and account, expiring and consumed once.
- **local**: trafilatura, pypdf.

### 5.3 Provider set v1

| Group | Providers |
|---|---|
| Web/news | Exa, Firecrawl, Tavily, Brave and/or Serper, Jina Reader (keyless), trafilatura |
| Academic | OpenAlex, Crossref, Semantic Scholar, arXiv, Scite public REST; Undermind (MCP), Consensus (REST key), Scite (MCP), Elicit (MCP/REST) |
| Developer | GitHub, Firecrawl developer search, Exa |

Hosted-MCP adapters map each capability to specific upstream tools (config-extendable). Consensus/Undermind return
Markdown → per-adapter parsers with fixture tests. Undermind: one dedicated workspace; `sample_abstract` templated from
the query; cite keys resolved to DOIs with one batched `get_paper_info`. `read_pdfs` returns generated question answers,
not source text: it is **not** a `PAPER_READ` provider in v1. Deep-search adapters retain ranked paper identities and
provenance, not an upstream-generated narrative summary.

### 5.4 Required fix

SDK `OAuthClientProvider` holds its lock across the whole upstream request, serializing parallel calls on one OAuth
account. Subclass so the lock covers only token init/refresh/401 re-auth; keep a per-account refresh mutex; test N
parallel calls ≈ max latency.

### 5.5 Upstream tool compatibility

On connect (and on startup when a stored OAuth connection is usable), the adapter checks that the upstream
`tools/list` contains the tools and parameters it maps. A mismatch is shown in admin as "adapter incompatible"
and the adapter's calls fail with a clear error. This is **not** part of availability state (§7).

---

## 6. Routing

### 6.1 Routing entry

One entry per capability, stored in DB, edited in admin:

```yaml
paper_search:  {mode: fanout,     providers: [openalex, s2, undermind, consensus, scite, elicit]}
web_read:      {mode: sequential, providers: [firecrawl, exa, jina, tavily, trafilatura]}
```

- `fanout`: call every available provider in the list in parallel; merge ranked hits (§8), or combine
  capability-specific records/edges without applying RRF to non-search results (§4.2).
- `sequential`: try providers in order; first usable answer wins. For `paper_metadata(ids[])`, apply this fallback
  **per ID**: a provider resolving only part of the batch does not hide the remaining IDs.
- Async capabilities use the same `sequential` route to choose **one** provider/account to start a job (§9.2);
  `job` is an execution lifecycle, not a third routing mode.
- A provider may appear only if it implements the capability (validated on save); route seeds contain only
  providers whose adapters have shipped in that phase.

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
| SITE_CRAWL | sequential (async) | Firecrawl |
| PAPER_SEARCH | fanout | OpenAlex, S2, Undermind, Consensus, Scite, Elicit, arXiv |
| PAPER_METADATA | sequential | OpenAlex, Crossref, S2, arXiv |
| PAPER_READ | sequential | arXiv, OpenAlex OA PDF, Scite `read_fulltext` |
| PAPER_RELATED | fanout | S2, OpenAlex, Undermind |
| CITATION_VERIFY | fanout | Crossref, OpenAlex, Scite (REST tallies + MCP) |
| CITATION_GRAPH | fanout | OpenAlex, S2, Scite |
| EDITORIAL_CHECK | fanout | Crossref, OpenAlex, Scite REST |
| DEEP_LITERATURE_SEARCH | sequential (async) | Undermind, Elicit |
| SYSTEMATIC_REVIEW | sequential (async) | Elicit |
| DEVELOPER_SEARCH | fanout | Firecrawl developer, GitHub issues, Exa |
| REPO_SEARCH | fanout | GitHub, Firecrawl developer |

### 6.3 Execution

- Each call has one deadline (`max_wait_s`, default 40, clamp 5–50; hosts cut at ~60 s).
- Validate the tool input once before routing (`INVALID_INPUT` for a genuinely invalid request). Adapters translate
  supported filters/parameters; a provider-specific rejection of a valid request is that provider's failed attempt.
- **fanout**: start all available providers; at the deadline return what has arrived (`status: partial` if any
  provider is still running; stragglers are cancelled and reported as timeout). No quorum/grace tuning in v1.
  A provider's error does not discard successful peers. A successful zero-hit answer is `coverage.ok`.
- **sequential**: per-provider timeout = remaining deadline; on error or empty result, next provider. For reads,
  "empty" is decided by the adapter (no text, known block/placeholder page), not by a global character threshold.
  `paper_metadata(ids[])` retries only unresolved IDs at each next provider and returns per-ID misses.
- Within a provider, accounts are tried in priority order; an account error that is not about the request (§7.3)
  moves to the next account. A provider with no available account is skipped and reported in `coverage.skipped`.
- If the deadline expires, return `partial` with completed/failed/skipped coverage when attempts were cancelled
  by that deadline; if all attempts have ended without any successful provider, return `NO_PROVIDER_AVAILABLE`.
  Use `NOT_FOUND` for an individual read target only when an authoritative resolver definitively reports absence;
  an upstream 404 for one read source means `target` failure and allows fallback, not a global absence claim.
  Upstream errors must not become empty success or false absence.

---

## 7. Accounts and availability

### 7.1 Model

```
provider ── account (credential, priority, enabled, quota_group?)
```

- `quota_group` (optional string): accounts sharing a limit (Exa/Firecrawl keys of one team) cool down together.
- Any number of accounts per provider. Keyless providers use an internal account entry without a secret so they
  participate in availability, routing and coverage like keyed providers.

### 7.2 Availability — three independent fields per account

| Field | Values | Set by | Cleared by |
|---|---|---|---|
| `credential` | `ok` · `needs_auth` · `disabled` | 401 after refresh fails / admin | reconnect, new key, admin |
| `cooldown_until` + `cooldown_reason` | timestamp + `rate_limited`/`exhausted`/`errors` | 429, quota exhaustion, repeated transient errors | time passes; admin reset |
| `blocked_capabilities` | set of capabilities + reason | plan denial of an entire capability (e.g. Elicit `api_access_denied` on Basic) | admin "re-check" |

An account is usable for capability C iff `enabled ∧ credential = ok ∧ now ≥ cooldown_until ∧ C ∉ blocked`.
There is no combined state enum and no severity order. Feature/parameter-level denials (e.g. paid pagination
or full-text chunks while Free search works) are handled inside the adapter, **not** by blocking all of C.

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
| `plan` | if the whole capability is unavailable, block it for that account and try the next; for a request-only feature restriction, leave the capability usable and report a failed attempt (or omit an unsupported optional feature) |
| `transient` | after 3 consecutive on the account: cooldown 30 s doubling (cap 10 min); try next account |
| `bad_request` | upstream refuses a valid input for this adapter: no state change; sequential tries the next provider, fanout records failure without cancelling peers. Globally invalid tool input is rejected before routing |
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
  `url:<b32(sha256(canon_url))[:20]>`. Store URL handles and their target URL already in P0; when a better ID is
  learned later, keep the prior handle as an alias. Never rewrite aliases across conflicting strong-ID clusters;
  if a URL is ambiguous across distinct ID clusters, keep their ID handles distinct and require an explicit ID
  rather than arbitrarily resolving a shared URL handle.

### 8.3 Dedup

Two hits may merge on a normalized strong ID or the same canonical URL **only when their known strong IDs do
not conflict**. Different DOIs (including preprint versus published DOI), arXiv IDs or PMIDs are distinct evidence:
never collapse them through a shared URL or a secondary OpenAlex/S2 ID; keep the source IDs/provenance separate.
A simple key-to-cluster map with an ID-conflict check is sufficient; no fuzzy matching/union-find policy language.
Hits without IDs from scholarly providers (e.g. Consensus Free has no DOI) get **one resolver lookup**
(OpenAlex title search / Crossref `query.bibliographic`, accept only an exact normalized-title + year match) to obtain
a DOI before dedup; otherwise they stay separate.

### 8.4 Merged fields

First non-empty value in a fixed provider order per field (e.g. title: Crossref › OpenAlex › S2 › arXiv › provider);
`providers[]` keeps every contributing hit's provider and rank. For non-search tools (§4.2), preserve source
assertions/edges instead of choosing one provider's answer and applying rank fusion.

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

Deadline → partial result (`status: partial`, `coverage` shows who did not finish). No job is created; a repeated
call re-attempts unfinished providers rather than receiving a cached partial response (§10).

### 9.2 Async capabilities

`site_crawl`, `deep_literature_search`, `systematic_review` have sequential provider routing and one job per call:
- Try eligible providers/accounts in order until `start()` succeeds. A definite start failure may try the next;
  **never fail over automatically after a start may have succeeded**, including on ambiguous timeout or poll failure
  (these upstream operations can cost credits/create workspace state). Return a clear error/unknown-start outcome
  rather than silently creating duplicate upstream work.
- Persist `{id, client_token_id, capability, provider, account, upstream_ref, status, result_ref, created_at,
  updated_at, last_error}` as soon as a successful start yields its ref; poll using that same provider/account. Return
  `{status: "running", job_id, poll_after_s}` if not complete within `max_wait_s`, otherwise return
  `{status: "completed", job_id, result}`. Terminal states are `completed`, `failed`, `cancelled`.
- One in-process runner polls running jobs and persists result or terminal failure. Transient poll errors can retry
  after a delay; unrecoverable upstream failure becomes `failed` with reason. `get_job(job_id, wait_s)` returns the
  stored status and result/error, waiting up to `wait_s`. On restart, resume polling rows with a persisted ref.
- Admin Cancel stops local polling; call an upstream cancel operation only if that adapter supports it, and report
  whether upstream work was actually stopped. Revoking a client token blocks its further `get_job` calls but does
  not silently cancel upstream work. A crash between upstream start and ref persistence can orphan work;
  report this limitation and require manual reconciliation rather than promising exactly-once launch.
- A successful job belongs to the client token that created it; `get_job` checks that token (admin may inspect all
  jobs). No leases, requeue or worker pool; one process, one loop.

---

## 10. Cache

| Cache | Key | TTL |
|---|---|---|
| Query | capability + normalized args | web/news 1 h, paper search/related/graph 1 d, repo/dev 1 d |
| Document | handle or canonical URL + cursor (source text only) | 7 d; bypassed by `fresh` |

Cache **only complete query results**; never cache a deadline-partial envelope as if it were complete. Document cache
contains question-independent source text, not upstream QA; a fresh read bypasses both engine and supported upstream
caches. Admin has "clear cache". Changing routing does not invalidate entries automatically (TTLs are short); manual
clear or `fresh` is the explicit refresh path. Request coalescing is deferred (§16).

---

## 11. Storage

- One SQLite file, WAL, `busy_timeout`, with SQLAlchemy mapped models and one session/transaction path for engine
  storage and sqladmin. Offload blocking DB/PDF work from the event loop; no Postgres or async-driver preparation.
  Adapt the useful migration runner from mcp-gateway but **do not import its client-facing OAuth AS schema or sync
  `sqlite3` storage layer**. The upstream OAuth token store uses the same DB/session path keyed by account.
- Blobs (documents, raw payloads) under `data/blobs/` with TTL cleanup.
- Secrets: mcp-gateway Fernet envelope-encryption logic adapted to the engine's secret rows; hashes (not raw values)
  for client bearer tokens.

Tables:

| Area | Tables |
|---|---|
| Bootstrap/auth | `meta` (encryption metadata), `client_tokens` (hash, label, revoked, last_used) |
| Providers | `providers` (name, enabled, option overrides), `accounts` (provider, label, priority, enabled, quota_group, credential, cooldown_until, cooldown_reason, blocked_capabilities, quota_remaining, quota_reset_at), `account_secrets` (encrypted API keys/upstream OAuth tokens and client registration per account) |
| Routing | `routing` (capability, mode, providers[]) |
| Requests | `requests` (tool, args, status, started/finished, coverage, replay_of, client_token_id), `attempts` (request, provider, account, outcome kind, latency, error message) |
| Evidence | `handles` (handle, canonical IDs, URL, title), `handle_aliases` |
| Cache/Jobs | `query_cache`, `doc_cache`, `jobs` |

P0 creates `meta`, providers/accounts/secrets/routing/client tokens/requests/attempts and URL handles/aliases; later
migrations add only the tables/columns needed in that phase (§17). Back up the DB, blob directory, config and
operator-held encryption key together; a missing or wrong key is a startup error, not an empty replacement store.

---

## 12. Configuration

- `config.yaml`: listen address/port, data dir, encryption key-file reference, admin user (password hash) and
  admin session secret reference. Keep key material and config with hashes out of git; create key/password hash
  with CLI helpers before first start. Missing key/hash fails startup rather than silently generating new credentials.
- On an empty volume: run P0 migrations; seed the provider **catalog** for adapters shipped so far and only their
  compatible route entries (P0: Exa + the selected Brave/Serper search provider; Firecrawl, Jina Reader,
  trafilatura for reads). Create a credential-free local account for keyless readers; no API key or OAuth secret
  is seeded. The owner logs in on `/admin`, adds API-key accounts,
  creates a client token shown once, and configures their remote client. Subsequent starts retain DB edits; later
  migrations/phase installs add new provider definitions without overwriting existing accounts or routes.
- Providers, accounts, secrets, routing entries and client tokens live in the DB, edited in admin. `cli export` /
  `cli import` are for backup only; verify a restore with the same encryption key before relying on it.

---

## 13. Admin

**sqladmin** mounted at `/admin` (session login with the single admin user from config), reachable via the tailnet
or server loopback; not publicly published. Forms and custom actions require admin authentication and CSRF
protection, with HttpOnly/SameSite cookies (Secure when using HTTPS). Do not show stored API keys or OAuth tokens
on list/detail pages. At P0, expose Accounts, Providers, Routing, Client tokens and Requests so bootstrap is usable:

| View | Model | Custom actions |
|---|---|---|
| Accounts | `accounts` (+ encrypted secret field, write-only) | **Test** (one cheap call), **Reset** (clear cooldown/blocked); **Connect OAuth** added in P3 |
| Providers | `providers` (enabled, option overrides) | — |
| Routing | `routing` (mode, ordered provider list) | validation on save (provider implements capability) |
| Client tokens | `client_tokens` (label, hash, created, last used, revoked) | **Create** (shows token once), **Revoke** |
| Requests | `requests` + inline `attempts` (read-only) | **Replay** added in P1 (re-run with current config, cache bypass; opens the new request next to the original) |
| Jobs | `jobs` (read-only; P3) | **Cancel** (§9.2) |
| Cache | — (P1) | **Clear** query/document cache |

Availability fields and latest quota are columns on the Accounts list. Secret entry is write-only; token creation
shows the value once. Connect OAuth becomes available in P3 and uses exact, single-use state bound to that admin
session/account (§5.2). No JSON admin API beyond what the actions need.

---

## 14. Operating policy

ToS assessment is the owner's (requirements, 2026-09-29). The engine enforces no ToS-derived restrictions; every
provider and account is configuration. Engineering defaults that remain and are configurable: client-side rate
limiters (§7.4), Firecrawl crawl/map caps in the adapter, upstream tool mapping per adapter.

---

## 15. Deployment

- Server = this machine (56 cores, 125 GB RAM, no GPU), also running `knowledge-server-infra` and OmniRoute; it holds all
  provider credentials. Clients are other tailnet machines (`ducanh-1`, `ducanh`, …) running Claude Code / agentRT.
- Docker compose project `research-engine`: one `engine` service (python:3.12-slim, non-root, writable `/data`
  volume owned by the container UID), `ports: ["100.66.213.111:8765:8765", "127.0.0.1:8765:8765"]`, restart
  `unless-stopped`, CPU/memory limits so it cannot starve RAGFlow/Elasticsearch. No dependency on
  `knowledge-server-infra`. Check Docker firewall/published-port reachability from a tailnet client and deny
  off-tailnet hosts; if the tailnet IP changes, update compose and clients.
- Client auth: `Authorization: Bearer <token>`; tokens created in admin, stored hashed, revocable; `requests` records
  which token called. Client setup:
  `claude mcp add --transport http research http://100.66.213.111:8765/mcp --header "Authorization: Bearer <token>"`.
- Tailnet is the network boundary; bearer tokens identify and revoke clients. Plain HTTP inside the tailnet (WireGuard
  encrypts transport).
- For P3 upstream OAuth only: serve the admin Connect action and `/oauth/callback` on the **same** stable tailnet
  HTTPS origin/certificate (e.g. Tailscale Serve) so the authenticated admin cookie survives the redirect. This
  HTTPS origin is tailnet-only; `/mcp` and `/admin` remain tailnet-only, and client authentication remains
  per-client bearer rather than an OAuth AS. The `research-engine-client` public repo serves the static
  CIMD `client-metadata.json` (no secrets); it does **not** expose this server. Prove that the upstream accepts the
  exact HTTPS redirect before enabling Undermind.
- `/healthz` liveness.

Security: bearer token on every `/mcp` request; admin login + CSRF protection on `/admin`; upstream callback
bound to admin session and one-time state; secrets Fernet-encrypted and redacted from logs; SSRF guard on
engine-side fetches (resolved-IP pinning, response-size cap).

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
| **P0 Walking skeleton** | research-mcp as base; pinned FastAPI/FastMCP mount and lifespan; SQLite/SQLAlchemy models (provider catalog, accounts/secrets, routing, client tokens, requests/attempts, URL handles/aliases); `web_search` (Exa + Brave/Serper) and `web_read` (Firecrawl → Jina → trafilatura) with RRF + exact dedup; compose on tailnet; minimal sqladmin (providers, routing, accounts, tokens, requests) and first-boot flow | From another tailnet machine, Claude Code and agentRT (when available) discover/list/call both tools with valid tokens; missing/revoked tokens and bad Origin are rejected, and off-tailnet port access is denied. `web_read(handle)` works after restart; forced account or sequential-provider failure proves real failover, while a fanout 429 appears in coverage. If agentRT is unavailable, record that acceptance as pending rather than claiming it passed |
| **P1 Web + dev** | Remaining synchronous web/news/dev providers and tools; limiters; query/doc cache; Replay and Clear Cache actions | P1 web/news/developer/repo tools work; partial results are not served as complete cache hits; replay works (site map/crawl are P3) |
| **P2 Academic open** | OpenAlex, Crossref, S2, arXiv, Scite REST; ID canonicalization; typed `paper_*`, `citation_graph`, `citation_verify`, `editorial_check` outputs | DOI/arXiv dedup preserves conflicting versions; metadata batch falls back per ID; graph edges/editorial notices and bibliographic versus claim evidence remain distinct in fixture tests |
| **P3 Hosted MCP + jobs** | Upstream OAuth per account (+ lock fix, CIMD public repo and tailnet HTTPS callback); Undermind, Consensus, Scite MCP, Elicit adapters; sequential job start + runner; `deep_literature_search`, `systematic_review`, Firecrawl `site_crawl`, `site_map`; Connect OAuth action | Live Undermind CIMD OAuth accepts the exact HTTPS redirect; deep search completes through `get_job` across restart, with failed/cancelled statuses tested. Elicit-dependent tools require an eligible account; report plan blocking explicitly if unavailable |
| **P4 Rerank (optional)** | Reranker interface; backend choice (Infinity shared cluster, API) + benchmark | Benchmark recorded; decision documented |

### 17.1 Definition of done

**First usable target: P0.** Finish the walking skeleton before adding more capabilities.
**v1 target: P0–P3.** P4 reranking and §16 deferred items do not block either target.

A phase is done when:

- [ ] Its scope is implemented and its exit criteria in the table above pass.
- [ ] A fresh checkout starts using documented configuration and Docker Compose commands; the phase's
  provider accounts, routing, and client tokens can be managed through sqladmin.
- [ ] A researcher on another tailnet machine can discover and call the phase's tools with a valid token
  and receive the documented output with provenance.
- [ ] Focused tests exercise the phase's authentication, failover, partial-result, deduplication, and persistence
  behavior. Handle reads survive restart; cache rules and job recovery are checked when those features are added.
- [ ] The implementing PR records validation commands and outcomes, separating fixture tests from live
  client/provider checks. Unavailable agentRT, eligible-account, or OAuth checks remain explicitly pending;
  required pending criteria prevent claiming the phase or v1 complete.

Implement one phase at a time, reuse compatible upstream code, and add abstractions only for a demonstrated
requirement. Keep §16 additions tied to their stated triggers.

---

## 18. Credits (for README)

`R0Wi/mcp-gateway` @59c1efd (author permission), `vvzvlad/research-mcp` @11f297d (MIT, author permission),
`decolua/9router` @f01fb90 (MIT), `diegosouzapw/OmniRoute` @666ea59 (MIT), `spences10/mcp-omnisearch` (MIT),
`exa-labs/exa-mcp-server`, `firecrawl/firecrawl-mcp-server` (MIT), FastMCP (Apache-2.0), MCP Python SDK (MIT).
