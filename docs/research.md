# Research Engine — Research Summary

Status: synthesis of research tasks 1–7. Date: 2026-09-29.
Audience: whoever implements or reviews the engine. Decisions live in [design.md](design.md); this file records
*what we found* and *why it matters*. Detailed evidence, file:line citations and URLs are in `research/`:

| # | File | Topic |
|---|---|---|
| 01 | [research/01-research-mcp.md](research/01-research-mcp.md) | Deep read of `vvzvlad/research-mcp` |
| 02 | [research/02-mcp-gateway-fastmcp.md](research/02-mcp-gateway-fastmcp.md) | Deep read of `R0Wi/mcp-gateway`; FastMCP 4 vs python-sdk v2; async jobs; Cloudflare Tunnel |
| 03 | [research/03-omniroute-9router.md](research/03-omniroute-9router.md) | Routing/resilience logic and admin UI from OmniRoute and 9router |
| 04 | [research/04-academic-providers.md](research/04-academic-providers.md) | Scite, Elicit, Undermind, Consensus, OpenAlex, Crossref, Semantic Scholar, arXiv |
| 05 | [research/05-web-dev-providers.md](research/05-web-dev-providers.md) | Exa, Firecrawl, GitHub, Tavily, Perplexity (+ Jina/Brave/Serper/Linkup/Parallel); omnisearch/metamcp patterns |
| 06 | [research/06-mcp-clients.md](research/06-mcp-clients.md) | ChatGPT, claude.ai, Claude Code requirements; MCP spec 2026-07-28 |
| 07 | [research/07-canonicalize-fusion-signals.md](research/07-canonicalize-fusion-signals.md) | Evidence model, handles, canonicalization, dedup, RRF fusion, reranking, signals |

---

> **Historical research snapshot, not an implementation checklist (read with design v3, audited 2026-10-02).**
> Sections 1–7 and research/01–07 include options proposed *before* the local-tailnet pivot. The current decisions
> are in [design.md](design.md): research-mcp skeleton; from mcp-gateway reuse only upstream OAuth client/token-store,
> crypto and applicable migration logic, **not** its client-facing OAuth authorization server/schema or `sqlite3`
> access layer; no public MCP ingress/Cloudflare, React admin, ordinary-call jobs or automatic search enrichment.
> Sections 4–6 below are alternative algorithms/UI/router patterns, not v3 requirements. In particular,
> research/03 maps OmniRoute/9router "model" to our "capability" too directly: research providers are not
> interchangeable, so its control-plane machinery does not transfer by default. Pricing/account status and API
> observations dated 2026-09-29 need live verification before implementation.

## 1. Reuse verdicts

| Source | Verdict | What we take | What we leave |
|---|---|---|---|
| **`R0Wi/mcp-gateway`** @59c1efd (Py, FastAPI + FastMCP 4; author permission) | **Targeted upstream reuse** | Upstream MCP OAuth client (CIMD/DCR, proactive refresh, exact-`state` callback routing), Fernet envelope crypto, applicable migration logic, upstream e2e test patterns | Client-facing OAuth AS and its tables, gateway `sqlite3` storage layer, proxy/mount aggregation, Svelte UI |
| **`vvzvlad/research-mcp`** @11f297d (Py, MIT; author permission) | **Engine core seed** | `_http.py` (retry, 402/429/credit-body detection), `_url_guard.py` (SSRF), `pdf.py`, `failure_reason.py` (to be typed), `ClientManager`, 20 provider adapters + respx tests (496 tests, 93 % coverage), read-chain algorithm (probe → PDF → cost-gated cascade → best-thin), `search_and_read` waves, Jina reranker validation | `pipeline_config.py` (config in code), 2 fixed provider protocols, first-wins URL dedup, 4 fixed tools, Russian strings, in-memory counters/throttles |
| **`decolua/9router`** @f01fb90 (JS, MIT) | **Reference only** | Small priority-failover and error-handling ideas where they fit the simple router | Combo/policy language, React admin, LLM translator/executors, token compression, MITM, bulk token import, proxy pools |
| **`diegosouzapw/OmniRoute`** @666ea59 (TS, MIT, fork of 9router) | **Reference only** | Provider quota/error examples and normalization patterns | Breaker profiles, strategy catalog, request coalescing, time-series/trace UI, 193-migration schema, next-intl UI, stealth/fingerprint/CAPTCHA/cookie-session/free-proxy/quota-exploit features |
| **FastMCP 4.0.10** (Apache-2.0) | **Framework** (pin exactly) | MCP server (both eras), upstream `Client`, tool schemas; mount its HTTP ASGI app with lifespan | Server-side `OAuthProvider`/CIMD/private_key_jwt (no client-facing OAuth AS), proxy/mount/transform, `fastmcp-tasks` |
| `mcp` python-sdk 2.2 (MIT) | Transitive dependency | — | — |
| `spences10/mcp-omnisearch` (TS) | Reference | Declarative provider registry, `provider:mode` keys, error taxonomy with `retryable`, jittered retry, **search-operator parsing + per-provider translation**, large-result offload | Its orchestration (consumer picks provider; no merge/rerank) |
| `metatool-ai/metamcp` (TS) | Reference | Functional middleware chain, tool filter enforced on list **and** call, per-client profiles, audit middleware, upstream error tracker, tool-list hash for schema drift | Namespace passthrough |
| `exa-mcp-server`, `firecrawl-mcp-server` (TS) | Reference | Default params, timeouts, error handling, raw-POST to keep `creditsUsed` | Their tool surfaces |

