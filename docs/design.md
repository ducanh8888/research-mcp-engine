# Research Engine — Design (v4)

Status: current target architecture, updated 2026-10-05. Code baseline:
`8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`. This document specifies the target;
[roadmap.md](roadmap.md) records implementation status and acceptance.
[requirements-notes.md](requirements-notes.md) records owner decisions.
[cleanup.md](cleanup.md) records required removals and replacements.
[code-audit.md](code-audit.md) records additional baseline defects and evidence limits.

`main` stays frozen at that baseline. Work continues on `research-specialists`.
Existing source and fixtures are implementation evidence, not proof of a live deployment.
Design v1–v3 and superseded decisions remain in git history.

## 0. Boundary

One private MCP endpoint for search / retrieve / verify / merge / rank.

Request → capability → provider → account → execute/fallback → normalize →
conservative dedup → RRF for ranked lists → optional rerank → provenance + results.

The consumer chooses the capability and interprets evidence. The engine does not classify
query intent, generate hypotheses, judge evidence, select an automatic best-N source set,
or make scientific decisions. No LLM or autonomous research scheduler runs in the engine.

OmniRoute supplies supported search, fetch and API rerank operations as an upstream service.
Research Engine supplies research-specialist operations missing from that service.
Reuse compatible code; importing a model router's control plane is outside this boundary.

## 1. Architecture

| Component | Owns |
|---|---|
| Research Engine | Capability routes, specialist accounts, normalized results, provenance, exact dedup, RRF, handles, cache, specialist jobs |
| OmniRoute | Its provider credentials, account selection, quotas/cooldowns and API normalization |
| Specialist adapters | Provider-specific HTTP/MCP translation, errors, entitlement and result parsing |
| Consumer | Capability selection, evidence interpretation and final source acceptance |

The engine uses one FastAPI/FastMCP process, SQLite WAL, encrypted secrets and sqladmin.
Clients use Streamable HTTP at `/mcp` with per-client bearer tokens over loopback/tailnet.
Admin is at `/admin`; the implemented upstream OAuth callback is
`/admin/oauth/callback`; liveness is at `/health`.

The bridge uses a private configured OmniRoute URL and an engine-to-OmniRoute credential.
It never forwards a researcher's bearer token. OmniRoute downtime affects bridged routes;
direct specialist routes remain independently usable.

## 2. Repository and reuse

Existing directories: `server/`, `router/`, `providers/{web,scholar,dev,mcp}/`,
`merge/`, `jobs/`, `admin/`, `storage/`, plus tests and deployment scripts.

At baseline, `providers/mcp/clients.py` and `oauth.py` exist;
`providers/mcp/adapters.py` and the OmniRoute bridge do not exist.
Future module names are implementation choices, not claims that files already exist.

| Source | Use |
|---|---|
| `vvzvlad/research-mcp` @11f297d | Compatible HTTP, SSRF/PDF and adapter code; preserve attribution |
| `R0Wi/mcp-gateway` @59c1efd | Upstream OAuth/token storage patterns and crypto; no client-facing OAuth AS |
| OmniRoute | Upstream search/fetch/rerank service; no copied combo/policy engine or UI |
| sqladmin | Existing admin dependency |
| research/01–07 | Historical investigations; current contracts here override their recommendations |

A copied-code notice currently exists under `providers/web/`. Complete provenance review
and top-level notices are tracked in P0; author permissions in historical notes do not
establish attribution for every existing module.

## 3. Runtime and authentication

- Python ≥3.12. Baseline pins FastMCP 4.0.10 and MCP SDK ≥2.2,<3 in `pyproject.toml`.
  Use the lockfile; dependency updates require actual MCP transport tests.
- One uvicorn worker. Blocking DB/PDF/model work runs off the event loop.
- Preserve the implemented MCP mount/lifespan arrangement after transport verification.
  The externally reachable endpoint is `/mcp`; no redundant mount rewrite is required.
