# Required cleanup

Source-reviewed at `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`, 2026-10-05.
All checkboxes below remain open. This pass marks code work; it does not claim those fixes
were implemented or runtime failures reproduced. Phase ownership is in [roadmap.md](roadmap.md).

These are observed shortcuts, inconsistent paths and scope leftovers. No authorship or motive
is inferred. Normal typed fallback, partial results, exact-ID conflict guards, encrypted token
storage and restart polling are required behavior and remain.

## Remove or replace before expanding implementation

- [ ] **C01 — Remove silent missing-module registration.**
  `providers/registry.py:build_registry` catches a missing provider package and continues;
  `mcp.adapters` is referenced but absent. Use explicit registration for shipped adapters
  and a visible pending inventory for unshipped ones. Required missing imports fail startup.
  Close when deletion of a required module fails clearly and unimplemented hosted adapters
  cannot be represented as connected/available.

- [ ] **C02 — Remove unsupported SDK-era branches.**
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

- [ ] **C04 — Replace speculative quota recovery and stale account retries.**
  `router/execute.py:_availability` assigns one hour after zero quota and defaults exhaustion
  to 3600 seconds; `_call` keeps an eligibility snapshot while shared cooldown can change.
  Persist known reset/observations, label unknown-reset retry estimates explicitly, and recheck
  eligibility before each attempt. Propagate shared zero-quota state consistently.
  Close when unknown reset stays unknown and a failed shared-limit account prevents another
  member from being called in the same request.

- [ ] **C05 — Replace dual credential-state vocabulary.**
  `_eligible` accepts both `ok` and `valid`; bootstrap, Test and success write `valid`,
  while the current contract uses `ok`. Normalize existing rows once, then use one vocabulary
  across model, admin, router and docs. Close with migration/restart coverage that preserves
  needs-auth, disabled and capability blocks.

- [ ] **C06 — Remove Test's reset-before-check shortcut.**
  `Engine.test_account` clears cooldown/blocks and marks credentials valid before making a
  direct adapter call, outside the normal limiter path. A pending async-only test can therefore
  erase availability evidence without testing it. Keep Test and Reset separate; route an
  explicit account check through the normal limit/error/result path.
  Close when failed/pending checks never create false ready state, and Test does not bypass
  a shared limit or choose another account.

- [ ] **C07 — Remove manual cache clearing as route correctness.**
  `admin/views.py:RoutingView` validates route edits but has no affected-query invalidation;
  `cache.py` keys omit route configuration. Invalidate query cache for changed routes/provider
  enablement/operation settings; leave document cache independent.
  Close when the first call after an edit uses the new route without manual Clear or a
  policy-version subsystem.

- [ ] **C08 — Replace unexpected-error-to-provider-cooldown masking.**
  `Engine._call` converts every ordinary unexpected exception into a transient provider
  failure and can cool down an account for a programming error. Preserve successful peers,
  but distinguish internal adapter failure from a classified upstream/transport error.
  Close when internal faults have redacted diagnostics and explicit error coverage without
  changing authentication, entitlement or quota state.

- [ ] **C09 — Remove unreviewed route expansion on registration.**
  `registry.py:default_routes` appends every compatible registered provider beyond `ORDER`;
  `Database.initialize` silently filters invalid seeded entries. Registration is not consent
  to invoke every new provider. Use explicit approved route seeds and validate invalid seeds;
  retain existing operator routes without overwriting them.
  Close when adding an adapter does not change an existing route or silently enable paid work.

- [ ] **C11 — Remove Deferred placeholders from the active surface.**
  `Provider.Capability` includes `SITE_INTERACT` despite no implementation/tool;
  `pyproject.toml` reserves `postgres` and `browser` extras despite the private single-process
  scope. Remove unused placeholders/dependencies when code cleanup begins.
  Existing disabled weighted/hierarchical fusion and relationship experiments stay outside
  release scope; retain their tests/artifact history without treating them as a roadmap.
  Close with no unsupported capability/extra presented as implemented and no new future hooks.

- [ ] **C12 — Replace split password creation paths.**
  `cli.initialize` writes bcrypt hashes, while `storage/crypto.py:hash_password` writes scrypt
  and the verifier supports both. Choose one creation path; existing hashes keep working
  through an explicit compatibility/migration boundary.
  Close when fresh init/admin login and existing-config login pass without lockout.

- [ ] **C13 — Remove duplicate compact output aliases.**
  `Engine._payload` adds `h/t/u` alongside `handle/title/url` on every search item.
  These aliases are not in the current consumer contract and duplicate payload.
  Use one documented schema; check actual clients before removing any relied-on compatibility.
  Close when MCP structured/text output and clients use canonical fields consistently.

## Retire after verified bridge cutover

- [ ] **C10 — Retire duplicate commodity execution and API rerank credential paths per operation.**
  `providers/web/{brave,serper,tavily,exa,firecrawl,jina,duckduckgo}.py` and
  `merge/rerank.py:_BACKENDS/_http_ranking` duplicate covered upstream operations.
  API rerank currently reads inline options/environment keys outside the account secret path.
  Ordinary `Engine._payload` does not call rerank, so module tests alone do not prove integration.
  Build/verify the bridge and route actual calls through it before retiring redundant paths.
  Preserve Firecrawl map/crawl and any verified specialized gaps, local readers/rerank,
  encrypted accounts, notices and frozen checkpoint history.
  Close per migrated operation with live parity, correct provenance/filter/error behavior,
  upstream multi-account checks and no mirrored commodity credential store.
  Local rerank wiring/default-off behavior is finalized in P5.

## Required retained behavior

- Ordinary typed provider/account fallback, deadline cancellation and visible partial coverage.
- No repeated upstream start after an ambiguous result.
- Distinct strong-ID evidence, durable handles and source-attributed records.
- Real refresh/state/session checks, auth rejection and secret redaction.
- Rerank failure preserving fused order with visible diagnostics.

A broad `except`, fallback or compatibility branch is not automatically a defect.
The entries above identify the concrete semantics that must change or be verified.