## 2. Framework and protocol facts that shape the design

- **MCP 2026-07-28 is current**: sessionless, `server/discover`, no `Mcp-Session-Id`, state must travel as
  server-minted handles, Tasks moved to an extension, DCR deprecated in favour of CIMD, RFC 9207 `iss`.
  ChatGPT, claude.ai and Claude Code already speak it; we must serve **both eras**. FastMCP 4 does.
- **No hosted client supports the Tasks extension.** Long work must use our own `job_id` + `get_job` poll tools.
- **Deadline rationale (historical client survey):** hosted clients had ~60 s ceilings, while Claude Code timeout
  configuration differs. v3 defaults to 40 s and clamps ordinary calls to 5–50 s; a timeout returns **partial
  results without a job ID**. Only inherently async capabilities use `get_job`.
- **Output limits**: Claude Code warns at 10k tokens and caps at 25k; other hosted clients have different budgets.
  v3 targets compact default ≤ ~8k tokens and paginates reads.
- **ChatGPT `search`/`fetch`** and client-facing OAuth AS are deferred: v3's clients are Claude Code and agentRT
  over the tailnet with per-client bearer tokens. The ChatGPT and Cloudflare findings below apply only if public
  hosted clients become a future requirement.
- **Upstream OAuth checklist:** Undermind needs CIMD, a public HTTPS metadata document and a provider-accepted
  **HTTPS callback** accessible to the admin browser on the tailnet; Scite/Elicit support DCR. Preserve exact
  `state` matching, PKCE and resource binding where supported. This is distinct from client-to-engine auth.
- **Cloudflare Tunnel notes are historical**; v3 has no tunnel or public `/mcp` ingress.
- **SDK bug to fix (R1)**: `OAuthClientProvider` holds its lock across the whole upstream request, so parallel calls
  on one OAuth account serialize. Subclass so the lock covers only token init/refresh, keep a per-account refresh mutex.

## 3. Provider landscape (key facts)

### 3.1 Current account state (observed 2026-09-29)

| Provider | State | Note |
|---|---|---|
| Scite | **EXHAUSTED** until 2026-10-01 | Free "Connect" = 25 MCP calls/month. Public REST `/tallies/{doi}` and `/papers/{doi}` (incl. `editorialNotices`) need no token and don't use MCP quota |
| Elicit | **PLAN_BLOCKED** (`api_access_denied`) | API/MCP needs Pro ($49/mo). **API terms forbid building a competing "research engine" / search index** — biggest ToS risk |
| Undermind | READY (Free) | MCP only, **CIMD only (no DCR)**, workspace-scoped cite keys (DOI costs +1 call), deep search async 2–5 min |
| Consensus | READY (Free) | 30 calls/month, 10 papers/search, **no DOI on Free**; REST API key available on every plan (same pool) |
| OpenAlex | needs free key | Keys required since 2026-02; $1/day free; rich `x-ratelimit-*-usd` headers |
| Semantic Scholar | needs free key | Keyless pool 429s immediately; key = 1 RPS |
| Crossref, arXiv | READY | Crossref dynamic limits via headers; arXiv 1 req / 3 s, single connection |
| Exa, Firecrawl, Tavily, GitHub, Perplexity, … | keys to be provisioned | — |

