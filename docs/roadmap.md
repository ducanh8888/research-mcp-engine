# Research Engine — Roadmap

Updated 2026-10-06. Baseline: `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`.
`main` is frozen; implementation continues on `research-specialists`.
This replaces the old P0–P4 plan. No phase is declared accepted from source inspection alone.

## Baseline evidence

| Area | Observed in baseline | Acceptance |
|---|---|---|
| Runtime/admin/auth/storage | Source, migrations, Docker/Compose and auth/MCP fixtures exist | Fresh install, container and actual tailnet clients pending |
| Commodity web/dev | Exa, Firecrawl, Tavily, Brave, Serper, Jina, DuckDuckGo, trafilatura and GitHub code/fixtures exist | Covered operations move to bridge; no new direct adapter work |
| Open/REST specialist | OpenAlex, Crossref, S2, arXiv, Scite REST, Consensus API, Elicit API code/fixtures exist | Harden and live-check existing work; do not rebuild it |
| Hosted MCP | At baseline client/OAuth infrastructure existed; normalized adapters were absent | Current Scite/Elicit search adapters are guarded; eligible live calls and Undermind schemas pending |
| Multi-account | N accounts, priority failover, cooldown/blocks/quota fields exist | Round-robin, quota-aware and shared-state correctness pending |
| Jobs/cache/merge | Implementations and deterministic fixtures existed | Firecrawl crawl completed after restart; cache/merge fixtures pass, broader live/client checks pending |
| Rerank | Baseline backends/tests and `benchmarks/results/local_cpu.json` existed | Current branch routes optional API rerank through one encrypted OmniRoute connection; local backends remain, default off |
| Experimental Deferred code | Disabled fusion/relationship helpers and unused dependency hooks exist | Outside acceptance; no expansion based on code presence |
| OmniRoute bridge | Absent at baseline | Explicit provider search/fetch/rerank bridge now implemented; per-operation live parity still pending |

The CPU artifact records eight authored queries with six candidates each. It is a recorded
local execution, not representative research evaluation or a newly rerun result.
The extended [code audit](code-audit.md) found additional contract/state/lifecycle defects
and reproduced eight behaviors with isolated original-source probes. That baseline
had no full suite or live evidence. The current snapshot below supersedes its
status assertions without rewriting the historical audit.

## Implementation and acceptance snapshot (2026-10-06)

