# Research Engine — Design

Status: **draft v1 for owner review**. Date: 2026-09-29.
Inputs: [requirements-notes.md](requirements-notes.md), [research.md](research.md), `research/01…07`.
Audience: the owner and whoever implements the engine (human or agent). Section numbers are stable; later docs cite
them as `design §N`.

---

## 0. Summary

One self-hosted MCP endpoint (`https://<host>/mcp`) that routes **capability-typed research requests** to many
providers and accounts, runs them with deadlines and failover, and returns **compact, ranked, deduplicated evidence
with signals and provenance**. It never interprets evidence.

Key decisions (details in the referenced sections):

| # | Decision | § |
|---|---|---|
| D1 | **Python 3.12, FastMCP `==4.0.10`** (mcp `>=2.2,<3`), FastAPI outer app, single process, asyncio | 3 |
| D2 | **App shell = fork of `R0Wi/mcp-gateway`**; engine core seeded from `vvzvlad/research-mcp`; routing/resilience ported from 9router + OmniRoute | 2 |
| D3 | **One MCP tool per capability** (17) + `get_evidence`, `get_job`, and ChatGPT-compatible `search`/`fetch` — 21 tools, all enabled | 4 |
| D4 | Upstream MCP servers (Scite, Elicit, Undermind, Consensus) are **internal providers, never mounted**; capability → upstream tool mapping in config | 7.4 |
| D5 | Account model **provider → quota scope (org/team/plan) → account (credential)**; 3-scope health (provider breaker, scope/account state, account×capability lock) | 8 |
| D6 | Pipeline: normalize → canonicalize → dedup (4 tiers) → **hierarchical weighted RRF (k=60)** → optional rerank (off by default) → enrich top-k → compact response | 9 |
| D7 | **Deadline-bounded calls with job handoff**: default `max_wait_s=40`; overrun → partial results + `job_id`; `get_job` long-polls; jobs persisted | 10 |
| D8 | **Single SQLite file (WAL)**, SQLAlchemy 2 Core async (aiosqlite) behind repository interfaces; large payloads as blobs on disk; no Redis. Postgres is a driver swap | 12 |
| D9 | Admin SPA = **9router UI extracted to Vite + React**, cherry-picked OmniRoute views; Python admin API mirrors 9router paths | 14 |
| D10 | Runs on the existing host next to `knowledge-server-infra`; exposed through **its existing Cloudflare tunnel + Caddy** on a new hostname (`research.ducanh.cloud`); OAuth 2.1 AS from mcp-gateway plus RFC 9207 `iss` | 13, 18 |
| D11 | **No engine-enforced ToS restrictions**; ToS is the owner's call. All providers/accounts/strategies enabled; limits and budgets are configurable defaults | 19 |

Non-goals (from requirements): hypothesis generation, research prioritization, knowledge admission, campaign
opening, scientific decisions, final source acceptance. No LLM runs inside the engine (classification is rule-based;
LLM-answer provider modes such as Exa `/answer` are not used for evidence).

---

## 1. Architecture

```
 ChatGPT ─┐   claude.ai ─┐   Claude Code ─┐   agentRT ─┐
          └──────────────┴────────────────┴───────────┘
                         │  HTTPS (Cloudflare named tunnel)
                         ▼
┌──────────────────────── research-engine (one process) ─────────────────────────┐
│ FastAPI app                                                                    │
│  ├─ /mcp  (FastMCP 4, both protocol eras) ── auth: OAuth 2.1 AS | personal PAT │
│  ├─ /authorize /token /register /.well-known/*  (mcp-gateway AS)               │
│  ├─ /oauth/*   upstream OAuth connect + CIMD client doc                        │
│  ├─ /admin/api/*  admin REST (session cookie + CSRF)      /admin/  SPA         │
│  │                                                                             │
│  ├─ server/tools ─► engine.execute(CapabilityRequest)                          │
│  │     router: classify → chain → select accounts → dispatch (fan-out/seq)     │
│  │     accounts/health: states, breakers, locks, limiters, quota              │
│  │     providers: http | sdk | mcp_oauth | local adapters                      │
│  │     pipeline: normalize → canonicalize → dedup → fusion → rerank → enrich   │
│  │     jobs: deadline handoff, workers, upstream pollers                       │
│  │     cache: query / document / rerank / enrichment + request coalescing      │
│  └─ storage: SQLite (WAL) + blobs/   ── crypto: Fernet envelope (gateway)      │
└────────────────────────────────────────────────────────────────────────────────┘
                         │
     REST/SDK: Exa · Firecrawl · GitHub · Tavily · Brave · Serper · Jina Reader (keyless) · …
               OpenAlex · Crossref · Semantic Scholar · arXiv · Scite public REST
     MCP+OAuth: Scite · Elicit · Undermind · Consensus (Consensus via REST key preferred)
```

Boundary: the engine owns search / retrieve / verify / merge / rank. Consumers own interpretation and decisions.

---

## 2. Repository layout and provenance

Python package `research_engine`. Top-level folders follow the agreed repo boundary; `jobs`, `cache`, `admin`,
`config` are added.

```
research-engine/
├─ pyproject.toml            # exact pins; hatchling
├─ config.example.yaml
├─ docker-compose.yml        # engine only; joins knowledge-server-infra `web` network (§18)
├─ THIRD_PARTY_NOTICES.md    # MIT/Apache texts + per-file provenance list
├─ src/research_engine/
│  ├─ server/        app.py, mcp_server.py, tools/*.py, schemas.py, instructions.md, middleware.py, state.py
│  ├─ auth/          oauth_server.py, users.py, ratelimit.py, web.py, pat.py
│  ├─ providers/     spec.py, base.py, registry.py, transport/{http.py,url_guard.py,clients.py},
│  │                 classify.py, query_ops.py, web/*.py, scholar/*.py, dev/*.py,
│  │                 mcp_upstream/{client.py,oauth.py,token_store.py,adapter.py,scite.py,elicit.py,undermind.py,consensus.py}
│  ├─ router/        classify.py, chains.py, executors.py (fanout, sequential, composite), budget.py, trace.py
│  ├─ accounts/      models.py, registry.py, selection.py, connect_flow.py
│  ├─ health/        states.py, breaker.py, locks.py, limiter.py, quota.py, probes.py, classifier_rules/*.yaml
│  ├─ normalization/ models.py (ProviderHit, Evidence, …), parsers.py
│  ├─ canonicalization/ urls.py, ids.py, handles.py, versions.py, trackers.txt, hosts/*.py
│  ├─ dedup/         tiers.py, simhash.py, fuzzy.py
│  ├─ fusion/        rrf.py, groups.py
│  ├─ reranking/     base.py, local_st.py, fastembed.py, voyage.py, cohere.py, jina_api.py, doc_text.py
│  ├─ enrichment/    editorial.py, citations.py, oa.py, venue.py, dates.py, source_type.py, coverage.py
│  ├─ jobs/          models.py, runner.py, pollers/*.py
│  ├─ cache/         keys.py, coalesce.py, stores.py
│  ├─ storage/       db.py, crypto.py, blobs.py, repos/*.py, migrations/ (alembic)
│  ├─ admin/         api/*.py (FastAPI routers), sse.py
│  ├─ config.py
│  └─ cli.py
├─ ui-auth/          # Svelte login/consent from mcp-gateway (kept)
├─ ui-admin/         # Vite + React SPA extracted from 9router (+ OmniRoute pieces)
└─ tests/            # unit (respx), e2e two-server harness (from mcp-gateway), live smoke (opt-in)
```

Provenance map (what seeds each module; every copied/ported file gets an SPDX + origin header, and is listed in
`THIRD_PARTY_NOTICES.md` with source path and commit):