### 3.2 Facts that constrain the design

- **Every vendor signals exhaustion differently**: Exa 402 + tag (key vs team scope), Firecrawl 402, Tavily 432/433,
  Perplexity **401** (ambiguous with bad key), Serper 400 body, Linkup 429 body, Scite tool-error text / JSON-RPC
  `-32001`, Consensus 429 body, Elicit `api_access_denied` / 402 / `pausedForInsufficientQuota`. → per-provider
  classifier rules (status + body + tag), not a shared status table.
- **Rate limits are team-wide at Exa and Firecrawl.** Extra keys in one team add cost labels and per-key budgets, not
  throughput. → account model needs **provider → quota scope (org/team) → credential**.
- **Dangerous defaults**: Firecrawl crawl `limit`=10000, map 5000, agent `maxCredits`=2500; Exa omits `contents` →
  10k chars of text. → engine always sends explicit caps.
- **Breaking changes in 2026**: Exa (`/research` retired, `neural` type gone, `livecrawl`→`maxAgeHours`,
  `research paper`→`publication`), Perplexity (Sonar chat ended 2026-09-27 → Search API only), GitHub
  (`search_type=semantic|hybrid` on issues, 10/min). research-mcp adapters need a refresh pass.
- **Proactive quota meters exist** for OpenAlex (headers), GitHub (headers), Firecrawl (`/v2/team/credit-usage`),
  Tavily (`/usage`), Elicit (`get_usage`), Linkup, Brave (monthly header), Exa (`costDollars` per call). Others need
  local counters against configured plan limits.
- **Hosted-MCP output is text**: Consensus and Undermind return Markdown → regex parsers; quota errors may arrive as
  `isError` tool results, not JSON-RPC errors.
- **Side effects**: upstream tools that write (collections, notes, shares, stars, `report_citations`) are not needed
  by any capability, so they are not in the default mapping (can be added in config). Undermind adds referenced papers
  to the workspace → use one dedicated workspace.

### 3.3 ToS notes (informational)

| Provider | Relevant rule | Consequence |
|---|---|---|
| Elicit | No competing research engine / search index / corpus; no proxying; loop safeguards | Flag to owner; if used: single user, TTL-cache only, no persistent corpus, budget guards |
| Undermind | One person per account; no repackaging into a product | Single-user personal gateway only |
| Scite | Automated access only via API/official connector; no circumventing limits | No multi-free-account stacking |
| Exa, Firecrawl, Tavily | No resale/proxy to third parties; no sharing keys | Never expose the gateway to other users |
| GitHub, Brave, Serper | Explicitly forbid multiple accounts to bypass limits | One personal PAT (+ at most one machine account/App) |
| arXiv | 1 req / 3 s across all our machines; circumvention banned | Global limiter |

This table is **informational**. Owner decision (2026-09-29): ToS assessment is the owner's; the engine enforces no
ToS-derived restrictions (design §19).

## 4. Algorithms (historical options; v3 adopts non-conflicting exact-ID/URL dedup + plain RRF)

- **Handles**: deterministic typed IDs, priority `doi > arxiv > pmid > pmcid > openalex > s2 > isbn > gh > url-hash`;
  upgrades keep old handles as permanent aliases; preprint and published = separate evidence, linked and collapsed in
  responses.
- **Canonicalization**: no library is complete (`url-normalize`, `courlan`, `w3lib` each miss steps; `idutils` misses
  arXiv-in-URL and keeps DOI case) → own URL/ID layer (~300 LOC + table tests) on top of `url-normalize` + tracker list.
- **Version linking**: union of Crossref `relation`, bioRxiv `/pubs`, S2 `externalIds`, arXiv `arxiv:doi`, OpenAlex
  `locations`; fuzzy only as a non-merging hint.
