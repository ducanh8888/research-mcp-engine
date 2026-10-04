# Research Engine — Roadmap

Updated 2026-10-05. Baseline: `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`.
`main` is frozen; implementation continues on `research-specialists`.
This replaces the old P0–P4 plan. No phase is declared accepted from source inspection alone.

## Baseline evidence

| Area | Observed in baseline | Acceptance |
|---|---|---|
| Runtime/admin/auth/storage | Source, migrations, Docker/Compose and auth/MCP fixtures exist | Fresh install, container and actual tailnet clients pending |
| Commodity web/dev | Exa, Firecrawl, Tavily, Brave, Serper, Jina, DuckDuckGo, trafilatura and GitHub code/fixtures exist | Covered operations move to bridge; no new direct adapter work |
| Open/REST specialist | OpenAlex, Crossref, S2, arXiv, Scite REST, Consensus API, Elicit API code/fixtures exist | Harden and live-check existing work; do not rebuild it |
| Hosted MCP | Client/OAuth infrastructure exists; `providers/mcp/adapters.py` absent | Scite/Elicit/Undermind normalized adapters pending |
| Multi-account | N accounts, priority failover, cooldown/blocks/quota fields exist | Round-robin, quota-aware and shared-state correctness pending |
| Jobs/cache/merge | Implementations and deterministic fixtures exist | Review actual contracts, restart and integration acceptance pending |
| Rerank | Backends/tests and `benchmarks/results/local_cpu.json` exist | Ordinary search path does not call rerank; integration pending; default off |
| Experimental Deferred code | Disabled fusion/relationship helpers and unused dependency hooks exist | Outside acceptance; no expansion based on code presence |
| OmniRoute bridge | Absent | P1 |

The CPU artifact records eight authored queries with six candidates each. It is a recorded
local execution, not representative research evaluation or a newly rerun result.
The extended [code audit](code-audit.md) found additional contract/state/lifecycle defects
and reproduced eight behaviors with isolated original-source probes. It did not execute
the full application suite or live provider calls. All cleanup items remain open.

## Work order

| Phase | Required scope | Exit criteria |
|---|---|---|
| **P0.1 Reproduce and repair correctness** | Establish frozen-lock checks/container first (C31/C32); fix C14/C15/C16/C18/C19/C20/C22/C24/C26/C27; establish safe upgrades (C28) before any schema change; retain regression cases for all eight reproduced behaviors | Real ORM block-state/fresh-versus-upgraded DB tests; public tool parameters reach upstream; one non-idempotent start submission; complete metadata coverage/unknown absence; usable read fallback; conflicting evidence preserved; bounded responses/deadlines; terminal correlated/redacted request errors; lint, relevant tests and HTTP smoke actually pass |
| **P0.2 Consolidate and preserve upgrades** | Close C01–C09/C11–C13 plus C23/C25/C29/C30; extract only the necessary shared contracts/state/storage functions; preserve credentials, operator routes, restart and OAuth | Required imports fail visibly; locked SDK behavior justified; no guessed reset/false Test success/manual cache workaround; actual rate/concurrency limits; responsive async jobs; catalog refresh preserves operator choices; reference-safe retention/maintenance |
| **P1 Minimal OmniRoute bridge** | One bridge for explicit-provider search/fetch and optional API rerank; start with two search providers and one reader; inspect the actual installed OmniRoute version/API/credentials | Real MCP search/read returns normalized provenance; explicit provider cannot silently substitute; 401/429/quota/timeout and bridge outage are scoped correctly; commodity account ownership stays upstream; C10 cutover criteria pass for each migrated operation |
| **P2 Specialist multi-account** | Implement priority, round-robin and quota-aware selection for direct providers; admin setting; shared quota eligibility, unknown/stale quota, concurrent selection; minimal migrations only | Two specialist accounts exercise each mode; real auth/rate/plan failure falls back correctly; group cooldown prevents retrying another member; restart preserves credentials/availability; one provider contributes one ranked list |
| **P3 Specialist completion** | Harden existing academic/REST/GitHub adapters and typed paper/metadata/related/verify/graph/editorial outputs; close C17/C21; finish demonstrated direct operation gaps, including Firecrawl map/crawl contract | Related modes/all seeds and citation-text resolution are implemented or explicitly unsupported; merged graphs preserve strong-ID conflicts/provenance/endpoints/bounds; real open-provider calls and targeted fixtures pass; metadata resolves per ID; local handles survive restart; unavailable paid accounts remain explicitly pending |
| **P4 Hosted research MCP and async** | Scite MCP, Elicit MCP, Undermind; Consensus REST first, MCP only for a verified gap; exact upstream schemas, account OAuth/CIMD, source parsing, one recoverable job lifecycle | Eligible live accounts initialize/list/call; refresh/concurrency and same-session HTTPS callback pass; deep search/review/crawl job completes across restart; unknown start never duplicates work; ownership/cancel/failure verified |
| **P5 Private integration acceptance** | Wire optional rerank through actual engine path with diagnostics/secret ownership; validate private deployment and documented run/connect paths end-to-end | Container health and real MCP calls pass; actual Claude Code and reachable tailnet peer verified; available agentRT verified; auth rejection, failover, partial/cache, handles and jobs tested; cloud/local rerank checks reported separately; rerank stays off until suitable evaluation |

Execute phases in order. Existing code can satisfy a phase after verification and corrections;
a phase is not permission to rewrite a working subsystem. External blocking of one provider
does not stop independent work, and does not turn the blocked check into PASS.

Cleanup ownership: P0.1 owns C14–C16/C18–C20/C22/C24/C26–C28/C31–C32;
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
- Required authentication, failover, partial results, dedup and persistence checks pass.
- Required live/client checks with no available environment/account remain pending and block
  that acceptance claim; independent accepted operations remain usable.
- Docs and cleanup statuses reflect the resulting code. `main` remains frozen until the owner
  separately requests integration.

## Deferred

[Design §16](design.md#16-deferred) defines reopen conditions. Deferred items are not subsequent
automatic phases. Existing disabled experiments do not get schema/API hooks or new rollout work.
Public exposure and `/mcp/compat` are absent from this private release plan.