| Our module | Seeded from | Mode |
|---|---|---|
| `server/app.py`, `auth/*`, `storage/crypto.py`, `storage/migrations` 0001–0002, `config.py`, `cli.py`, `ui-auth/` | mcp-gateway @59c1efd | copy + modify |
| `providers/mcp_upstream/{client,oauth,token_store}.py`, `accounts/connect_flow.py` | mcp-gateway `upstream.py` | copy, re-key by account, fix lock (§13.3) |
| `providers/transport/*`, `providers/web/*`, `providers/classify.py` (fallback), `pdf`, read chain in `router/executors.py`, `reranking/jina_api.py` | research-mcp @11f297d | copy + modify |
| `health/breaker.py`, `health/states.py`, `health/locks.py`, `health/quota.py`, `router/chains.py`, fan-out quorum, `accounts/selection.py`, `cache/coalesce.py` | 9router @f01fb90 + OmniRoute @666ea59 | port TS/JS → Python (algorithms, not files) |
| `providers/query_ops.py`, error taxonomy `retryable` flag | mcp-omnisearch | port |
| `ui-admin/` | 9router UI + OmniRoute components | copy + shim |
| everything else (capability router, canonicalization, dedup, fusion, enrichment, jobs, evidence store) | — | new |

---

## 3. Runtime and framework

- **FastMCP 4.0.10 exactly**, `mcp>=2.2,<3`, `httpx` for our outbound HTTP (never mix with FastMCP's `httpx2`
  exception/auth types). Bump only with the full e2e suite green.
- One uvicorn worker, one event loop. All provider I/O is async. CPU-heavy steps (local rerank, SimHash, PDF parse)
  run in a bounded thread pool (`anyio.to_thread`, limiter = cores − 1).
- FastAPI outer app with FastMCP ASGI mounted as catch-all (as in mcp-gateway). `json_response` **off** (SSE keepalive
  every 15 s is what keeps Cloudflare from 524ing).
- FastMCP features used: server + tools with `outputSchema`, both eras, `OAuthProvider`/CIMD/private_key_jwt, logging,
  timing and error middleware, upstream `Client`. **Not used**: `create_proxy`/`mount`/tool transforms,
  `fastmcp-tasks` (never imported in the process, because its client extension auto-registers and would block on
  upstream tasks).
- Server `instructions` (first 512 chars carry the workflow): capabilities overview → "results carry `handle`s; pass
  them to `get_evidence`, `web_read`, `paper_read`" → "if `status` is `running`, call `get_job(job_id)` after
  `poll_after_s`".

---

## 4. MCP tool surface

### 4.1 Tools

All tools: `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: true`, `title`,
`outputSchema` + `structuredContent` + one compact text block. Names ≤ 64 chars, descriptions ≤ 2,000 chars with the
decision-relevant facts first ("Use this when… / not when…", sibling tools, limits). Hosts already namespace tools per
server (`mcp__research__web_search`), so names carry no prefix.

| Tool | Capability | Kind | Primary input | Notes |
|---|---|---|---|---|
| `web_search` | WEB_SEARCH | fan-out | `query`, `filters{domains_include/exclude, date_from/to, language, country}` | query operators (`site:`, `-site:`, `"…"`, `before:`/`after:`, `filetype:`) parsed and translated per provider |
| `news_search` | NEWS_SEARCH | fan-out | `query`, `recency` (`day|week|month|year`) or dates | |
| `web_read` | WEB_READ | sequential | `target` = URL or handle, `fresh`, `cursor`, `max_chars` | DOI/arXiv/PMID targets are rerouted to `paper_read` rules |
| `site_map` | SITE_MAP | sequential | `url`, `search`, `limit ≤ 500` | |
| `site_crawl` | SITE_CRAWL | job | `url`, `limit ≤ 100`, `max_depth ≤ 3`, `include/exclude_paths` | always async-capable |
| `site_interact` | SITE_INTERACT | single | `url`, `actions[]` or `prompt` | Firecrawl interact; annotated `readOnlyHint: false` because actions can change page state |
| `paper_search` | PAPER_SEARCH | fan-out | `query`, `filters{year_from/to, study_types, open_access, fields, venue, exclude_preprints}` | |
| `paper_read` | PAPER_READ | sequential | `target` (handle/DOI/arXiv/PMID/URL), `question?`, `cursor` | `question` enables QA-mode providers (Undermind `read_pdfs`); reports `fulltext` vs `abstract_only` |
| `paper_metadata` | PAPER_METADATA | resolver | `ids[]` or `citation` string (title/author/year) | batch ≤ 50 |
| `paper_related` | PAPER_RELATED | fan-out | `seeds[]` (handles/ids), `mode` (`similar|citing|cited`) | |
| `citation_verify` | CITATION_VERIFY | composite | `citation` (string or id) + optional `claim` text | returns existence, metadata match score, editorial, support tallies, statement snippets — **signals only** |
| `citation_graph` | CITATION_GRAPH | fan-out | `seeds[] ≤ 10`, `direction`, `depth ≤ 2`, `with_intent` | edges merged on canonical ids |
| `editorial_check` | EDITORIAL_CHECK | fan-out | `ids[] ≤ 50` | free sources only by default |
| `systematic_review` | SYSTEMATIC_REVIEW | job | `question`, `criteria[]`, `depth` (must be explicit) | Elicit only; PLAN_BLOCKED error if unavailable |
| `deep_literature_search` | DEEP_LITERATURE_SEARCH | job | `goal` | Undermind → Elicit report → engine-native wide job |
| `developer_search` | DEVELOPER_SEARCH | fan-out | `query`, `repos[]`, `language`, `types[]` | |
| `repo_search` | REPO_SEARCH | fan-out | `query`, `language`, `min_stars`, `topic` | `mode: code` sub-mode → GitHub code search |
| `get_evidence` | — | local | `handles[] ≤ 20`, `expand[]` (`provenance|signals|passages|versions|abstract|raw_ids`) | cache/store only; never calls providers except lazy enrichment |
| `get_job` | — | local | `job_id`, `max_wait_s ≤ 40` | long-poll; returns complete or updated partial |
| `search` | compat | fan-out | `query` | ChatGPT/OpenAI deep-research schema: `{results:[{id,title,url}]}` |
| `fetch` | compat | read | `id` (handle) | `{id,title,text,url,metadata}`; `text` ≤ 25k chars; `metadata` = signals, provenance, `next_cursor` |

21 tools in total, all visible. Profiles (metamcp-style tool filter enforced on list
**and** call): `/mcp` = full; `/mcp/compat` = `search`, `fetch` only (own PRM document, for OpenAI API deep research).

### 4.2 Common parameters

| Param | Default | Meaning |
|---|---|---|
| `limit` | 8 | results returned (max 25). Internally over-fetched per provider (window W=20). |
| `depth` | `standard` | `standard` = all healthy providers in the chain whose `use` is `always` or `budgeted` (within budget); `thorough` = also `thorough_only` modes (Exa deep, Tavily advanced, extra query variants, premium providers regardless of reserve) |
| `max_wait_s` | 40 | clamp 5–50; deadline for the synchronous part (§10) |
| `providers` | — | optional `{include:[…], exclude:[…]}` advanced override; unknown names → validation error |
| `fresh` | false | read tools: bypass caches (`maxAge=0`, `max_age_hours=0`, Jina `X-No-Cache`) |
| `cursor` | — | read tools: continue a paginated document |

### 4.3 Output envelope (search-like tools)

```json
{
  "status": "complete | partial | running",
  "items": [ { "handle": "doi:10.1038/s41586-020-2286-9", "kind": "scholarly_work",
               "title": "…", "year": 2020, "venue": "Nature", "url": "https://doi.org/…",
               "snippet": "…≤300 chars…",
               "alt": [{"handle": "doi:10.1101/2020.03.22.002386", "rel": "has-preprint"}],
               "signals": {"agreement": {"providers": 4, "eligible": 6}, "stage": "published",
                           "editorial": "none_found", "oa": "green", "cited_by": {"openalex": 4100}, "age_days": 2300},
               "prov": ["openalex#1", "s2#2", "scite#1", "exa#7"] } ],
  "coverage": { "attempted": 7, "ok": 6, "failed": [{"p": "undermind", "err": "TIMEOUT"}],
                "skipped": [{"p": "scite", "why": "EXHAUSTED until 2026-10-01"}], "gaps": ["…"] },
  "job": { "job_id": "j_…", "poll_after_s": 15, "progress": {"done": 5, "total": 7} },
  "request_id": "r_…"
}
```

- Item target ≤ ~120 tokens; default response ≤ ~8k tokens. Text block = compact numbered rendering (title — venue
  year — handle — 1-line snippet — signal badges) instead of duplicating the JSON (ChatGPT truncation workaround; verify
  in P0).
