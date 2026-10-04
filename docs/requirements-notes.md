# Research Engine — Requirements Notes

Current owner decisions, updated 2026-10-05.
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
| Immediate task | Clean docs/roadmap, audit main-code defects and mark removals/corrections before further implementation |
| OmniRoute coverage | Defer new direct work on covered search/fetch/API rerank operations; use one small bridge |
| Specialist work | Build/harden operations OmniRoute does not provide: academic, hosted research MCP, GitHub and site map/crawl gaps |
| Existing adapters | Preserve the checkpoint; retire redundant active paths only after equivalent bridge behavior is verified |
| Multi-account | Research Engine owns specialist accounts; OmniRoute owns accounts for its supported operations |
| Specialist selection | `priority`, `round_robin`, `quota_aware`; capability eligibility, cooldown, quota and shared limits apply |
| Routing | Capability chosen by tool; ordered providers with `fanout|sequential`; no imported combo/policy engine |
| Output | Ranked hits for search; source documents for reads; typed metadata/verification/graph records |
| Merge | Conservative exact ID/URL dedup, plain RRF, optional rerank off by default |
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

Multiple accounts must not become multiple independent provider votes in RRF.
Shared limits apply across accounts in the same provider quota group.
Unknown quota/reset data remains unknown; selection never assumes independent quotas.

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
No code/live fixes are claimed by this documentation-only scope update.
