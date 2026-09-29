# Research Engine — Requirements Notes

Status: clarification rounds 1–7 done. Research + design delivered: see research.md and design.md.
Last updated: 2026-09-29

## Positioning

"OmniRoute for research": 1 MCP endpoint → many research providers → many accounts →
automatic routing/failover → clean ranked evidence.

**In scope:** search / retrieve / verify / merge / rank infrastructure.
**Out of scope:** hypothesis generation, research priority, knowledge admission,
campaign opening, scientific decisions, final source acceptance. Not an autonomous researcher.

## Guiding principle: maximize reuse

Build on existing source code wherever possible; custom code only for the research-specific
core (capability router, account router, provider policies, canonicalization/dedup, fusion,
signals). Language: **Python** (follows from the core foundation).

## Source repos

| Tier | Repo | Lang / License | Role | Reuse mode |
|---|---|---|---|---|
| Core | `modelcontextprotocol/python-sdk` | Py / MIT | MCP server/client, Streamable HTTP, OAuth primitives | Dependency |
| Core | `vvzvlad/research-mcp` | Py / MIT | Provider type/instance, search/read pipeline, failover (402/429/credit-body detection), URL dedup, rerank, URL guard, PDF | **Copy** (author permission; credit in README). No upstream sync; manually cherry-pick later if needed |
| Gateway/Auth | `R0Wi/mcp-gateway` | Py (FastAPI+FastMCP 4) / no LICENSE file, pyproject says MIT | Client-facing OAuth 2.1 AS (DCR/CIMD/PKCE, RFC 8414/9728/8707), upstream MCP OAuth client + token refresh, Fernet-encrypted SQLite, alembic, YAML config, both MCP protocol eras | **Copy** (author permission; credit in README) |
| Gateway/Auth | `metatool-ai/metamcp` | TS / MIT | Aggregation, namespace, middleware, tool filtering | Reference |
| Provider/Pipeline | `spences10/mcp-omnisearch` | TS / MIT | Multi-search adapters, merge/rerank | Reference |
| Provider/Pipeline | `exa-labs/exa-mcp-server` | TS / MIT | Exa tool schemas/params | Reference; implement via `exa-py` |
| Provider/Pipeline | `firecrawl/firecrawl-mcp-server` | JS / MIT | Firecrawl tool schemas/params | Reference; implement via `firecrawl-py` |
| Admin/Router | `diegosouzapw/OmniRoute` | TS (Next.js/React, recharts, xyflow, monaco) / MIT | Dashboard UI; combo/fallback chains, quota/usage tracking, circuit breaker/cooldown | **Copy React components**; **port logic to Python** |
| Admin/Router | `decolua/9router` | JS (Next.js/React) / MIT | Same as OmniRoute (predecessor/sibling) | Copy/port where better than OmniRoute |
| Framework | `jlowin/fastmcp` | Py / Apache-2.0 | Proxy/mount upstream MCP, OAuth proxy, middleware, tool transform | **Evaluate vs raw python-sdk** (note: mcp-gateway already depends on FastMCP 4) |
| — | `scitedotai/scite-mcp-skill` | MIT | Skill/reference only | Not a dependency |

Hosted MCP providers (no fork, connect directly via MCP client + OAuth): **Scite, Elicit,
Undermind, Consensus**.

## Decisions

