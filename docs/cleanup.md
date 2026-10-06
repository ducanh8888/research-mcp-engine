# Required cleanup

Source-reviewed at `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`, extended 2026-10-05.
Baseline descriptions below are historical observations. Checked items are closed by
implementation and the cited fixture/live evidence summarized in the
[roadmap snapshot](roadmap.md#implementation-and-acceptance-snapshot-2026-10-06).
Unchecked items remain open or only partially verified; phase acceptance is separate.
[Code audit](code-audit.md) separates eight isolated source reproductions from static findings
and records environment limits. Phase ownership and priority are in [roadmap.md](roadmap.md).

These are observed shortcuts, inconsistent paths and scope leftovers. No authorship or motive
is inferred. Concurrent evidence collection, scoped account availability retries, sequential
reads/unresolved metadata/single async starts, partial results, exact-ID conflict guards,
encrypted token storage and restart polling are required behavior and remain.

## Remove or replace before expanding implementation

- [x] **C01 — Remove silent missing-module registration.**
  `providers/registry.py:build_registry` catches a missing provider package and continues;
  `mcp.adapters` is referenced but absent. Use explicit registration for shipped adapters
  and a visible pending inventory for unshipped ones. Required missing imports fail startup.
  Close when deletion of a required module fails clearly and unimplemented hosted adapters
  cannot be represented as connected/available.

- [x] **C02 — Remove unsupported SDK-era branches.**
  `providers/mcp/oauth.py` falls back to SDK 1.x callback tuples and probes for different
  private auth-flow shapes; `clients.py` inspects constructor support at runtime.
  The declared dependency is MCP ≥2.2,<3. Target the actual locked SDK contract and fail
  incompatibility explicitly. Close with OAuth callback/refresh fixtures and transport tests
  against that lock, without legacy guessing branches.

- [ ] **C03 — Verify and isolate the private OAuth lock shim.**
  `NonSerializingOAuthClientProvider` replaces `context.lock`, drives inherited private
  generators and overrides token requests. Source presence does not prove the shim is still
  required or cancellation-safe on the installed SDK. Measure same-account concurrency,
  refresh races and cancellation first. Remove obsolete overrides; retain a minimal isolated
  shim only for a reproduced upstream defect with a pinned regression test.
  Close only with that evidence; do not delete valid OAuth behavior blindly.

- [x] **C04 — Replace speculative quota recovery and stale account retries.**
  `router/execute.py:_availability` assigns one hour after zero quota and defaults exhaustion
  to 3600 seconds; `_call` keeps an eligibility snapshot while shared cooldown can change.
  Persist known reset/observations, label unknown-reset retry estimates explicitly, and recheck
  eligibility before each attempt. Propagate shared zero-quota state consistently.
  Close when unknown reset stays unknown and a failed shared-limit account prevents another
  member from being called in the same request.

- [x] **C05 — Replace dual credential-state vocabulary.**
  `_eligible` accepts both `ok` and `valid`; bootstrap, Test and success write `valid`,
  while the current contract uses `ok`. Normalize existing rows once, then use one vocabulary
  across model, admin, router and docs. Close with migration/restart coverage that preserves
  needs-auth, disabled and capability blocks.

- [x] **C06 — Remove Test's reset-before-check shortcut.**
  `Engine.test_account` clears cooldown/blocks and marks credentials valid before making a
  direct adapter call, outside the normal limiter path. A pending async-only test can therefore
  erase availability evidence without testing it. Keep Test and Reset separate; route an
  explicit account check through the normal limit/error/result path.
  Close when failed/pending checks never create false ready state, and Test does not bypass
  a shared limit or choose another account.

- [x] **C07 — Remove manual cache clearing as route correctness.**
  `admin/views.py:RoutingView` validates route edits but has no affected-query invalidation;
  `cache.py` keys omit route configuration. Invalidate query cache for changed routes/provider
  enablement/operation settings; leave document cache independent.
  Close when the first call after an edit uses the new route without manual Clear or a
  policy-version subsystem.

- [x] **C08 — Replace unexpected-error-to-provider-cooldown masking.**
  `Engine._call` converts every ordinary unexpected exception into a transient provider
  failure and can cool down an account for a programming error. Preserve successful peers,
  but distinguish internal adapter failure from a classified upstream/transport error.
  Close when internal faults have redacted diagnostics and explicit error coverage without
  changing authentication, entitlement or quota state.

- [x] **C09 — Remove unreviewed route expansion on registration.**
  `registry.py:default_routes` appends every compatible registered provider beyond `ORDER`;
  `Database.initialize` silently filters invalid seeded entries. Registration is not consent
  to invoke every new provider. Use explicit approved route seeds and validate invalid seeds;
  retain existing operator routes without overwriting them.
  Close when adding an adapter does not change an existing route or silently enable paid work.

- [x] **C11 — Remove Deferred placeholders from the active surface.**
  `Provider.Capability` includes `SITE_INTERACT` despite no implementation/tool;
  `pyproject.toml` reserves `postgres` and `browser` extras despite the private single-process
  scope. Remove unused placeholders/dependencies when code cleanup begins.
  Existing disabled weighted/hierarchical fusion and relationship experiments stay outside
  release scope; retain their tests/artifact history without treating them as a roadmap.
  Close with no unsupported capability/extra presented as implemented and no new future hooks.

- [x] **C12 — Replace split password creation paths.**
  `cli.initialize` writes bcrypt hashes, while `storage/crypto.py:hash_password` writes scrypt
  and the verifier supports both. Choose one creation path; existing hashes keep working
  through an explicit compatibility/migration boundary.
  Close when fresh init/admin login and existing-config login pass without lockout.

- [x] **C13 — Remove duplicate compact output aliases.**
  `Engine._payload` adds `h/t/u` alongside `handle/title/url` on every search item.
  These aliases are not in the current consumer contract and duplicate payload.
  Use one documented schema; check actual clients before removing any relied-on compatibility.
  Close when MCP structured/text output and clients use canonical fields consistently.

## Additional main-code corrections

- [x] **C14 — Unify persisted capability-block shape.**
  `storage/db.py:Account.blocked_capabilities` is a `MutableList` JSON list, while
  `_availability` assigns a reason dict and `test_account` assigns `{}`; admin Reset uses `[]`.
  Dict expansion also fails on a nonempty list. Choose one typed persisted shape and migrate
  existing values before changing writers/readers; keep reasons if required by the contract.
  Close with real ORM tests for plan denial, second denial, Test, Reset and restart, including
  existing rows. No internal exception may replace the original provider error.

- [x] **C15 — Remove automatic retry of non-idempotent starts.**
  `providers/http.py:request` retries POST on transport errors/408/5xx by default.
  `ElicitAPIProvider.start` inherits that default through `json_request`; marking an error
  `ambiguous_start` afterwards is too late. Firecrawl explicitly disables retries already.
  Make retry eligibility explicit at the transport boundary; a side-effecting start gets one
  submission unless a verified upstream idempotency contract makes another safe.
  Close when lost responses and 500s send at most one start POST, produce an unknown outcome,
  and cannot trigger account/provider resubmission. Retain safe read retries.

- [x] **C16 — Repair public tool-to-adapter request contracts.**
  `server/tools.py` exposes `max_depth`, `claim`, `year_from/year_to`; Firecrawl reads `depth`,
  S2 reads `statement`, and no shipped adapter consumes the two public year fields.
  Replace independent `locals()`/loose-dict conventions with validated capability requests
  and explicit adapter translation. Validate enums, ranges and required async input before
  invoking upstream; unsupported filters must be visible, never silently ignored.
  Close through actual MCP tool calls that inspect sent crawl depth/year filters and returned
  claim passages, plus malformed-input/no-upstream-call checks.

- [ ] **C17 — Complete the advertised related/metadata operation semantics.**
  OpenAlex/S2 `paper_related` consume only the first seed and always return similar papers,
  ignoring public `mode=citing|cited`. Metadata's `citation` alternative is forwarded to
  ID-only lookup paths; plain citation text has no explicit bibliographic resolver there.
  Implement supported modes/all seeds and a conservative citation resolver, or narrow the
  advertised contract with explicit unsupported results. Never turn a top search hit into
  an established identity without identifier/field evidence.
  Close with multi-seed/mode tests through MCP, known IDs, plain citation text, ambiguity,
  and source-attributed per-input outcomes.

- [x] **C18 — Separate metadata data aggregation from attempt coverage.**
  `_execute` replaces the first successful `Outcome.result` with the whole batch and deletes
  later successful outcomes. Records from a second provider survive while its coverage vanishes.
  It also labels every unresolved ID `not_found`, including after another provider times out.
  Aggregate records independently; preserve every attempted provider and per-ID evidence.
  Reserve definitive absence for completed applicable lookups; timeout/unavailable stays unknown.
  Close when two providers resolve different IDs and both appear in coverage, while unfinished
  IDs remain distinguishable from definitive misses in a partial response.

- [x] **C19 — Validate payloads before declaring success or ending sequential reads.**
  `Result` allows all payloads to be absent/extra, and `_call` checks only its class.
  Sequential read then stops on `Result(document=None)` and returns complete/ok with no document.
  Define capability-specific success/empty/invalid criteria at one boundary, with explicit
  known fields (including scholarly hit `id`) rather than relying on arbitrary extras.
  Close when empty reads try the next provider, malformed outputs are adapter failures,
  and a legitimate successful search with zero hits remains a valid empty result.

- [x] **C20 — Remove match-wins verification aggregation.**
  `_payload(CITATION_VERIFY)` returns `match` whenever any source matches, even if another
  explicitly mismatches. Preserve both assertions and expose a conflict/unknown aggregate
  according to the documented schema; neither provider order nor rank resolves disagreement.
  Close with match+mismatch, unknown+match, and all-unknown cases through the public output.
  Bibliographic matching, claim passages and citation tallies remain separate.

- [x] **C21 — Replace raw graph concatenation with conservative identity merging.**
  `_payload(CITATION_GRAPH)` concatenates provider nodes/edges without deduplication or remapping
  endpoints. Equivalent papers/edges can appear more than once under provider-specific IDs.
  Reuse strong-ID conflict guards, remap endpoints, retain source assertions and bound the
  combined graph. Do not add fuzzy identity inference or a new graph framework.
  Close with overlapping DOI graphs, conflicting identifiers, valid endpoints, distinct
  source assertions and a combined-budget truncation case.

- [x] **C22 — Apply actual runtime deadlines and response bounds.**
  Tools/router use a 30-second default and accept up to 120 seconds; design specifies 40 and
  a 5–50 second clamp. `Settings.max_response_bytes` has no runtime consumer. Individual
  document/HTTP limits do not bound metadata, graph or completed-job envelopes.
  Centralize the ordinary deadline rule and enforce serialized response size across capabilities,
  using explicit pagination/truncation or errors rather than silently dropping attribution.
  Close with deadline cancellation and oversized metadata/graph/job outputs; both structured
  and text results obey the same contract. Keep job poll wait separate from execution deadline.

- [x] **C23 — Replace disconnected rate/concurrency options with one working limiter.**
  Scholar adapters declare `rate_limit_rps` and `concurrency`, but `_call` consumes a different
  `min_interval_s` option/hardcoded table. Its lock spaces adapter calls, not each outgoing
  request, and does not cap concurrent in-flight calls; graph/related adapters issue subrequests.
  Apply documented limits at the actual HTTP/MCP operation boundary and share state where
  quota groups require it. Remove unused knobs/duplicate spacing; no distributed limiter.
  Close with controlled-clock subrequest/concurrent-call tests showing the configured rate,
  concurrency and shared-account behavior; Test and job polling use the same applicable limits.

- [x] **C24 — Stop retrying account-independent errors across every account.**
  `_call` loops through all accounts after every `ProviderError`, including `BAD_REQUEST`
  and `TARGET`. Three accounts repeat an identical missing-target request three times.
  Make error scope explicit: credential/quota failures can select another eligible account;
  invalid request or provider target absence goes to the appropriate provider/input outcome.
  Close when target/bad-input failures do not replay across keys, while auth/rate/quota retries
  and capability-block semantics follow the error table. The earlier C24 closure does not
  establish the broader account retry restriction now required by C33.

- [x] **C25 — Keep job storage off the event loop.**
  `JobRunner.create/get/cancel/_poll_once` directly open synchronous DB sessions or call
  synchronous `_load/_persist`; the router/auth paths already offload DB work.
  Isolate short transactions and offload them consistently without sharing sessions between
  threads. Preserve one-process restart/cancel semantics; no worker system/async-DB rewrite.
  Close with a deliberately delayed DB operation while an unrelated MCP request remains
  responsive, plus ownership, concurrent cancel and restart checks.

- [x] **C26 — Finalize every recorded request on internal failure/cancellation.**
  `Engine.execute` records a running request but finalizes only success or `ToolError`.
  Unexpected merge/cache/storage/adapter errors escape to the server's generic response and
  leave the row running; cancellation has the same missing-finalization path.
  Keep a request lifecycle boundary with terminal status, request ID and redacted diagnostics.
  If storage itself fails, surface that limitation clearly; never invent a successful audit row.
  Close with internal fault/cancel injection, correlated MCP error output and persisted terminal
  state, preserving unknown-start reconciliation and peer results.

- [ ] **C27 — Unify redaction before persistence and public/admin errors.**
  `_call` redacts only top-level string credentials. Test emits `str(ProviderError)` directly;
  `JobRunner._error_text` retains it in `last_error`, including upstream crawl error text.
  Apply one redaction boundary for attempts, request results, jobs and admin output, including
  nested credentials, URLs and account-scoped OAuth tokens. Do not rely on adapter good behavior.
  Close with synthetic secret-bearing failures on call/Test/poll/cancel: no secret appears in
  storage, logs or MCP/admin output, while useful error kind/request correlation remains.

- [x] **C28 — Make migrations independent of today's ORM schema.**
  `storage/migrations/v0001–v0003` create tables from current `Base.metadata` with `checkfirst`.
  Editing an ORM field therefore changes fresh-install historical DDL but does not upgrade an
  existing table. Preserve versioned schema definitions and explicit forward alterations/backfills.
  Close with old-schema upgrade versus fresh-install schema/data parity, especially C14/C05,
  without deleting the DB or recreating existing operator data.

- [x] **C29 — Reconcile shipped catalog facts without overwriting operator choices.**
  `Database.initialize` inserts provider capabilities only when the row is absent; adapter
  capability changes leave an existing admin catalog stale. Route validation reads that catalog.
  Separate shipped capability facts from operator enablement/options/routes and update only
  owned facts during upgrade. Close when an adapter capability change appears after restart,
  removed support is handled visibly, and operator routes/credentials/options are preserved.

- [ ] **C30 — Wire cache/blob maintenance and define retention.**
  Expired cache rows are removed only when that key is read; `BlobStore.cleanup` has no caller.
  Clear deletes cache rows but leaves unreferenced blobs. Request/job result retention is unbounded.
  Provide one bounded local maintenance path with explicit retention, reference-safe blob deletion
  and dry-run/reporting. Preserve identities and active/recoverable job data; no scheduler service.
  Close with expiry/clear/orphan fixtures and retained shared-blob/live-job references.

- [x] **C31 — Repair verification that bypasses the public contract.**
  `tests/test_mcp_e2e.py` uses `json.loads` in three tests without importing `json`.
  Direct adapter fixtures use `depth`/`statement`, allowing C16's public-field mismatch to escape.
  Repair these tests and run frozen-lock Ruff/pytest; add public-contract regression cases for
  the reproduced defects and a small required CI check. Test behavior, not implementation copies.
  Close only with actual lint/test results and recorded blockers; isolated probes are not a suite PASS.

- [x] **C32 — Build the container from the verified lock.**
  `Dockerfile` copies `pyproject.toml/src` and runs `pip install .`, ignoring the committed
  `uv.lock`. Container dependency resolution can differ from frozen local verification.
  Use a reproducible locked install for the runtime image and verify CLI/assets/startup there.
  Close with a clean container build whose installed runtime versions match the lock and a
  real HTTP discovery/tool smoke test. Keep paid/live checks separate.

## Current branch evidence-coverage correction

- [x] **C33 — Enforce concurrent evidence coverage and truthful partial status.**
  Static review at `27d28ee0747f98634756d076ae3c1d1c9df8255c` found that
  `router/execute.py:Engine._route` accepts sequential search configuration;
  `_execute` then stops on first success and labels ordinary fanout with failed peers
  complete unless pending/metadata conditions apply. `cache.py:Cache.put_query`
  caches complete output, so reduced evidence can be reused as complete. `_call`
  still traverses accounts after plan/transient/internal errors. These are source
  observations, not a newly run regression suite.

  Enforce mandatory fanout for all six search capabilities and applicable
  verification/graph/editorial providers. Run every enabled, configured independent
  routed source concurrently under one deadline; preserve all successful results,
  provenance and failed/unfinished coverage. A peer error, unavailable account or
  timeout returns partial; invalidate previously misclassified complete query entries.
  Normalize search lists with conservative exact dedup and plain RRF, then validated
  rerank only when enabled. Typed assertions aggregate without RRF.

  Validate execution mode and actual-provider identity per capability at save, upgrade and
  runtime. Correct legacy sequential search routes without losing provider selection,
  credentials or unrelated operator edits. Covered commodity operations use one
  explicit OmniRoute entry: no direct/bridge duplicate vote or gateway-triggered retry
  of frozen direct code. Preserve direct specialist and demonstrated operation gaps.
  Account failover is only same-provider auth/rate/quota availability and contributes
  one result list; other errors must not walk the account pool.

  Keep sequential same-source reads, unresolved metadata and exactly one safe async
  start. Do not introduce provider chains, fallback architecture, scoring policy,
  classifiers or control-plane machinery. Close with the focused public-contract
  [regression matrix](roadmap.md#evidence-coverage-correction); record two independent
  live sources separately where configured, leaving unavailable live checks pending.

C33 correction verified 2026-10-06: 38 focused real HTTP MCP fixtures cover all six
mandatory search fanouts, simultaneous start, independent-provider identity,
failed/skipped/timeout partial coverage, no reduced-coverage caching, gateway
failure without direct fallback, account retry scope, sequential exceptions,
typed assertions/site maps and default-off/validated rerank. Full frozen suite:
391 passed. A live MCP web search collected DuckDuckGo, Exa and Serper results
with Brave failed and `status=partial`; a fresh academic search collected four
successful sources while an unconfigured peer was skipped with partial status.
This verifies the C33 correction for the available sources, not upstream account
pool failover or paid-provider eligibility.

## Retire after verified bridge cutover

- [ ] **C10 — Retire duplicate commodity execution and API rerank credential paths per operation.**
  `providers/web/{brave,serper,tavily,exa,firecrawl,jina,duckduckgo}.py` and
  `merge/rerank.py:_BACKENDS/_http_ranking` duplicate covered upstream operations.
  API rerank currently reads inline options/environment keys outside the account secret path.
  Ordinary `Engine._payload` does not call rerank, so module tests alone do not prove integration.
  Build/verify the bridge and route actual calls through it before retiring redundant code.
  Covered direct operations must not remain active fallback entries or duplicate votes.
  A gateway failure is explicit reduced coverage, never permission to reactivate them.
  Preserve Firecrawl map/crawl and any verified specialized gaps, local readers/rerank,
  encrypted accounts, notices and frozen checkpoint history.
  Close per migrated operation with live parity, correct provenance/filter/error behavior,
  upstream multi-account checks and no mirrored commodity credential store.
  Local rerank wiring/default-off behavior is finalized in P5.

## Status and remaining closure work (2026-10-06)

**Verified locally with real ORM/HTTP fixtures and frozen checks:** C01 (explicit
imports), C02 (locked SDK), C04–C06 (shared account state/Test), C07 (route-key
cache correctness without deleting historical rows), C08–C09, C11–C16,
C18–C26, C28–C29 and C31–C32. Baseline defect descriptions above
remain for auditability; they are not assertions about current code. C22 now
carries one 5–50 second deadline across the entire request including cache/merge,
reserving aggregation time for partial outcomes and returning bounded errors.
C17 scholarly related
modes, citation resolver and graph handling pass adapter fixtures, but public
multi-seed/ambiguity integration is not exhaustively checked, so it stays open.

**Open:** C03 needs stale concurrent 401/full reauthorization and force-connect
refresh-race evidence on eligible hosted accounts. C10 is operation-by-operation:
OmniRoute DuckDuckGo/Exa search and Jina read worked live, API rerank helper
worked live and its ordinary path passed fixtures, but Brave/Serper upstream
accounts are unconfigured; no upstream account-pool failover or filter parity
was verified. Preserve retained direct code/credential data pending each operation's
cutover evidence, while removing covered direct operations from active fallback/duplicate
routes under C33. The 38 focused public MCP fixtures and live independent-source
search recorded above now verify C33 for configured operations. C27 has synthetic
call/Test and job poll/cancel redaction fixtures; complete log/admin/OAuth
failure injection is pending. C30 prunes cache/orphan blobs safely but only
reports aged request/job rows: historical row deletion was denied pending
an explicitly authorized retention policy. No deletion is attempted.

## Required retained behavior

- Concurrent independent provider evidence, same-provider auth/rate/quota availability
  retries, required sequential source/metadata/start progression, deadline cancellation
  and visible partial coverage.
- No repeated upstream start after an ambiguous result.
- Distinct strong-ID evidence, durable handles and source-attributed records.
- Real refresh/state/session checks, auth rejection and secret redaction.
- Rerank failure preserving fused order with visible diagnostics.

A safe read retry, account availability retry or compatibility branch is not automatically
a defect. Search-provider fallback chains violate the owner directive. The entries above
identify the concrete semantics that must change or be verified.