- Read tools return `{handle, title, url, text, total_chars, next_cursor, source, fulltext: bool, signals, prov}` with
  page ≈ 20k chars; set `_meta["anthropic/maxResultSizeChars"]=100000` on read tools.
- `coverage.gaps` are rule-generated facts ("no biomedical provider queried"), never advice.

### 4.4 Errors

Validation and capability errors are **tool errors** (`isError: true`) with structured content:
`{error: {code, message, retryable, next_actions: [{kind: "retry_later", after_seconds} | {kind: "use_tool", tool} | {kind: "enable_provider"}]}}`.
Codes: `INVALID_INPUT`, `NO_PROVIDER_AVAILABLE` (with per-provider reasons), `PLAN_BLOCKED`, `ALL_RATE_LIMITED`
(with earliest retry), `NOT_FOUND`, `DISABLED_CAPABILITY`, `INTERNAL`. Upstream error text is never forwarded verbatim
(`mask_error_details=True`); it is recorded in the request log.

---

## 5. Request lifecycle

```
tool call ─► validate (pydantic) ─► CapabilityRequest{capability, params, depth, deadline, subject}
  ─► cache lookup (query key)  ── hit (fresh) ─► respond
  ─► coalesce (same key in flight → await it)
  ─► router.plan: chain for capability (+ rule reroutes) ─► candidate steps
  ─► for each step: provider breaker? account selection (state, locks, limiter, quota, budget) ─► attempt
  ─► executor: fan-out (quorum + grace + hard deadline) | sequential (cost-gated) | composite
       each attempt: translate query → adapter.execute → classify outcome → update health/quota/usage
  ─► normalize hits ─► canonicalize ─► dedup T1/T2/T4 ─► RRF ─► [rerank top-40] ─► T3 on fetched text
  ─► enrich top-k (batched, cached) ─► version collapse ─► handle upgrade/aliases
  ─► persist: request, attempts, decision trace, hits, evidence ─► cache store ─► respond
  └─ deadline hit at any point ─► persist as job, respond `partial` + job_id; work continues in background
```

Rule-based classification/reroutes (no LLM), applied in `router/classify.py`:
- `search` (compat): DOI/arXiv/PMID in query → `paper_metadata`; code-ish (`stacktrace`, `error:`, backticks,
  `repo:`) → `developer_search` + `web_search`; otherwise `web_search` ∪ `paper_search` fused with capability weights
  (web 1.0 / scholarly 1.0) — the only multi-capability fusion.
- `web_read` target is a DOI/arXiv/PMID URL → `paper_read` chain.
- `paper_search` query with study-type words (RCT, meta-analysis, cohort …) → mark Consensus step `use: always`.
- Any tool: explicit `providers.include` restricts candidates (still health-checked).

---

## 6. Capability routing policy

### 6.1 Chains

A **chain** (9router/OmniRoute "combo") is per capability, stored in DB, seeded from YAML, editable in the admin
policy editor, versioned (`policy_version` is part of cache keys and request logs).

```yaml
chains:
  paper_search:
    mode: fanout                      # fanout | sequential | composite
    fanout: {min_quorum: 2, grace_ms: 8000, hard_ms: 35000}
    steps:
      - {provider: openalex,  use: always,   weight: 0.7, group: openalex}
      - {provider: s2,        use: always,   weight: 1.0, group: s2}
      - {provider: undermind, use: always,   weight: 1.0, group: undermind, op: search_papers}
      - {provider: arxiv,     use: when_hint, hint: cs_physics, weight: 0.6, group: arxiv}
      - {provider: consensus, use: budgeted, weight: 1.0, group: consensus, reserve_pct: 30}
      - {provider: scite,     use: budgeted, weight: 1.0, group: scite, reserve_pct: 40}
      - {provider: elicit,    use: budgeted, weight: 1.0, group: elicit}
      - {provider: exa,       use: thorough_only, weight: 0.5, group: exa, params: {category: publication}}
    validation: {min_results: 1}
  web_read:
    mode: sequential
    steps: [cache, firecrawl, exa_contents, jina_keyless, tavily_extract, trafilatura]
    success: {min_chars: 400}          # best-thin fallback as in research-mcp
```

Step fields: `provider` | `chain` (nested), `account` / `allowed_accounts` (plan-gated pinning), `use`
(`always|budgeted|thorough_only|when_hint|explicit_only|fallback_only_on_exhaustion`), `weight`, `group`
(independence group for fusion), `op` + `params` (provider operation and fixed params), `timeout_ms`.
Chain config: `chain_timeout_ms`, `target_timeout_ms`, `cooldown_wait{max_wait_ms, max_attempts}` (wait out short
`retry-after` only for RATE_LIMITED, never EXHAUSTED/AUTH), `validation`, `fanout{min_quorum, grace_ms, hard_ms}`.

Dry-run: `POST /admin/api/combos/:id/simulate` returns the ordered candidates with skip reasons (OmniRoute
`explain_route`) — the same code path the router uses, minus dispatch.

### 6.2 Default routing (v1)

| Capability | Mode | Default fan-out / chain | Fallback / notes |
|---|---|---|---|
| WEB_SEARCH | fan-out | Exa (auto, highlights) + Brave and/or Serper + Tavily (basic) | Firecrawl search (Perplexity/Parallel/Linkup: later, not provisioned in v1) |
| NEWS_SEARCH | fan-out | Brave news and/or Serper news + Tavily (news) | Exa (category=news) → Firecrawl (sources=news) |
| WEB_READ | sequential | cache → Firecrawl scrape → Exa contents → Jina Reader (keyless, 20 RPM) → Tavily extract → trafilatura (local) | URL-level failures do not penalize the account; PDF path from research-mcp. No Bright Data / crawl4ai in v1 |
| SITE_MAP | sequential | Firecrawl map (limit 500) → Tavily map → sitemap.xml parse | |
| SITE_CRAWL | job | Firecrawl crawl (limit ≤ 100, depth 2, dedup similar) → Tavily crawl | |
| SITE_INTERACT | single | Firecrawl interact | off by default |
| PAPER_SEARCH | fan-out | OpenAlex + S2 + Undermind search; arXiv on CS/physics hint; Consensus/Scite budgeted; Elicit when enabled | results resolved to DOIs via PAPER_METADATA |
| PAPER_METADATA | resolver | OpenAlex singleton → Crossref → S2 batch → arXiv id_list; title-only: OpenAlex search + Crossref `query.bibliographic` → S2 match | id crosswalk cached 30 d |
| PAPER_READ | sequential | arXiv HTML/PDF → OpenAlex best OA PDF (engine PDF pipeline) → Scite `read_fulltext` → S2 snippets; `question` → Undermind `read_pdfs` | never serves paywalled text |
| PAPER_RELATED | fan-out | S2 recommendations + OpenAlex `related_works` + Undermind citations/references | |
| CITATION_GRAPH | fan-out | OpenAlex + S2 (intents, influential); Scite `citation_graph` budgeted when `with_intent` | |
| CITATION_VERIFY | composite | ① existence + metadata match: Crossref + OpenAlex ② tallies: Scite public REST ③ statements: Scite MCP (budgeted) | signals only |
| EDITORIAL_CHECK | fan-out | Crossref `updated-by` + OpenAlex `is_retracted` + Scite public `/papers` | all free; also auto-enrichment on top-k |
| DEEP_LITERATURE_SEARCH | job | Undermind `launch_deep_search` (dedicated workspace, ≤ 1–2 concurrent) → Elicit `create_report` (if enabled) → engine-native wide job | engine-native = multi-variant PAPER_SEARCH + one citation-expansion round |
| SYSTEMATIC_REVIEW | job | Elicit only | PLAN_BLOCKED error when unavailable |
| DEVELOPER_SEARCH | fan-out | Firecrawl developer search + GitHub issues (hybrid) + Exa (fast, docs domains) | Serper `site:stackoverflow.com` → Brave |
| REPO_SEARCH | fan-out | GitHub repositories (+ GraphQL for README/topics) + Firecrawl developer (readme) | `mode: code` → GitHub code search (10/min limiter) |

### 6.3 Budgets

- Each provider declares a `CostModel` (unit `usd|credits|calls|free`, per call/result, shared `pool`).
- Per-call budget from policy per capability and `depth` (e.g. `standard` web search ≤ $0.03; `thorough` ≤ $0.10).
  Steps are admitted in chain order while the estimated cost fits.