- Reject absent/revoked/invalid tokens on every MCP transport request. Validate Origin when
  supplied. Admin uses session authentication and CSRF protection.
- Keep provider secrets encrypted and redact logs. Engine-side URL reads use SSRF/size guards.
  A private OmniRoute service URL is operator configuration, not a consumer-supplied URL.
- Loopback is the local default. Tailnet binding uses the actual server IP; the checked-in
  `100.66.213.111` is the prior deployment target, not evidence this environment owns it.
- OAuth uses a provider-accepted HTTPS callback on the same origin as admin Connect.
  Publish public CIMD metadata only when required; that document does not expose the engine.

## 4. MCP surface and outputs

Sixteen capability tools plus `get_job`; upstream tool names remain internal.

| Tool | Execution | Output |
|---|---|---|
| `web_search`, `news_search` | fanout | ranked `items[]` |
| `web_read`, `paper_read` | sequential | source `document`, optional next cursor |
| `site_map` | sequential | `urls[]` |
| `site_crawl` | sequential async start | job; completed URLs/documents |
| `paper_search`, `paper_related` | fanout | ranked `items[]` |
| `paper_metadata` | sequential per unresolved ID | `records[]`, `not_found[]`, per-ID coverage |
| `citation_verify` | fanout aggregation | bibliographic assertions, sources, attributable claim evidence |
| `citation_graph` | fanout aggregation | `nodes[]`, `edges[]`, `truncated` |
| `editorial_check` | fanout aggregation | source-attributed `checks[]` |
| `systematic_review`, `deep_literature_search` | sequential async start | job; completed capability-specific records |
| `developer_search`, `repo_search` | fanout | ranked `items[]` |
| `get_job` | local lookup/poll | stored job envelope |

Tools declare schemas, structured content and appropriate read/write annotations.
Tool discovery does not prove a provider is configured. Unavailable tools return an explicit
error with coverage; empty success is reserved for a successful zero-result upstream call.

Ordinary results include `request_id`, `status: complete|partial` and
`coverage: {ok, failed, skipped}`. Items retain canonical handle, URL, title,
provider-supplied fields and provider/rank provenance. Bridged provenance names the actual
upstream provider and OmniRoute transport; never count both as independent search lists.

Read output labels full source text versus abstract. Generated QA/report prose is not full
text. Metadata/verification/graph records retain conflicting source assertions; RRF never
resolves their disagreements. Scite tallies are citation-level counts, not verification of
a particular claim. Missing editorial data means unknown.

Default search limit is 8, maximum 25. Reads paginate at approximately 20,000 characters.
Tool errors use `INVALID_INPUT`, `NO_PROVIDER_AVAILABLE`, `NOT_FOUND` or `INTERNAL`;
a failed provider does not erase successful peers.

`get_evidence` is removed. `SITE_INTERACT`, public ingress and `/mcp/compat` are deferred.

## 5. Provider scope and bridge

The scope decision is per operation, not per vendor. Existing direct commodity code is
retained at the frozen checkpoint. New work on covered operations goes through the bridge.
Retirement follows equivalent-operation validation and route migration, not deletion by name.