Implementation commits on `research-specialists` now cover the core correctness,
versioned migrations, specialist account selector, locked container, guarded hosted
search adapters, explicit OmniRoute search/read bridge and ordinary-path optional
rerank. `uv run --frozen pytest -q`: **352 passed**; Ruff and whitespace checks passed.
The container is healthy on loopback/tailnet and real MCP calls to available
OmniRoute, open academic, GitHub and Firecrawl operations succeeded. See the
[deployment record](deployment.md#validation-record-2026-10-06) for the precise
observations. None of these facts alone accepts P0–P5 in full.

| Phase | Implemented and checked | Exit criteria still pending |
|---|---|---|
| P0.1 | HTTP public-contract regressions, migration upgrade fixtures, non-idempotent start protection, redaction, bounded outputs; frozen suite/container pass | C33 evidence-coverage correction and focused regressions; exhaustive live deadline/error fault injection |
| P0.2 | Explicit catalog, stable credentials, account Test separation, request-bound limiter, nondestructive route-aware query keys, cache/blob maintenance | C03 concurrent OAuth edge cases; historical request/job retention approval; live admin route/Reset check |
| P1 | One encrypted OmniRoute connection, explicit search/fetch/rerank; live DuckDuckGo/Exa search and Jina read | Brave/Serper upstream keys absent; upstream multi-account and provider quota failover not observable with API-scoped credential; fresh cache bypass unsupported |
| P2 | Priority/round-robin/quota-aware fixtures, shared cooldown and persistent observations, admin mode | Two real specialist accounts with actual auth/rate/quota availability retries |
| P3 | Academic/REST/GitHub semantics, conservative graph merging, live metadata/search/repo/map/crawl | Multi-seed/claim live coverage and paid Scite/Elicit/Consensus entitlements pending |
| P4 | Guarded Scite/Elicit hosted paper search and SDK fixtures; one Firecrawl job completed across restart | Undermind authenticated schemas, eligible OAuth/tool calls, HTTPS callback, live async review/deep jobs and independent owner/cancel checks pending |
| P5 | Locked container, auth rejection, MCP discovery/search/read/jobs on local/tailnet bind, optional rerank API check; remains default off | Independent tailnet peer, actual Claude Code/agentRT clients, off-tailnet denial, live MCP rerank toggle (setting change denied), representative rerank evaluation |

No phase is marked fully accepted while its stated live/client criteria remain
pending. These blockers do not prevent independently verified operations from running.

## Evidence coverage correction

The owner directive in [AGENTS.md](../AGENTS.md#owner-directive--evidence-coverage)
requires concurrent collection of independent evidence in one MCP request. C33 in
[cleanup](cleanup.md) is open and belongs to P0.1. Implement it before claiming search
acceptance in P1/P3/P5. The existing 352-test/live snapshot does not validate this new
contract; retain its evidence and add the following behavioral checks.

| Check | Required observation |
|---|---|
| All six search capabilities | Every enabled, configured, independent routed provider starts concurrently under one shared deadline; fast success and valid zero hits never skip peers; sequential search configuration cannot take effect |
| Actual source identity | Direct/OmniRoute aliases for the same provider/operation are rejected or normalized to one bridge entry; direct/hosted interfaces of one actual provider contribute one RRF list per capability; distinct independent sources remain included |
| Gateway failure | Failed covered OmniRoute operation contributes failed coverage without invoking its frozen direct adapter; successful direct specialists survive |
| Partial and cache | Success plus failure, unavailable eligible account or timeout preserves successful results/provenance and returns partial; reduced coverage never becomes a complete cache entry; all failed returns `NO_PROVIDER_AVAILABLE` |
| Same-provider accounts | Auth/rate/quota retries can produce one provider list; plan/input/target/transient/internal errors do not trigger an account chain; shared limits remain effective |
| Required sequential operations | Same-source reads progress until usable, metadata retries only unresolved IDs, and ambiguous async start never creates duplicate work |
| Typed aggregation and rerank | Verification/graph/editorial fanout preserves source assertions and conflicts without RRF; site maps aggregate URLs; exact dedup and plain RRF merge all successful search lists; disabled or invalid rerank cannot replace fused order |

Use controlled concurrent/error fixtures through the public MCP contract. Record live
coverage separately with at least two configured independent sources where available;
missing credentials leave live acceptance pending. Do not add a routing/scoring policy,
classifier or fallback framework to implement these checks.

## Work order

| Phase | Required scope | Exit criteria |
|---|---|---|
| **P0.1 Reproduce and repair correctness** | Establish frozen-lock checks/container first (C31/C32); fix C14/C15/C16/C18/C19/C20/C22/C24/C26/C27 and the open C33 evidence-coverage correction; establish safe upgrades (C28) before any schema change; retain regression cases for all eight reproduced behaviors | Real ORM block-state/fresh-versus-upgraded DB tests; public tool parameters reach upstream; one non-idempotent start submission; complete metadata coverage/unknown absence; usable sequential reads; mandatory concurrent search, actual-provider identity, truthful partial/cache status and scoped account retries satisfy the C33 matrix; conflicting evidence preserved; bounded responses/deadlines; terminal correlated/redacted request errors; lint, relevant tests and HTTP smoke actually pass |
| **P0.2 Consolidate and preserve upgrades** | Close C01–C09/C11–C13 plus C23/C25/C29/C30; extract only the necessary shared contracts/state/storage functions; preserve credentials, operator routes, restart and OAuth | Required imports fail visibly; locked SDK behavior justified; no guessed reset/false Test success/manual cache workaround; actual rate/concurrency limits; responsive async jobs; catalog refresh preserves operator choices; reference-safe retention/maintenance |
| **P1 Minimal OmniRoute bridge** | One bridge for explicit-provider search/fetch and optional API rerank; start with two search providers and one reader; inspect the actual installed OmniRoute version/API/credentials | Real MCP search/read returns normalized provenance; explicit provider cannot silently substitute; 401/429/quota/timeout and bridge outage are scoped correctly; commodity account ownership stays upstream; C10 cutover criteria pass for each migrated operation |
| **P2 Specialist multi-account** | Implement priority, round-robin and quota-aware selection for direct providers; admin setting; shared quota eligibility, unknown/stale quota, concurrent selection; minimal migrations only | Two specialist accounts exercise each mode; real auth/rate/quota availability failure retries within the provider correctly; plan/target/input/transient/internal errors do not traverse accounts; group cooldown prevents retrying another member; restart preserves credentials/availability; one provider contributes one ranked list |
| **P3 Specialist completion** | Harden existing academic/REST/GitHub adapters and typed paper/metadata/related/verify/graph/editorial outputs; close C17/C21; finish demonstrated direct operation gaps, including Firecrawl map/crawl contract | Related modes/all seeds and citation-text resolution are implemented or explicitly unsupported; merged graphs preserve strong-ID conflicts/provenance/endpoints/bounds; real open-provider calls and targeted fixtures pass; metadata resolves per ID; local handles survive restart; unavailable paid accounts remain explicitly pending |
| **P4 Hosted research MCP and async** | Scite MCP, Elicit MCP, Undermind; Consensus REST first, MCP only for a verified gap; exact upstream schemas, account OAuth/CIMD, source parsing, one recoverable job lifecycle | Eligible live accounts initialize/list/call; refresh/concurrency and same-session HTTPS callback pass; deep search/review/crawl job completes across restart; unknown start never duplicates work; ownership/cancel/failure verified |
| **P5 Private integration acceptance** | Wire optional rerank through actual engine path with diagnostics/secret ownership; validate private deployment and documented run/connect paths end-to-end | Container health and real MCP calls pass; actual Claude Code and reachable tailnet peer verified; available agentRT verified; auth rejection, independent provider fanout, account availability retries, partial/cache, handles and jobs tested; cloud/local rerank checks reported separately; rerank stays off until suitable evaluation |

Execute phases in order. Existing code can satisfy a phase after verification and corrections;
a phase is not permission to rewrite a working subsystem. External blocking of one provider
does not stop independent work, and does not turn the blocked check into PASS.

Cleanup ownership: P0.1 owns C14–C16/C18–C20/C22/C24/C26–C28/C31–C33;
P0.2 owns C01–C09/C11–C13/C23/C25/C29–C30; P3 owns C17/C21.
C10 closes per bridged operation in P1, with actual rerank wiring/acceptance in P5.
P2 and P4 extend corrected account/job paths; they do not postpone foundational fixes.

## Provider work queue

- Bridge covered generic search/fetch/API rerank operations listed in
  [design §5](design.md#5-provider-scope-and-bridge).
- Direct focus: OpenAlex, Crossref, S2, arXiv, Scite, Elicit, Consensus, Undermind, GitHub,
  and demonstrated map/crawl/specialized operation gaps.
- Specialist account modes belong in the engine. Commodity account modes belong in OmniRoute.
- Retain local PDF/trafilatura readers and optional local rerank without a cross-repo deployment dependency.
- New direct Perplexity/Linkup/SearchAPI/You.com/Google PSE/SearXNG/Ollama/Z.AI/AnySearch/Nimble
  adapters are removed from the work queue while OmniRoute covers the requested operation.

## Per-phase evidence

Record exact commit, command, environment, result and blockers with the implementing change.
Keep fixture tests, recorded artifacts, live upstream calls and actual clients separate.
No fabricated PASS count, no claims from discovery alone, and no hidden paid-plan prerequisite.

For each migrated bridge operation, record requested/returned provider IDs, filter semantics,
account behavior, quota errors and cache/fresh behavior. Retire only verified redundant paths;
a gateway gap is explicit pending work, not a silent fallback to frozen direct code.

## Definition of done

- Relevant phase scope and exit criteria actually pass.
- A fresh checkout starts through the documented CLI/Compose path using private configuration.
- The phase's provider/account/routes can be managed in admin without secret leakage.
- Required authentication, concurrent evidence coverage, account availability retries,
  partial results/cache, exact dedup, plain RRF and persistence checks pass.
- Required live/client checks with no available environment/account remain pending and block
  that acceptance claim; independent accepted operations remain usable.
- Docs and cleanup statuses reflect the resulting code. `main` remains frozen until the owner
  separately requests integration.

## Deferred

[Design §16](design.md#16-deferred) defines reopen conditions. Deferred items are not subsequent
automatic phases. Existing disabled experiments do not get schema/API hooks or new rollout work.
Public exposure and `/mcp/compat` are absent from this private release plan.