- **Premium quota providers** (hard monthly caps: Scite, Consensus, Undermind deep, Elicit) are `budgeted`: admitted
  only while `remaining_pct > reserve_pct` for `standard` depth; the reserve is for `thorough` or explicit
  `providers.include`. Monthly usage and reset dates come from §8.5.
- Daily/monthly spend guard per provider (OmniRoute `costRules`): warn at 80 %, block at 100 % (admin-configurable).

---

## 7. Providers

### 7.1 Interface

Generalizes research-mcp's two protocols into one capability-based interface (research/01 §4).

```python
class Capability(StrEnum): WEB_SEARCH, WEB_READ, NEWS_SEARCH, SITE_MAP, SITE_CRAWL, SITE_INTERACT, PAPER_SEARCH,
    PAPER_READ, PAPER_METADATA, PAPER_RELATED, CITATION_VERIFY, CITATION_GRAPH, EDITORIAL_CHECK,
    SYSTEMATIC_REVIEW, DEEP_LITERATURE_SEARCH, DEVELOPER_SEARCH, REPO_SEARCH

@dataclass(frozen=True)
class ProviderSpec:
    type: str                                   # registry key
    ops: dict[Capability, OpSpec]               # capability → operation(s) this provider implements
    transport: Literal["http", "sdk", "mcp_oauth", "local"]
    auth: Literal["none", "api_key", "oauth", "bearer"]
    options_model: type[BaseModel]              # typed options (no overloaded slots)
    cost: CostModel
    limits: list[RateLimit]                     # per resource: rps/rpm/min_interval, on_busy skip|wait
    quota_scope: Literal["account", "team"]     # Exa/Firecrawl = team
    supports: frozenset[str]                    # paging, language, date_filter, domain_filter, prefetched_body, pdf …
    classifier: str                             # rules file in health/classifier_rules/
    persist_hits: bool = True                   # per-provider switch to keep hits only in the TTL cache
    independence_group: str | None = None

class Provider(Protocol):
    spec: ClassVar[ProviderSpec]
    async def execute(self, ctx: CallContext, req: CapabilityRequest) -> ProviderResult: ...
    def classify(self, outcome: RawOutcome) -> Classification | None: ...   # provider-specific rules first
    async def probe(self, ctx: CallContext) -> HealthProbe | None: ...      # cheap, no-quota check if available
    async def quota(self, ctx: CallContext) -> QuotaSnapshot | None: ...    # usage API if available
```

`CallContext` = http client (proxy/guarded), account handle (secrets resolved, never logged), deadline, remaining
budget, hints (`is_pdf`, `prefetched_html`). `ProviderResult` = `hits: list[RawHit]` (rank preserved, **all
upstream fields kept**: score, dates, authors, ids) or `document`, plus `cost_actual`, `quota_hint` (parsed headers).
Errors are typed `ProviderFailure(reason, scope: account|scope|request|target, retry_after, status)`; research-mcp's
text classifier stays only as fallback for foreign exceptions.

### 7.2 Transports

- **http**: research-mcp `_http.request_with_retry` extended: typed errors, `Retry-After`/`x-ratelimit-*` parsing,
  401 vs 403 split, jittered exponential backoff for transient errors, never retry 402/429 (fail over instead),
  response attached for classifiers. `ClientManager` per (proxy, guarded).
- **sdk**: only where the SDK adds value; default is raw HTTP for uniform error handling. Firecrawl: raw POST (the SDK
  strips `creditsUsed`); Tavily: raw HTTP (SDK maps 429/432/433 to misleading exceptions); Exa: raw HTTP or `exa-py`
  with explicit `contents`.
- **mcp_oauth**: §7.4.
- **local**: trafilatura, pypdf, sitemap parser. Reranking is an HTTP call to the shared Infinity service (§9.5).

### 7.3 Provider set for v1

| Group | Providers (v1) | Later |
|---|---|---|
| Web/news | Exa, Firecrawl, Tavily, Brave and/or Serper, Jina Reader (keyless), trafilatura | Perplexity Search, Parallel, Linkup, You.com, SearXNG, crawl4ai, Bright Data, DuckDuckGo scrape (adapters exist in research-mcp; not enabled in v1) |
| Academic open | OpenAlex, Crossref, Semantic Scholar, arXiv, Scite public REST, bioRxiv `/pubs` (version links), NCBI ID converter | Europe PMC, Unpaywall direct |
| Academic hosted | Undermind (MCP), Consensus (REST key preferred, MCP fallback), Scite (MCP), Elicit (enabled; PLAN_BLOCKED until Pro; REST key preferred once Pro) | Lune (PAT), alphaXiv (API key) |
| Developer | GitHub REST/GraphQL, Firecrawl developer search, Exa | — |
| Rerank | Jina API (existing), local cross-encoder, Voyage, Cohere | — |

Adapter refresh required before trusting research-mcp adapters: Exa (2026 changes), Tavily (432/433), Serper (400
credit body), Jina keyless limits. Unused adapters are ported later, not in v1.

### 7.4 Hosted MCP upstreams

- One FastMCP `Client` per **account**, `NoForwardStreamableHttpTransport` (no token passthrough; startup assertion),
  `mode="auto"` era negotiation, kept open for app lifetime.
- OAuth: CIMD (our `/oauth/client-metadata.json`, required for Undermind) → DCR fallback (Scite, Elicit, Consensus);
  tokens per account encrypted; proactive refresh; per-account refresh mutex; **lock-scope fix** (§13.3).
- `adapter.py` maps `capability → upstream tool + arg translation + result parser`. Default mapping (extendable in config):
  - Scite: `search_literature`, `read_fulltext`, `citation_graph` (never collections/notes/`report_citations`).
  - Undermind: `get_orientation`, `list_workspaces`, `create_workspace` (once), `search_papers`, `get_paper_info`,
    `lookup_papers_by_metadata`, `launch_deep_search`, `inspect_deep_searches`, `read_pdfs`, `find_papers_by_author`.
  - Consensus: `search` (REST `/v1/search` preferred when an API key exists).
  - Elicit: `search_papers`, `search_trials`, `create_report`/`get_report`, `create_systematic_review`/
    `get_systematic_review`, `get_usage`.
- Parsers: Consensus and Undermind Markdown → regex parsers with fixture tests; Scite/Elicit JSON-ish text.
  Quota errors may be `isError` results, so classifiers inspect both JSON-RPC errors and tool-result text.
- Undermind specifics: dedicated workspace `research-engine`; `sample_abstract` templated from the query (no LLM);
  cite-key → DOI via one batched `get_paper_info` (cached per workspace).
- Schema drift: hash of upstream `tools/list` per account stored in health; change → DEGRADED + admin alert.

### 7.5 Query translation

Port mcp-omnisearch's operator parser: `site:`, `-site:`, `filetype:`, `intitle:`, `inurl:`, `before:`, `after:`,
`"exact"`, `lang:`, `+/-term`. Each adapter declares how operators map (native pass-through for Brave/Serper/Firecrawl;
Tavily/Exa → request params; GitHub qualifiers; academic providers → year filters and quoted titles).

---

## 8. Accounts, quota scopes and health

### 8.1 Data model

```
provider (type, enabled, options, policy)                     e.g. exa
 └─ quota_scope (org/team/plan; limits; reset rule)           e.g. exa:team-main  plan=payg
     └─ account (credential; label; priority; weight; enabled; plan; max_concurrent)   e.g. exa:key-research
```

- Team-wide limits (Exa, Firecrawl): a RATE_LIMITED/EXHAUSTED(team) outcome marks the **scope**, cooling all its keys.
  Key budget exhaustion (Exa `API_KEY_BUDGET_EXCEEDED`) marks only the account.
- Per-account for OAuth upstreams; "connect another account" warns that the browser must log in as a different
  upstream identity; a duplicate upstream identity (e.g. same Undermind `get_orientation` email) is shown as a warning.

### 8.2 States