- **Dedup**: strong-ID union-find with cannot-link → canonical URL → near-dup text (SimHash/MinHash) → fuzzy
  title+year+first-author. Ambiguous → `possible_duplicate_of`, never merge.
- **Fusion**: hierarchical weighted RRF, k = 60, window W = 20, inner per-provider (best rank over variants/accounts),
  outer across **independence groups** (providers sharing an index count once).
- **Rerank**: off until benchmarked; when on, top-40 → second-stage RRF(fused ×1, reranked ×2). Local
  `gte-reranker-modernbert-base` (EN) or `bge-reranker-v2-m3` (multilingual), both Apache-2.0; APIs Voyage/Cohere.
  Jina **weights** are CC-BY-NC (Jina **API** is fine). CPU latency is an estimate — spike required.
- **Signals**: only agreement affects ranking (implicitly via RRF). Editorial (Crossref `updated-by` incl. Retraction
  Watch, OpenAlex `is_retracted`, Scite public REST — all free), citations, Scite tallies, venue, stage, OA, web
  dates, source type, freshness, code stats, versions, and a per-call coverage/gaps report. `unknown ≠ negative`.
  Enrichment runs only on returned top-k, batched and cached.

## 5. Routing/resilience options (historical; v3 adopts only cooldowns + priority failover)

- Three scopes, kept separate: **provider breaker** (CLOSED→DEGRADED→OPEN→HALF_OPEN; trips only on
  408/5xx/transport), **account state** (cooldown with exponential backoff; terminal states never overwritten by
  transient ones), **account×capability lock** (plan gating, per-endpoint limits; success-decay).
  Effective state = max severity of the three for the requested capability.
- Chains ("combos"): ordered steps (provider / nested chain / pinned account), `fallback_only_on_exhaustion`,
  per-target and whole-chain timeouts, cooldown-aware wait for short `retry-after`, response validation (0 results =
  soft failure).
- Fan-out primitive: **quorum + straggler grace + hard timeout** (9router `collectPanel`), stragglers finish into the
  cache.
- Account selection: fill-first (default), sticky round-robin, P2C, least-used, weighted, reset-aware — all
  configurable per provider.
- Quota: `QuotaSnapshot` windows from (1) provider usage API, (2) response headers, (3) local counters; preflight
  cutoff at ≤ 2 % remaining, warn at 20 %.

## 6. Admin UI

9router UI as base (≈17k LOC in scope, plain English, little Next coupling) extracted to **Vite + React 19 +
react-router 7 + Tailwind 4**, with `next/*` shimmed via aliases; cherry-pick OmniRoute health/breaker views,
request-log detail/timeline, resilience settings, chain editor pieces, backup tab. Neither repo has replay or a
fusion-policy editor — we build those. Estimate ≈ 15–20 person-days + 2–4 for new pages. Do not copy provider logos.

## 7. Top risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Elicit API terms vs "research engine" | Inform the owner; they assess ToS. No ToS-derived engine restriction (§14 of design) |
| 2 | Hosted-MCP quota snapshots are small and date-sensitive | Configure technical limits and verify current quotas when provisioning; open REST sources cover common requests |
| 3 | Ordinary call deadlines vs inherently async work | Partial ordinary results without jobs; persisted jobs only for inherently async capabilities |
| 4 | SDK OAuth lock serializes per-account calls | Subclass fix + concurrency test before enabling parallel OAuth upstreams |
| 5 | FastMCP churn + semi-private APIs | Exact pin, e2e suite (from mcp-gateway) on every bump |
| 6 | Upstream schema drift of hidden MCP tools | Tool-list hash per account in health; alert |
| 7 | Adapter rot (2026 breaking changes; 3 unverified research-mcp adapters) | Live smoke test per provider behind an opt-in flag |
| 8 | SQLite contention (cache/jobs/log volume) | WAL, busy_timeout, short SQLAlchemy transactions, blobs on disk; optimize only if observed |
| 9 | Undermind CIMD and callback availability | Public static metadata document; live-test provider acceptance of a tailnet HTTPS callback |
| 10 | Admin UI scope grows past workload | Start with sqladmin; add a custom UI only if it becomes a real limitation |