| Topic | Decision |
|---|---|
| Upstream access | **Hybrid.** Official REST API/SDK where available (Exa via exa-py, Firecrawl via firecrawl-py, GitHub, Tavily, OpenAlex/Crossref/S2/arXiv). MCP-client-over-OAuth for MCP-only providers (Scite, Elicit, Undermind, Consensus). |
| Upstream tool exposure | **Hidden.** Consumers see only normalized capability tools; upstream tools are internal. |
| Deployment | **Self-hosted, single user** (no plan for many users). Home machine + **Cloudflare Tunnel** for HTTPS. OAuth so ChatGPT / Claude.ai can connect; also Claude Code / agentRT. |
| Skeleton | Delegated to design doc. Leaning: mcp-gateway as app shell (auth, storage, upstream clients, config) + research-mcp pipeline/providers ported in as engine core. |
| Storage | Delegated to design doc. Criteria: fast to ship, few bugs, easy to scale. Leaning: single SQLite (WAL, encrypted secrets, alembic) incl. cache/jobs/handles; storage interfaces allow later Postgres/Redis. |
| First deliverable | **Research + design doc** (no code yet). Docs in Markdown, English. |
| Account pool | Multiple keys/plans/accounts per provider; mostly 1 today; model supports N. Used for failover, rotation, cost allocation, plan-gated capabilities. Any number of accounts and any strategy (owner decides ToS). |
| MCP tool surface | **One tool per capability.** ChatGPT `search`/`fetch` compatibility: **research current ChatGPT requirements first**, decide in design doc. |
| Capability classification | Consumer specifies (tool choice); rule/heuristic fallback. No LLM in the engine. |
| Reranker | Pluggable + toggleable: local cross-encoder or rerank API. Default fusion = RRF. |
| Long-running jobs | Async jobs (job_id + poll/notify), persisted across restarts. |
| Open metadata | OpenAlex, Crossref, Semantic Scholar, arXiv as internal resolver **and** providers. |
| Admin UI | **Copy React components from OmniRoute/9router** → SPA over a Python admin API. Keep mcp-gateway's OAuth login/consent pages only. |
| Admin v1 scope | Provider/account mgmt + OAuth connect + test; health/quota dashboard; request log + replay; policy editor (routing, fusion weights, reranker toggle). |
| Router logic from OmniRoute/9router | Port: combo/fallback chains, quota/usage tracking, circuit breaker/cooldown. **Plus inventory any other reusable logic** during research. |

## Usage pattern

- A working session = **many small calls**, not one big deep call.
- Normal call: **quality over latency, 10–30 s acceptable** → wide fan-out, wait, fuse, rerank, enrich.
- **Evidence handles** (stable canonical IDs) + **query/document cache** with TTL.
  No cross-call "seen" suppression.
- Response: **compact + expand** (top-k ~8–10: title, handle, snippet, signals, provenance summary;
  details via read/get by handle).

## Evidence signals (research further)

Signals only; consumer interprets. Candidates: cross-provider agreement; scholarly
(retraction/editorial, citations, Scite supporting/contrasting, venue, year, peer-reviewed vs
preprint, OA); web (publish date, domain, source type, freshness); coverage/gaps report;
full per-item provenance. Open: definitions, source per signal, effect on ranking.

## Research tasks for the design doc

1. Deep-read research-mcp + mcp-gateway: module-by-module keep/modify/drop map.
2. FastMCP vs raw python-sdk for gateway, proxying, middleware, auth.
3. OmniRoute/9router: UI components to copy; combo/quota/circuit-breaker logic to port; other reusable pieces.
4. metamcp / mcp-omnisearch / exa & firecrawl MCP servers: patterns + best param defaults.
5. Per provider: API/MCP tools, capabilities mapping, limits, pricing, ToS for programmatic/MCP-client use,
   quota observability (how to detect RATE_LIMITED / EXHAUSTED / PLAN_BLOCKED).
6. ChatGPT connector requirements (`search`/`fetch`) as of now.
7. Signal definitions + canonicalization (DOI/arXiv/PMID/URL) + fusion design.
8. Storage/job architecture; Cloudflare Tunnel + OAuth callback constraints.
9. Credits/attribution section for README (research-mcp, mcp-gateway, OmniRoute, 9router, …).

## Owner decisions after design review (2026-09-29)

See design.md §22. Summary: no engine-enforced ToS restrictions — owner handles ToS (design §19); Elicit enabled; v1 keys = Exa, Firecrawl, Tavily, Brave/Serper,
Consensus API key, GitHub PAT, OpenAlex, S2; Scite free + public REST; host = current machine (no GPU) shared with
knowledge-server-infra; ingress via existing tunnel + Caddy on a new hostname; reranker on the shared Infinity;
monitoring via admin UI only; only Jina keyless kept among optional fallbacks.