| State | Scope | Enter on | Exit |
|---|---|---|---|
| READY | any | success; lazy expiry of timed states | — |
| COOLING | account / provider | 408, 5xx, transport error (30 s × backoff); breaker OPEN | lazy expiry; breaker HALF_OPEN probe |
| RATE_LIMITED | account / scope / account×cap | 429 or rate-limit wording; `Retry-After`/reset header | `until` from hint, else `2 s·2^level` capped 5 min (reset hints capped 30 min for short limits) |
| EXHAUSTED | account / scope | credit/quota signals (per-provider rules) | parsed reset time → quota window end (billing period, 1st of month UTC, midnight UTC) → quota refresh showing remaining > 0 → manual |
| AUTH_REQUIRED | account | 401 after refresh attempt fails; invalid key | manual reconnect / new key |
| PLAN_BLOCKED | account×capability (or account) | 403 feature/plan, `api_access_denied`, `feature_not_allowed` | TTL (24 h) re-probe, or admin "plan changed" |
| DEGRADED | provider | breaker degraded threshold; schema drift | failures fall below threshold |
| DISABLED | account / provider | admin toggle; misconfiguration; deactivated/banned signal; repeated refusals | manual only |

Precedence rules (from both routers): text/tag signals beat status codes; terminal states (DISABLED, AUTH_REQUIRED,
EXHAUSTED before reset) are never overwritten by transient ones; a success clears only the scope that succeeded;
only 408/5xx/transport errors feed the provider breaker; request-scoped 4xx (bad params) change no state and are not
retried on other accounts; target-scoped failures (URL 403, bot protection, "website not supported") change no state.
Anti-thundering-herd: concurrent failures on one account increment the backoff level once.

**Effective state** for (account, capability) = max severity of provider breaker, scope state, account state,
account×capability lock. Severity: DISABLED > AUTH_REQUIRED > PLAN_BLOCKED > EXHAUSTED > RATE_LIMITED > COOLING >
DEGRADED > READY. DEGRADED remains routable but is ordered last.

### 8.3 Provider circuit breaker

OmniRoute 4-state breaker: CLOSED → DEGRADED → OPEN → HALF_OPEN; thresholds by profile (OAuth 5/8 failures, reset
60 s; API key 7/12, 30 s; local 2, 15 s); lazy recovery on read; one half-open probe slot; reset timeout doubles per
open cycle after 3 cycles, cap 16×. Persisted (restart-safe).

### 8.4 Classifier rules

Per-provider YAML rules evaluated before generic status rules (OmniRoute "status restatement" + research-mcp credit
markers). Example:

```yaml
# health/classifier_rules/tavily.yaml
- {status: [432, 433], state: EXHAUSTED, scope: account, reset: monthly_1st_utc}
- {status: [429], state: RATE_LIMITED, retry_after: header}
# perplexity.yaml — 401 is ambiguous
- {status: [401], state: EXHAUSTED_OR_AUTH, cooldown: 1h, alert: true}
# scite.yaml (MCP tool-error text)
- {text: "monthly MCP usage limit", state: EXHAUSTED, reset: parse_date(r"resets on (\d{4}-\d{2}-\d{2})") | monthly_1st_utc}
- {jsonrpc_code: -32001, state: EXHAUSTED, reset: monthly_1st_utc}
# exa.yaml
- {status: [402], tag: API_KEY_BUDGET_EXCEEDED, state: EXHAUSTED, scope: account}
- {status: [402], tag: [NO_MORE_CREDITS, TEAM_BUDGET_EXCEEDED], state: EXHAUSTED, scope: scope}
- {status: [403], tag: FEATURE_DISABLED, state: PLAN_BLOCKED, scope: account_capability}
```

Full per-provider tables: research/04 (academic), research/05 §9 (web/dev). Retry-hint parsing ports OmniRoute's
parsers (`retryDelay`, "retry after 20s", ISO timestamps, "resets on Oct 3").

### 8.5 Rate limiters and quota

- **Limiters** per (account or scope, resource): token bucket / min-interval with `on_busy: skip | wait(max_ms)`.
  Seeds: arXiv 1 req/3 s global single-connection (wait), S2 1 rps (wait), Crossref from `x-rate-limit-*` headers
  (dynamic), GitHub search 30/min, code 10/min, issue-semantic 10/min, Jina keyless 20 RPM (skip), Brave free
  1 rps. State persisted so a restart does not walk back into a block.
- **Quota snapshots** `{scope/account, windows[{name, unit, limit, used, remaining_pct, reset_at, source}], fetched_at}`
  from (1) usage APIs (Firecrawl credit-usage, Tavily usage, Elicit `get_usage`) polled with TTL,
  fail-open; (2) headers (OpenAlex usd, GitHub, Brave monthly); (3) local counters vs configured plan limits (Scite
  25/month, Consensus 30/month, …). Preflight: block at ≤ 2 % remaining, warn at 20 %. Snapshots stored as time series
  for charts. A recent success overrides a stale "exhausted" snapshot.
- **Health probes** (no-quota where possible): Scite `/mcp/health`, Elicit `get_usage`, Undermind `get_orientation`,
  OpenAlex singleton headers, Firecrawl credit-usage. Scheduler interval per account (default 60 min, backoff on
  failure 5→10→30→120 min).

### 8.6 Account selection

Filter: enabled, effective state routable, limiter admits, quota preflight passes, budget fits. Strategies per
provider: `fill-first` (default; priority order = failover), `sticky-round-robin`, `p2c` (health score), `reset-aware`
(spend credits that expire soonest; OmniRoute formula), `least-used`, `weighted`. Any strategy can be set per provider
and per chain step; any number of accounts per provider. Selection guarded by a per-provider
`asyncio.Lock`; per-account concurrency cap (`max_concurrent`).

---

## 9. Evidence pipeline

Detailed algorithms: research/07. This section fixes the defaults.

### 9.1 Models

- `ProviderHit` — immutable, one per upstream result (provider, account ref, capability, op, query variant, rank,
  list_len, raw score, raw ids/url/title/snippet/dates, latency, cost, payload blob ref).
- `Evidence` — canonical cluster: `handle`, `handle_aliases`, `kind` (`scholarly_work|web_page|news_article|code_repo|
  code_item|issue|dataset|other`), merged fields each with `source`, `ids`, `urls`, `versions`, `related`
  (`is-preprint-of`, `has-preprint`, `retracted-by`, `possible_duplicate_of`, `syndicated-from` …), `provenance`,
  `passages`, `signals`, `merge_log`. Fully recomputable from hits + enrichment records.

### 9.2 Canonicalization and handles

- Own `canonicalization/ids.py` (DOI lowercase + publisher-URL patterns, arXiv versionless + versions, PMID, PMCID,
  OpenAlex, S2, OpenReview/ACL/SSRN/Zenodo/bioRxiv) with table-driven tests; `idutils` optional for long-tail validation.
- `urls.py`: redirector unwrap → `url-normalize` → tracker removal (list seeded from courlan) → fragment drop (except
  `#!`) → sorted query → key-only rules (https, `www.`/`m.`/`amp.`, trailing slash, index files) → AMP → host
  plugins (YouTube, GitHub, Reddit, X, StackOverflow, Wikipedia, Medium).
- Handles: `doi:` > `arxiv:` > `pmid:` > `pmcid:` > `openalex:` > `s2:` > `isbn:` > `gh:` > `url:<b32(sha256)[:20]>`;
  DataCite arXiv DOIs → `arxiv:`; aliases permanent; `get_evidence(old)` resolves transparently.
- Version links from Crossref relations, bioRxiv `/pubs`, S2 externalIds, arXiv `arxiv:doi`, OpenAlex locations;
  preprint and published are separate evidence, **collapsed in responses** (published primary, preprint in `alt`).

### 9.3 Dedup

T1 strong-ID union-find with cannot-link (two DOIs never merge; S2 `{ArXiv, DOI}` = version link) → T2 canonical URL
→ T4 fuzzy scholarly (RapidFuzz `token_sort_ratio ≥ 95`, year ±1, first author; 90–95 → `possible_duplicate_of`)
→ T3 near-dup text on fetched top-N only (SimHash 64-bit Hamming ≤ 3; MinHash-LSH J ≥ 0.8 for short text;
cross-domain → `syndicated-from`, counted once for agreement).

### 9.4 Fusion

Hierarchical weighted RRF, `k = 60`, window `W = 20`: inner per provider = best rank across its lists (variants,
accounts); outer across **independence groups** = max within group, sum across groups, times `(capability, provider)`
weights from the chain. Deterministic tie-break: best single rank → #groups → provider priority → handle. Provider raw
scores are kept in provenance, not used. `k` and weights are policy (admin-editable), tuned later offline with `ranx`.

