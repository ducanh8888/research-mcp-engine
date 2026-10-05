# Research Engine — Requirements Notes

Current owner decisions, updated 2026-10-06.
[design.md](design.md) defines runtime contracts; [roadmap.md](roadmap.md) defines the work queue.
Superseded requirement tables remain in git history, not in the active specification.

## Goal and boundary

One private MCP for search / retrieve / verify / merge / rank across research providers.
Consumers are local Claude Code/agentRT sessions and tailnet machines.
The engine returns compact results with handles and provenance; the consumer interprets evidence.

No hypothesis generator, research scheduler, evidence judge, automatic best-N source selector,
query-intent classifier, internal RAG or scientific decision layer.

## Current scope

| Decision | Requirement |
|---|---|
| Branches | Freeze `main` at `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`; work on `research-specialists` |
| Immediate task | Implement the current roadmap and enforce the owner evidence-coverage directive in `AGENTS.md`; C33 is open |
| OmniRoute coverage | Defer new direct work on covered search/fetch/API rerank operations; use one small bridge |
| Specialist work | Build/harden operations OmniRoute does not provide: academic, hosted research MCP, GitHub and site map/crawl gaps |
| Existing adapters | Preserve checkpoint code/data and verify bridge cutover; covered direct operations must not remain active fallback paths or duplicate evidence |
| Multi-account | Research Engine owns specialist accounts; OmniRoute owns accounts for its supported operations |
| Specialist selection | `priority`, `round_robin`, `quota_aware`; capability eligibility, cooldown, quota and shared limits apply |
| Routing | Capability chosen by tool; all six search capabilities fan out to every enabled, configured, semantically independent routed provider under one deadline; no first-success or provider chains |
| Output | Ranked hits for search; source documents for reads; typed metadata/verification/graph records |
| Merge | All successful search lists retain provenance, conservative exact ID/URL dedup and plain RRF; one actual provider counts once per capability; validated rerank only when enabled, default off |
| Jobs | Persist inherently async specialist jobs; ordinary timeout returns partial results |
| Admin | Minimal sqladmin: accounts/connect/test, latest health/quota, request/replay, routing/rerank settings |
| Deployment | Single user/process, Python, SQLite WAL, loopback/tailnet, per-client bearer tokens |
| Public hosting | Public MCP/admin ingress, client OAuth AS and `/mcp/compat` remain deferred |
| Deferred | Excluded from the current queue; reopen only for the conditions in design §16 |
| Acceptance | Distinguish code, fixture evidence and live validation; blocked checks remain pending |

## Account ownership

Direct research-specialist credentials, OAuth sessions, eligibility and selection live in the engine.
Commodity credentials, account pools and same-provider balancing/failover live in OmniRoute.
The bridge stores only its own service credential and selected upstream operation mapping.
There is no mirrored commodity account database.

Same-provider account failover is limited to auth/rate/quota availability. It must not
replay account-independent plan, input, target, transient or internal failures across keys.
Multiple accounts must not become multiple independent provider votes in RRF.
Shared limits apply across accounts in the same provider quota group.
Unknown quota/reset data remains unknown; selection never assumes independent quotas.

## Evidence coverage

Search-like means `web_search`, `news_search`, `paper_search`, `paper_related`,
`developer_search` and `repo_search`. Successful peers survive failures/timeouts and
return partial coverage. Direct and OmniRoute access to the same upstream operation
is one source; covered commodity operations use an explicit OmniRoute provider and
never silently retry the frozen direct adapter. Specialists remain direct.

Sequential progression is limited to same-source reads, unresolved metadata IDs and
one async start. Verification, citation graphs and editorial checks fan out and aggregate
source assertions without RRF. Site maps fan out and aggregate URLs. Do not add fallback
architecture, scoring policy, classifiers or a control plane. [Design §6](design.md#6-routing)
defines execution; [roadmap](roadmap.md#evidence-coverage-correction) defines acceptance.

## Reuse and engineering constraints

Reuse compatible upstream code and preserve notices. Do not copy a model router's abstractions
because they already exist. Do not build a second control plane, generic plugin framework,
distributed queue, React admin or future schema hooks.

Providers differ by capability and evidence semantics; they are not interchangeable models.
Treat scope at operation level: Firecrawl search/fetch can bridge while site map/crawl stays direct.
Technical limits and entitlement remain explicit; the owner assesses provider terms.

## Work and validation

[cleanup.md](cleanup.md) lists observed shortcuts with required action and closure checks.
[code-audit.md](code-audit.md) records reproduced/static findings and verification limits.
[roadmap.md](roadmap.md) replaces the old docs-only P0–P4 plan.
Detailed research/01–07 is historical evidence, not permission to restore rejected features.
The original scope decision was documentation-only; subsequent changes on
`research-specialists` implemented and checked selected code/live operations.
[Roadmap](roadmap.md#implementation-and-acceptance-snapshot-2026-10-06) distinguishes
verified work from external acceptance blockers.