OmniRoute source was inspected at
[`fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b`](https://github.com/diegosouzapw/OmniRoute/tree/fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b).
Its `package.json` says 3.8.51. On 2026-10-06 the running local `omniroute`
container's `/app/package.json` also reported **3.8.51**; this verifies that
installed package, not a purported 3.8.52 release. The existing API-scoped key
can call `/v1/search`, `/v1/web/fetch` and `/v1/rerank` through the configured
private URL but receives 403 on account/provider management endpoints. Therefore
its provider catalog and successful calls do not establish the configured account
inventory or same-provider failover.

| Operation | Ownership |
|---|---|
| Search: Brave, Serper, Tavily, Exa, Firecrawl, Perplexity, Linkup, SearchAPI, You.com, Google PSE, SearXNG, Ollama, Z.AI, Jina, DuckDuckGo, AnySearch, Nimble | Bridge `POST /v1/search`; direct development deferred |
| Fetch: Firecrawl, Jina Reader, Tavily, TinyFish, AnySearch, Nimble | Bridge `POST /v1/web/fetch`; direct development deferred |
| Context7 library documentation search/fetch | Bridge when its library-reference semantics match the tool; not a generic URL reader |
| API rerank: Jina, Cohere, Voyage and other configured OmniRoute backends | Bridge `POST /v1/rerank`; direct API backend development deferred |
| OpenAlex, Crossref, Semantic Scholar, arXiv | Direct specialist adapters |
| Scite REST/MCP, Elicit API/MCP, Consensus, Undermind | Direct specialist adapters |
| GitHub repo/code/issues | Direct specialist adapter |
| Firecrawl site map/crawl | Direct gap; generic URL fetch does not supply this lifecycle |
| Exa contents or specialized developer operations | Direct only for a demonstrated semantic gap; no extra generic search adapter |
| Local PDF/trafilatura, Infinity/FastEmbed | Retain local execution; optional rerank remains off |

Evidence: OmniRoute
[search registry](https://github.com/diegosouzapw/OmniRoute/blob/fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b/open-sse/config/searchRegistry.ts),
[fetch handler](https://github.com/diegosouzapw/OmniRoute/blob/fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b/open-sse/handlers/webFetch.ts),
[rerank registry](https://github.com/diegosouzapw/OmniRoute/blob/fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b/open-sse/config/rerankRegistry.ts).

Bridge contract:

- Each virtual provider identifies one upstream operation, e.g. `omni:brave-search`.
  Send the exact OmniRoute provider ID: Firecrawl search is `firecrawl`, DuckDuckGo is
  `duckduckgo-free`, fetch uses `jina-reader` or `tavily-search`. Virtual names are not API IDs.
- Always specify the upstream provider (or `provider/model` for rerank). No automatic
  cross-provider OmniRoute selection; the engine owns research fanout/fallback.
- Verify requested versus returned provider. A hidden provider substitution is an error,
  not another vote in RRF. Parameter/filter gaps stay explicit; no silent dropping.
- Normalize responses and typed errors within the bridge. Gateway authentication failure
  affects the bridge connection; provider exhaustion is scoped to that virtual provider,
  not every provider sharing the OmniRoute credential.
- The engine stores the OmniRoute connection credential, not copies of commodity accounts.
  Selection, balancing and same-provider account failover remain OmniRoute's responsibility.
  Validate those behaviors on the actual instance; endpoint presence alone does not prove them.
- Reads request source content; bridge cache/fresh behavior must match the advertised contract.
  If upstream cannot bypass cache, report that limitation instead of claiming a live fetch.
- One bridge transport with operation mappings is enough. No mirror admin, account database,
  copied breaker framework or generic plugin platform is required.

Direct adapters keep a small `call/start/poll/cancel` contract and capability-specific results.
Hosted MCP adapters map known tools and test actual schemas; arbitrary upstream namespace
passthrough is outside scope. Incompatible schemas appear as adapter errors, separate from
credential availability. Undermind `read_pdfs` QA is not a `paper_read` source.

## 6. Routing

One DB entry per capability: `mode: fanout|sequential` and an ordered provider list.
Validate capability support and duplicate entries on save. Seed only explicitly approved
routes for implemented adapters; registration alone does not append a provider to every route.

Fanout calls available providers concurrently within one deadline. Sequential tries ordered
providers until a usable result; batched metadata retries unresolved IDs. Async start is
sequential and selects one provider/account. `job` is a lifecycle, not a routing mode.

Validate consumer input before routing. Ordinary deadline defaults to 40 seconds, clamps
to 5–50 seconds, cancels stragglers and returns partial coverage without creating a job.
All failed/unavailable providers return `NO_PROVIDER_AVAILABLE`. A single upstream 404
permits fallback; it does not establish global absence.

No nested chains, classifier, per-step policy language, spending strategy or policy version.

## 7. Specialist multi-account

Each direct provider owns N encrypted accounts with priority, enablement, credential state,
cooldown, blocked capabilities and optional shared `quota_group`.
The bridge owns one connection configuration; commodity accounts stay in OmniRoute.

Target selection modes, configured per direct provider:

| Mode | Selection |
|---|---|
| `priority` (default) | First eligible account by priority, stable ID tie-break; next eligible account on account failure |
| `round_robin` | Rotate eligible accounts within the best priority tier; fall back to lower tiers when needed |
| `quota_aware` | Prefer usable accounts with comparable remaining-quota observations; unknown/stale/incomparable values fall back to priority |

Filter disabled, invalid credentials, active cooldowns and blocked capabilities before
selection. Select one account per provider per request; accounts are not separate RRF votes.
Recheck eligibility before every retry, including shared quota-group cooldown changes.
Concurrent selection updates use a small in-process critical section; no durable scheduler.

Keep three independent dimensions: `credential: ok|needs_auth|disabled`, cooldown timestamp/reason,
and capability blocks. Migrate the existing `valid` alias once; avoid indefinite dual vocabulary.

Adapters classify `rate_limited|exhausted|auth|plan|transient|bad_request|target`.
Known `retry_after/reset_at` is authoritative. Unknown quota reset remains unknown; a bounded
retry cooldown is labelled a retry estimate, never invented billing/reset metadata.
Store the latest quota observation with its units/scope/time when needed for selection.
Use provider defaults when limits are documented, in-memory account/group limiters, and
provider-aware error translation. No background health traffic or quota time series.

An admin Test performs a real check. It does not first erase authentication, entitlement or
cooldown evidence; Reset and credential reconnection are separate explicit actions.

## 8. Merge and rerank

Normalize provider fields → canonical strong IDs/URL → exact-key clusters with ID-conflict
checks. Different known DOIs/arXiv IDs/PMIDs remain distinct even when URLs overlap.
Stable handles survive restart; aliases do not join conflicting identities.

A scholarly hit missing strong IDs may get one exact normalized-title + year resolver lookup;
ambiguous matches stay separate. Preserve source fields and provenance; no fuzzy evidence merge.

Search-like results use one ranked list per actual provider and plain RRF:
`score = Σ 1/(60 + rank)`. No account votes, independence map or second-stage fusion.
Graph/metadata/verification/editorial outputs aggregate source records without RRF.

Optional rerank replaces the order of the top-N; failure preserves fused order with diagnostics.
Cloud rerank uses OmniRoute; local Infinity/FastEmbed remains available. The baseline module
and eight-case CPU benchmark exist, but the ordinary engine search path does not invoke rerank.
Integration, secret ownership and representative evaluation remain roadmap work.

No implicit enrichment or preference ranking. Existing disabled weighted/hierarchical RRF
and fuzzy/version relationship helpers are historical experiments, not current runtime requirements.

## 9. Async jobs

Only site crawl, deep literature search and systematic review use durable jobs.
Select/start sequentially; definite failure may try another account/provider.
Ambiguous start or poll failure never automatically starts duplicate upstream work.

Persist provider, account, upstream ref, client-token owner and status/result immediately
after start returns a recoverable ref. One process resumes polling referenced running jobs
after restart. Terminal states are completed, failed and cancelled.
`get_job` checks ownership; admin Cancel reports whether upstream cancellation actually occurred.

A crash after upstream creation but before ref persistence can orphan work. Record unknown
start outcomes and reconcile explicitly; no exactly-once guarantee or distributed lease queue.

## 10. Cache

Query and source-document caches use TTL; cache only complete query results.
Partial calls re-attempt unfinished work. Read cache contains source text, not question answers.
Fresh bypasses engine cache and supported upstream caches, with limitations exposed.

Route changes invalidate affected query cache automatically. Provider enablement/operation
changes invalidate affected routes' query cache. Do not require manual Clear as a correctness
workaround, or introduce policy-version identity. Document cache stays independent.

## 11. Storage

SQLite WAL, short SQLAlchemy transactions and blobs on disk. Existing migrations are retained.
Add schema only for accepted work in the current phase; no Postgres/distributed placeholders.

Existing tables cover providers/accounts/secrets, routes, client tokens, requests/attempts,
handles/aliases, query/document cache and jobs. Specialist account selection can add only
the fields it actually consumes. Secrets use the same account-scoped encrypted store.

Back up DB, blobs, config and matching encryption/session keys together. Wrong/missing key
fails startup; never silently replace the credential store.

## 12. Configuration

YAML owns server/bootstrap settings; DB owns provider/account/route/client-token settings.
CLI `init`, `migrate`, `import-env`, `serve`, `token create/revoke` exist.
There is no implemented `export/import` backup CLI.

Bootstrap seeds implemented catalogs and explicit routes without overwriting operator edits.
Commodity credentials are configured in OmniRoute after bridge cutover. Existing direct
accounts remain intact until migration is verified. No hidden provider fallback is added
when OmniRoute is unavailable.

## 13. Admin

Keep sqladmin and its four functions: provider/account management and Test/Connect/Reset;
latest availability/quota; request inspection/replay; routing and rerank settings.
Client token creation/revocation, cache clearing and job inspection/cancel remain small actions.

Specialist selection mode is editable per provider. Bridged entries show upstream provider
and bridge connection status, without copying OmniRoute account management.
Replay reruns current config with explicit cache bypass and keeps the old request.
No policy simulator, decision timeline, replay experimentation or React extraction.

## 14. Operating policy

The owner decides provider terms/account use. Technical authentication, rate limits,
capability entitlement and request caps remain explicit. Failures stay visible; no success
claims from placeholders, broad exception suppression or unavailable live checks.

## 15. Deployment

Local/tailnet-first, single user, one process. No public MCP/admin ingress or client-facing OAuth AS.
Follow [deployment.md](deployment.md) for baseline commands and limitations.
Use a private reachable OmniRoute endpoint; container loopback is not the host's loopback.
Direct specialist execution remains available when the bridge is down.

## 16. Deferred

Deferred means excluded from the current implementation queue, not a promise to build every
historical idea. A new requirement or measured gap reopens an item. No placeholder schemas,
capabilities, APIs or dependency extras are created to reserve future work.

| Item | Condition to reopen |
|---|---|
| New direct commodity adapters | Tested operation gap in installed OmniRoute; document the exact missing semantics |
| Public ingress, client OAuth AS, `/mcp/compat` | Owner needs a consumer outside loopback/tailnet |
| `SITE_INTERACT` | Owner requests a concrete browser-action workflow |
| Weighted/hierarchical RRF, fuzzy relationships, version linking | Representative evaluation/logs establish a need beyond exact dedup + plain RRF |
| Nested route policy, breaker framework, spend model | Existing simple routing/account cooldowns demonstrably cannot meet a requirement |
| Health scheduler, quota charts, coverage analytics | Explicit operational requirement with evidence from actual use |
| Distributed workers, Postgres, multi-user abstractions | Deployment requires more than this single process/user |
| Custom React admin | A required task cannot be completed through current sqladmin/actions |

Existing experimental helpers stay disabled and outside release acceptance; their presence
does not justify further work. The old instruction to implement all Deferred items is
superseded by the specialist/bridge scope decision.

## 17. Roadmap and definition of done

[roadmap.md](roadmap.md) is the single roadmap and acceptance record.
[cleanup.md](cleanup.md) is the source-grounded removal/replacement checklist.
A phase is accepted only after its stated checks actually pass; blocked live provider/client
checks remain pending. Module presence, fixtures and authored benchmarks are reported separately.

## 18. Credits

Historical reuse references: research-mcp @11f297d, mcp-gateway @59c1efd,
9router @f01fb90, OmniRoute @666ea59, mcp-omnisearch, Exa/Firecrawl MCP servers,
FastMCP and MCP Python SDK. Retain source notices and establish exact copied paths/licenses
during P0; reference and dependency are distinct from copied code.