### 9.5 Rerank (optional, off by default)

`Reranker` protocol; implementations: Noop, **Infinity HTTP** (default local backend), Jina API (existing adapter),
Voyage, Cohere. **Local backend = the existing Infinity cluster of `knowledge-server-infra`** (`michaelf34/infinity`
CPU, behind `embedding-lb`), extended with a rerank model and called over its `/rerank` endpoint on the shared Docker
network — owner decision; no torch/ONNX inside the engine process. Host: 56-core Xeon E5-2680 v4, 125 GB RAM,
**no GPU**, so candidates are CPU-friendly: `bge-reranker-v2-m3` (multilingual) or `gte-reranker-modernbert-base`
(EN), picked by the P0 benchmark. Adding the model is a change to `knowledge-server-infra` (its own review process)
and must not degrade its embedding latency: the engine sends rerank traffic with its own concurrency cap and the
reranker has an 8 s budget.
Candidates top-40; doc text per kind (paper: title + venue/year + abstract; web: title + best passage, MaxP when
fetched). Final order = second-stage RRF(fused ×1, reranked ×2). On timeout → fused order +
`coverage.rerank="skipped:timeout"`. Infinity ↔ API failover through the account router. Cache
`(reranker_id, sha1(query), sha1(text)) → score`, 30 d. Jina **weights** (CC-BY-NC) are not used locally.

### 9.6 Enrichment and signals

Runs on returned top-k (+ `get_evidence` expands), batched (OpenAlex `filter=doi:a|b|c`, S2 `/paper/batch`, Crossref
per DOI), cached per handle (editorial 7 d, citations 7 d, OA 30 d, venue 90 d, web dates 30 d). Signals and sources
per research/07 §7: `agreement`, `editorial`, `citations`, `scite`, `venue`, `stage`, `oa`, `dates`, `source_type`/
`domain`, `freshness`, `code`, `versions`, plus call-level `coverage`. Every signal carries `source`, `as_of`; unknown
≠ negative. **Only agreement affects ranking** (through RRF). Consumers may pass explicit policies
(`recency_weight`, `demote_retracted`) — off by default and recorded in the response.

---

## 10. Jobs and long-running work

- Every request runs as a task with a deadline (`max_wait_s`, default 40, clamp 5–50). If the deadline passes, the task
  is persisted as a **job** and the tool returns `status: "partial"` with results fused so far plus
  `{job_id, poll_after_s, progress}`. Inherently async capabilities (deep search, systematic review, crawl) always
  create a job and return early partials if any.
- **Job ID is idempotent**: `hash(subject, tool, normalized args, policy_version, cache_epoch)`. A retried call
  (ChatGPT re-calls after timeouts) attaches to the same job.
- `get_job(job_id, max_wait_s)` long-polls up to 40 s and returns the complete or updated partial result.
  No `cancel_job` tool (would be a write action); jobs expire by TTL (default 72 h) and admin can cancel.
- Workers: in-process asyncio runner with per-kind concurrency (Undermind deep ≤ 1–2, Elicit ≤ 1, crawl ≤ 2).
  Upstream pollers store upstream refs (Undermind workspace + search name, Elicit session id, Firecrawl crawl id) and
  poll at provider cadence (Undermind 15–30 s, Elicit 30–60 s with backoff, Firecrawl 2–5 s). On startup, `running`
  jobs are resumed from stored refs; jobs with no upstream ref are re-queued.
- Queue = `jobs` table (status, lease owner, lease expiry). On Postgres the same table uses `FOR UPDATE SKIP LOCKED`.
- Progress notifications are emitted when the client sent a `progressToken` (Claude Code renders them); never relied
  on to extend timeouts. The tasks extension (SEP-2663) can later be exposed over the same job table for agentRT.

---

## 11. Caching and session features

| Cache | Key | TTL (default) | Notes |
|---|---|---|---|
| Query | capability + normalized query (NFKC, casefold, whitespace) + normalized params + depth + `policy_version` | web 6 h, news 30 min, paper search 7 d, related/graph 7 d, developer/repo 1 d | stores fused result (handles + ranks + coverage) |
| Document | canonical handle/URL + read params | 7 d (read), honors `fresh` | text in blobs; paginated by cursor |
| Enrichment | handle + signal type | per §9.6 | |
| Rerank | reranker id + query hash + text hash | 30 d | |
| Id crosswalk | any id → canonical ids | 30 d | |

- **Request coalescing**: an in-flight map per query key; concurrent identical calls await one execution (OmniRoute
  `searchCache`). Stragglers that finish after a response still write to caches.
- **Evidence handles** are stable across calls and sessions (subject-scoped visibility is unnecessary for a single user
  but the `subject` column is kept).
- No cross-call "seen" suppression (explicit requirement).
- Providers flagged `persist_hits: false` keep hits only in the TTL query cache (none in v1).

---

## 12. Storage

- **One SQLite file** `data/engine.db`, `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`,
  `foreign_keys=ON`. Engine repositories use **SQLAlchemy 2.0 Core async + aiosqlite**; mcp-gateway's auth storage keeps
  its sync `sqlite3` access initially (low volume) on the same file and is migrated to the async repos later. One
  alembic history for everything (gateway 0001–0002 kept, engine from 0003).
- **Blobs** (raw payloads, documents, PDFs text) in `data/blobs/sha256/ab/cd…` with TTL GC; DB rows hold refs.
- **Scale path**: every table sits behind a repository `Protocol`; switching to Postgres = asyncpg driver + alembic
  run; the jobs queue already uses a lease pattern. No Redis in v1 (single process; in-memory coalescing/limiters are
  snapshotted to DB).
- Secrets: mcp-gateway Fernet envelope (KEK from `ENGINE_ENCRYPTION_KEY[_FILE]`, per-DB DEK, `rotate-key`).
  Encrypted: OAuth client records, upstream tokens, account credentials, session secret.
- Backups: `cli backup` (SQLite online backup API + blobs tar); admin "export config" (no secrets) / import.

Tables (migration 0003+):

| Area | Tables |
|---|---|
| Auth (gateway) | `meta`, `oauth_clients`, `auth_codes`, `access_tokens`, `refresh_tokens`, `auth_txns`, `revoked_sessions`, + `personal_tokens` |
| Providers/accounts | `providers`, `quota_scopes`, `accounts`, `account_credentials` (enc), `upstream_schema_hash` |
| Health | `health_state` (scope_kind, scope_id, capability?, state, until, reason, backoff_level, failure_count, updated_at), `provider_breakers`, `limiter_state`, `quota_snapshots` (time series) |
| Policy | `chains` (versioned), `settings` (kv), `policy_versions` |
| Requests | `requests` (call-level: tool, args, subject, status, timings, coverage, policy_version, replay_of), `attempts` (provider, account, op, outcome, reason, status, latency, cost, blob refs), `decision_traces` |
| Evidence | `provider_hits`, `evidence`, `evidence_fields`, `handle_aliases`, `evidence_links`, `id_crosswalk` |
| Cache | `query_cache`, `doc_cache`, `enrichment_cache`, `rerank_cache` |
| Jobs | `jobs`, `job_events` |
| Usage | `usage_events`, `usage_daily` |

Retention: requests/attempts 90 d, payload blobs 14 d, hits of TTL-only providers 24 h, evidence indefinitely.

---

## 13. Auth

### 13.1 Client-facing (from mcp-gateway)

OAuth 2.1 AS: PKCE S256, DCR (gated behind owner login), CIMD (+ `private_key_jwt`), RFC 8414/9728/8707, hashed
rotating tokens, single local identity (bcrypt). Additions:
- **RFC 9207**: `iss` on every authorization response + `authorization_response_iss_parameter_supported: true`.
- `offline_access` in `scopes_supported`; `token_endpoint_auth_methods_supported: ["none", "private_key_jwt"]`.
- Redirect allowlist: `https://chatgpt.com/connector_platform_oauth_redirect`, `https://chatgpt.com/connector/oauth/*`,
  `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, loopback
  `http://localhost:*/callback`, `http://127.0.0.1:*/callback` (port-agnostic).
- PRM per profile path (`/mcp`, `/mcp/compat`), exactly one `authorization_servers` entry, `resource` = exact URL.
- **Personal access tokens** (new, `auth/pat.py`) for agentRT and optionally Claude Code static headers: created in
  admin, hashed, scoped to profiles, revocable.

### 13.2 Upstream

Per account (§7.4). Connect flow = mcp-gateway's (`/oauth/connect/{account_id}` → upstream AS → `/oauth/callback`
exact-state routing). MCP traffic never starts an interactive flow; missing/expired auth → AUTH_REQUIRED + admin alert.

### 13.3 SDK lock fix (blocking for parallel OAuth upstreams)

`OAuthClientProvider._auth_flow` holds `context.lock` across the request. Subclass so the lock covers only token
initialization, refresh and 401 re-auth; keep a separate per-account refresh mutex (refresh-token rotation safety).
Concurrency test: N parallel calls on one account complete in ≈ max latency, not the sum.

---

## 14. Admin surface

### 14.1 API (`/admin/api/*`, FastAPI, session cookie + CSRF header)

Paths mirror 9router's so copied UI code needs minimal edits (research/03 §3.5). Nouns: 9router "providers" =
our accounts, "combos" = chains.

| Area | Endpoints |
|---|---|
| Auth | `POST auth/login`, `POST auth/logout`, `GET auth/status`, `GET auth/csrf` |
| Catalog | `GET catalog/providers`, `GET catalog/capabilities`, `GET/PUT pricing` |
| Accounts | `GET/POST providers`, `GET/PUT/DELETE providers/:id`, `POST providers/validate`, `POST providers/:id/test`, `POST providers/test-batch`, `POST providers/:id/reset-state`, quota scopes CRUD |
| OAuth connect | `GET oauth/:account/authorize`, callback via `/oauth/callback`, SSE status |
| Quota/usage | `GET usage/:accountId?force=1`, `GET usage/provider-limits`, `GET quota/snapshots`, `GET usage/stats|chart`, `GET usage/stream` (SSE) |
| Logs/replay | `GET logs?…`, `GET logs/:id` (summary + attempts + decision trace + redacted payloads), `POST logs/:id/replay` → new request with `replay_of`, `GET logs/:id/diff/:other`, `GET logs/stream` (SSE) |
| Chains | `GET/POST combos`, `GET/PUT/DELETE combos/:id`, `POST combos/:id/simulate`, `POST combos/:id/test`, `GET combos/metrics` |
| Health | `GET monitoring/health`, `DELETE monitoring/health?provider=` (reset breaker), `GET/DELETE resilience/locks`, `GET/PUT resilience` |
| Settings/policy | `GET/PATCH settings` (fusion k/W/weights/groups, reranker, cache TTLs, budgets, fan-out quorum/grace) |
| Jobs | `GET jobs`, `GET jobs/:id`, `POST jobs/:id/cancel` |
| Keys | `GET/POST keys`, `DELETE keys/:id` (personal access tokens) |
| Backup | `GET settings/export`, `POST settings/import`, `POST db-backups`, `GET db-backups/:id` |

All streaming endpoints use `text/event-stream` (Cloudflare buffers anything else).

### 14.2 UI

`ui-admin/`: 9router dashboard extracted to Vite + React 19 + react-router 7 + Tailwind 4, `next/*` shimmed via
Vite aliases, static catalogs replaced by `catalog/*` API, runtime i18n dropped (English). Pages: Providers & accounts
(cards, cooldown timers, test, OAuth connect), Chains editor (dnd-kit, simulate), Health (OmniRoute breaker matrix,
connections table, breaker timeline), Quota & usage (progress bars, charts), Request logs (OmniRoute LoggerV2 detail +
timeline, decision trace), **Replay + diff** (new), **Fusion & rerank policy** (new), Jobs, Settings, Backup, Keys.
Provider logos: our own neutral icons (no copied logos). Interim until P5: mcp-gateway's Svelte backends page for
OAuth connect + JSON admin API.

---

## 15. Configuration

- `config.yaml` (with `${ENV}` / `${ENV:-default}` expansion, key-file support): server (`public_url`,
  `trusted_proxy_ips`), auth (users, token TTLs, redirect allowlist), storage paths, **seed** for providers, quota
  scopes, accounts (secret references only), chains, settings.
- **DB is the runtime source of truth.** On startup, seed entries are inserted if absent; changes made in admin persist
  in DB. `cli config export` writes the current state back to YAML (secrets as references) for versioning.
- `cli check` validates YAML (pydantic, readable errors with paths — research-mcp `config_errors` extended).

---

## 16. Observability and replay

- Every tool call → `requests` row + one `attempts` row per provider attempt + a **decision trace** (every candidate
  step with decision and allowlisted skip reason, e.g. `skip: scite EXHAUSTED until 2026-10-01`, `skip: budget`,
  `skip: breaker OPEN`). No secrets; request URLs/params pass through a redaction layer (keys in query strings, e.g.
  XMLRiver).
- Structured logs (JSON) with `request_id`; metrics are served to the admin UI only (`/admin/api/metrics`, JSON; no
  Prometheus/Grafana integration in v1 — owner decision):
  per provider/account latency, success, cost, quota; per capability p50/p95; cache hit rates.
- **Replay** re-executes the stored normalized `CapabilityRequest` (not raw HTTP) with options: same policy version vs
  current, cache bypass on/off; result linked via `replay_of` and diffable (rank changes, added/removed handles,
  coverage differences). This is the tuning loop for chains, weights and rerank.
- Alerts (admin UI banner + optional webhook): AUTH_REQUIRED, EXHAUSTED on a primary, breaker OPEN > 10 min, schema
  drift, quota warn threshold, refresh-token/credit expiry ahead (OmniRoute `providerExpiration`).

---

## 17. Security

- No token passthrough to upstreams (startup assertion kept from mcp-gateway).
- SSRF guard (research-mcp) on every engine-side fetch, applied at entry and on each redirect hop; **fix DNS
  rebinding** by pinning the resolved IP for the connection; **cap probe response size** (e.g. 20 MB, streaming).
  `verify=False` TLS retries recorded as `tls_unverified` provenance.
- Secrets encrypted at rest; never logged; redaction on request logs and payload blobs.
- Admin: bcrypt login, signed session cookie, CSRF header, login rate limit; optional Cloudflare Access on `/admin/*`.
- MCP endpoint: OAuth or PAT only; `/register` rate-limited and gated.
- Upstream tool mapping (§7.4) is explicit, so a mapping bug cannot call an unmapped upstream tool.

---

## 18. Deployment

Host: the current machine (56 cores, 125 GB RAM, no GPU), shared with `knowledge-server-infra` (RAGFlow, own
`mcp-gateway`, Caddy, cloudflared, Infinity, Redis, monitoring) and OmniRoute (port 20128).

- **Own compose project** (`research-engine`), repo separate from `knowledge-server-infra`. Services: `engine`
  (python:3.12-slim, non-root, `/data` volume; admin UI built in a node stage). It joins the external Docker network
  `web` of `knowledge-server-infra` to be reachable by Caddy and to call Infinity for rerank. No host port published
  (optionally `127.0.0.1:<port>` for local debugging; must not collide with 80, 3000, 8080, 20128).
- **Ingress = existing tunnel + Caddy** (owner decision): add hostname `research.ducanh.cloud` to the existing
  Cloudflare tunnel → `caddy:80` → new Caddy site block `research.ducanh.cloud:80 { reverse_proxy engine:<port> }`
  with `flush_interval -1` so SSE is not buffered. TLS stays edge-terminated, as for the other hostnames. These are
  changes to `knowledge-server-infra` (Caddyfile + tunnel public hostname) and go through its own process.
- The existing Caddy `:80` block is deny-by-default with static bearer keys; the research hostname gets its own site
  block because OAuth discovery and callbacks must be reachable unauthenticated. Auth is enforced by the engine.
- `public_url = https://research.ducanh.cloud`; `trusted_proxy_ips` = Caddy's container address; client IP taken from
  `CF-Connecting-IP` only when the peer is Caddy.
- Cloudflare settings for this hostname: **no** Bot Fight Mode / JS or managed challenge / Access on `/mcp*`,
  `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`, `/oauth/*`; no redirects on the MCP URL; no
  caching of `/.well-known/*` or `/admin/api/*`. **Cloudflare Access (operator identity) on `/admin*`**, same pattern
  as `grafana.` and `ragflow.`.
- Health: `/healthz` (liveness), `/readyz` (DB + at least one READY provider per enabled capability). Monitoring is
  the admin UI only (owner decision); no Prometheus scrape in v1.
- Downtime: clients reconnect; upstream refresh tokens keep working; CIMD documents are refetched by upstream ASes
  only occasionally — surfaced as a health note.
- Resource guard: container CPU/memory limits (e.g. 8 CPUs, 8 GB) so the engine cannot starve RAGFlow/Elasticsearch.

---

## 19. Operating policy

ToS assessment is the **owner's responsibility** (owner decision, 2026-09-29). The engine enforces **no ToS-derived
restrictions**: every provider, capability, account and strategy is available and controlled by configuration only.
What remains are engineering defaults, all editable in config/admin:

1. **Rate limiters** are seeded from each provider's documented limits so requests are not wasted on predictable
   429s/blocks (e.g. arXiv 1 req/3 s, S2 1 rps). Values are configurable per account; a limiter can be disabled.
2. **Budgets and reserves** (§6.3) exist for cost control and default to permissive values; each can be turned off.
3. **Upstream MCP tool mapping** (§7.4) starts with the read tools the capabilities need; more upstream tools can be
   mapped through config. Undermind uses one dedicated workspace so engine traffic does not clutter personal ones.
4. **Explicit caps on Firecrawl crawl/map/agent** are defaults to avoid accidental large spends; configurable.
5. Provider attribution is kept in provenance (source URLs, provider names).

Out of scope because a research engine does not need them (not ported from OmniRoute/9router): MITM interception,
client fingerprint spoofing, CAPTCHA solving, cookie-session scraping of consumer web UIs, quota-reset tricks.

## 20. Testing strategy

- **Unit**: ported respx provider tests (research-mcp), new classifier rule tables per provider (status/body/tag →
  state), canonicalization fixture corpus (≈ 500 real URLs/ids from provider outputs), dedup tiers, RRF math, handle
  upgrades, state machine transitions, limiter behaviour.
- **E2E**: mcp-gateway's two-server harness (engine + fake OAuth-protected upstream MCP) for OAuth, both protocol eras,
  token refresh, upstream tool hiding ("upstream tools never listed/callable"), SDK lock fix concurrency, job handoff
  across deadline, restart resume.
- **Contract**: MCP tool schemas snapshot-tested; `search`/`fetch` shape tests against OpenAI's reference schema.
- **Live smoke** (opt-in, `pytest -m live`, budget-capped): one cheap call per provider/account; used before enabling a
  provider and after dependency bumps.
- **Quality eval**: 50–100 logged real queries labeled quickly (binary relevance top-20) → `ranx` comparisons of
  k ∈ {10, 20, 60}, weights, independence groups, rerank on/off. Run before changing defaults.

---

## 21. Roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Spikes** (≈1 wk) | Fork mcp-gateway; pin FastMCP; add RFC 9207; one `hello` tool; `research.ducanh.cloud` via existing tunnel + Caddy site block (SSE unbuffered); connect ChatGPT (dev mode), claude.ai, Claude Code; SDK lock fix prototype; rerank model on the shared Infinity + CPU benchmark (bge-v2-m3 vs gte-modernbert) with embedding-latency check; provision keys (OpenAlex, S2, GitHub PAT, Consensus API key, Exa, Firecrawl, Tavily, Brave/Serper) | All three clients complete OAuth and call a tool through the tunnel; lock-fix test passes; benchmark table recorded |
| **P1 Skeleton** | Package layout; migrations 0003 (providers/scopes/accounts/credentials/health/requests/jobs/cache); config seed; capability tool scaffolding with schemas; request log + decision trace; PATs | Tools list correctly in all clients; admin JSON API for accounts works |
| **P2 Web core** | Port research-mcp transport + web adapters (refreshed for 2026 changes); classifier rules; state machine, breaker, limiters, quota snapshots; chains + fan-out/sequential executors; URL canonicalization, dedup T1/T2, RRF; query/doc cache + coalescing; tools `web_search`, `news_search`, `web_read`, `developer_search`, `repo_search`, `get_evidence`, `search`, `fetch` | 10–30 s answers with coverage; failover demonstrated by forcing 402/429 in tests; ChatGPT `search`/`fetch` works |
| **P3 Academic open** | OpenAlex, Crossref, S2, arXiv, Scite public REST, bioRxiv/NCBI links; ID canonicalization, handles + aliases, version linking, dedup T4; enrichment signals; tools `paper_*`, `citation_graph`, `editorial_check`, `citation_verify` (free tiers) | Wakefield-style retraction surfaces from ≥ 2 sources; preprint/published collapse works on fixtures |
| **P4 Hosted MCP + jobs** | Upstream OAuth per account; Undermind, Consensus (REST key), Scite MCP adapters with allowlists + parsers; schema-drift hashes; jobs subsystem + pollers + resume; `deep_literature_search`, `site_crawl`, `site_map`, `systematic_review`; Elicit adapter (PLAN_BLOCKED until Pro) | Deep search via Undermind completes through `get_job` across a restart; quota exhaustion of Scite reflected as EXHAUSTED with reset date |
| **P5 Admin UI** | SPA extraction; all admin pages incl. replay/diff and policy editor | Owner can connect accounts, see health/quota, replay a request and compare |
| **P6 Quality** | Reranker plugins (Infinity + API), eval set + `ranx` tuning, independence-group map, SimHash/MinHash T3, Lune/alphaXiv adapters | Documented eval results; defaults updated from data |

---

## 22. Owner decisions (resolved 2026-09-29)

| # | Topic | Decision |
|---|---|---|
| 1 | Elicit / ToS in general | Enabled like every other provider; ToS is the owner's call, the engine enforces none. Elicit shows PLAN_BLOCKED only because the current account's API access is denied until Pro |
| 2 | Providers provisioned for v1 | Exa, Firecrawl, Tavily, Brave and/or Serper, Consensus API key, GitHub PAT (+ free OpenAlex and S2 keys). Not in v1: Perplexity, Parallel, Linkup |
| 3 | Scite | Free plan (25 MCP calls/month) + public REST (tallies, editorial notices); MCP only for budgeted statement-level verification |
| 4 | Hardware | Current host (56 cores, 125 GB RAM, no GPU), shared with knowledge-server-infra and OmniRoute |
| 5 | Ingress | Existing Cloudflare tunnel + Caddy, new hostname `research.ducanh.cloud`; Access on `/admin*` |
| 6 | Reranker | Shared Infinity cluster of knowledge-server-infra with an added rerank model |
| 7 | Monitoring | Admin UI only |
| 8 | Optional fallbacks | Keep Jina Reader keyless. DuckDuckGo scrape, Bright Data, crawl4ai not provisioned in v1 (adapters can be enabled any time) |

Still open (non-blocking, decide during P0):
- Exact hostname (default `research.ducanh.cloud`) and whether Brave, Serper or both are provisioned.
- Coordination of the two `knowledge-server-infra` changes (Caddy site block + tunnel hostname; Infinity rerank model)
  with that project's review/audit process.

---

## 23. Credits (for README)

- `R0Wi/mcp-gateway` @59c1efd — gateway shell, OAuth AS, upstream OAuth client, encrypted storage (used with the
  author's permission; pyproject declares MIT).
- `vvzvlad/research-mcp` @11f297d (MIT) — provider adapters, HTTP/SSRF/PDF layers, read pipeline (used with the
  author's permission).
- `decolua/9router` @f01fb90 (MIT, © 2024-2026 decolua and contributors) — routing, fallback, admin UI.
- `diegosouzapw/OmniRoute` @666ea59 (MIT, © 2026 diegosouzapw) — circuit breaker, quota model, resilience and log UI.
- `spences10/mcp-omnisearch` (MIT) — query-operator translation, provider registry patterns.
- `metatool-ai/metamcp` (MIT) — middleware, tool filtering, audit patterns.
- `exa-labs/exa-mcp-server`, `firecrawl/firecrawl-mcp-server` (MIT) — provider defaults.
- FastMCP (Apache-2.0), MCP Python SDK (MIT), and the libraries listed in research/07.
