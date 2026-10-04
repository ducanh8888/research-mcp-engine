# 03 — OmniRoute & 9router: reuse analysis

> Historical investigation, recorded before the specialist/OmniRoute bridge pivot.
> Current authority: [design v4](../design.md), [roadmap](../roadmap.md) and
> [cleanup](../cleanup.md). Recommendations, pricing, protocol and account observations
> below are dated evidence, not current requirements or live validation.

Status: research, 2026-09-29. Inputs: shallow clones (`--depth 1`)
- `diegosouzapw/OmniRoute` @ `666ea59` (v3.8.52, TypeScript, Next.js 16, MIT, © 2026 diegosouzapw)
- `decolua/9router` @ `f01fb90` (v0.5.91 app, plain JS ESM, Next.js 16, MIT, © 2024-2026 decolua and contributors)

Paths below are relative to each repo root. `OR:` = OmniRoute, `9R:` = 9router.

## TL;DR

- **Lineage.** OmniRoute started as a fork of 9router and was rewritten in TypeScript (OR `README.md` §acknowledgements).
  Both use the same concepts: provider, connection (= our *account*), combo (= our *chain*), `open-sse/` engine, and a
  Next.js dashboard with API routes under `src/app/api`.
- **Logic.** Use **9router as the skeleton** because its code is small and readable: `accountFallback.js` (≈250 LOC), `combo.js`
  (≈600 LOC) and `src/sse/services/auth.js` (≈360 LOC) cover chains, cooldown/backoff, selection and per-model locks.
  **Add OmniRoute's hardened parts**: the 4-state provider circuit breaker, the error taxonomy with signal lists,
  retry-hint parsing, terminal account states, the `QuotaInfo` window model, quota fetchers for **Firecrawl and Tavily**,
  reset-aware scoring, the search-provider registry with a normalized result schema, and the search cache with
  request coalescing. OmniRoute's `accountFallback.ts` is 2.5k LOC and `auth.ts` is 3.6k LOC, mostly
  LLM/provider special cases. Port algorithms from them, not files.
- **Research fan-out.** 9router's fusion `collectPanel()` (quorum + straggler grace + hard timeout) fits our
  "wide fan-out, wait, fuse" call pattern almost exactly. Port it as the core fan-out primitive.
- **UI.** See §3.
- **Exclude.** MITM, TLS/CLI fingerprint stealth, bulk token import, web-session scraping, free-proxy rotation,
  token compression, the translator and every LLM-format feature (§4).

---

## 1. Architecture overview

### 1.1 9router

| Layer | Location | Notes |
|---|---|---|
| HTTP entry | `src/app/api/v1/*` (Next route handlers; `/v1/*` is rewritten to `/api/v1/*` in `next.config.mjs`) | OpenAI-compatible gateway. The dashboard/admin APIs sit alongside it in `src/app/api/*` |
| App glue | `src/sse/handlers/chat.js` | Parses the request, expands combos (`handleSingleModelChat`), runs the **account fallback loop** |
| Account routing | `src/sse/services/auth.js` | `getProviderCredentials()` (filter + strategy, guarded by a global promise mutex), `markAccountUnavailable()`, `clearAccountError()` |
| Engine | `open-sse/` (provider-agnostic, usable standalone) | `services/combo.js` (chains, round-robin, capability reorder, fusion), `services/accountFallback.js` (cooldown math, model locks), `config/errorConfig.js` (declarative `ERROR_RULES`), `services/usage/*` (per-provider quota fetchers), `services/tokenRefresh*`, `executors/*`, `translator/*` |
| Storage | `src/lib/db/` | SQLite with an adapter fallback chain (`driver.js`: bun:sqlite → better-sqlite3 → node:sqlite → sql.js). Declarative schema in `src/lib/db/schema.js`, additive auto-sync, repos in `src/lib/db/repos/*`. Path from `src/lib/db/paths.js` (`DATA_DIR` or `~/.9router`) |
| State | `src/store/*` (zustand, UI only) | — |

**Data model** (`src/lib/db/schema.js`):
- `providerConnections(id, provider, authType, name, email, priority, isActive, data JSON, createdAt, updatedAt)`.
  Everything else lives inside the `data` JSON: `apiKey`, `accessToken`, `refreshToken`, `expiresAt`, `testStatus`,
  `lastError`, `errorCode`, `lastErrorAt`, `backoffLevel`, `rateLimitedUntil`, `lastUsedAt`, `consecutiveUseCount`,
  `providerSpecificData`, and flat **per-model lock fields `modelLock_<model>` / `modelLock___all`** holding ISO expiry
  timestamps.
- `combos(id, name UNIQUE, kind, models JSON)`. `models` is an ordered list of `"provider/model"` strings. A combo
  name is used in place of a model name.
- `settings(id=1, data JSON)`, a singleton. Routing knobs: `fallbackStrategy` (`fill-first` | `round-robin`),
  `stickyRoundRobinLimit`, `providerStrategies{<provider>: {fallbackStrategy, stickyRoundRobinLimit, rotateStrategy, proxyPoolId}}`,
  `comboStrategy` (`fallback` | `round-robin` | `fusion`), `comboStrategies{<combo>: {fallbackStrategy, judgeModel, fusionTuning}}`,
  `comboStickyRoundRobinLimit`. Export/import helpers: `settingsRepo.js::exportSettings`.
- `providerNodes` (custom OpenAI-compatible endpoints), `proxyPools`, `apiKeys` (client keys), `kv(scope,key,value)`,
  `usageHistory` (per request: provider, model, connectionId, tokens, cost, status), `usageDaily(dateKey, data)`,
  `requestDetails(id, ts, provider, model, connectionId, status, data JSON)`.
- Providers are static registry files (`open-sse/providers/registry/*.js`, auto-generated index), not DB rows.

### 1.2 OmniRoute

| Layer | Location | Notes |
|---|---|---|
| HTTP entry | `src/app/api/v1/*` plus ≈100 admin API groups under `src/app/api/*` | No global middleware. Each route runs CORS → Zod → auth → policy → handler |
| App glue | `src/sse/handlers/chat.ts`, `chatHelpers.ts` (breaker wiring), `src/sse/services/auth.ts` (3,622 LOC: selection, cooldown persistence, terminal states, session affinity, leases) | — |
| Engine | `open-sse/` workspace: `handlers/` (chat, **search**, **webFetch**, **rerank**, embeddings…), `services/` (≈300 files: `combo.ts` + `combo/*` 60 modules, `accountFallback.ts` + `accountFallback/*`, `errorClassifier.ts`, quota fetchers, `searchCache.ts`, `fusion.ts`, `autoCombo/*`), `config/` (`constants.ts` PROVIDER_PROFILES, `searchRegistry.ts`, `providerErrorRules.ts`, `upstreamStatusRestatement.ts`), `mcp-server/` | — |
| Domain | `src/domain/*` | `policyEngine.ts` (combines fallback, cost, lockout and breaker into one verdict), `fallbackPolicy.ts`, `costRules.ts` (budgets), `quotaCache.ts`, `degradation.ts`, `providerExpiration.ts`, `lockoutPolicy.ts` (login brute-force lockout, not routing) |
| Resilience | `src/shared/utils/circuitBreaker.ts`, `src/lib/resilience/*`, `open-sse/services/providerCooldownTracker.ts` | Documented in `docs/architecture/RESILIENCE_GUIDE.md` (best single reference) |
| Quota | `src/lib/quota/*` (pools, fair-share, plan registry, schedules, burn-rate, sqlite/redis stores), `open-sse/services/quotaPreflight.ts`, `quotaMonitor.ts`, `quotaResetParsing.ts`, `dailyQuotaReset.ts` | Much of this is multi-key "quota share", which a single-user deployment does not need |
| Storage | `src/lib/db/` | better-sqlite3 in WAL mode. **193 versioned SQL migrations** (`src/lib/db/migrations/*.sql`) with domain modules. AES-256-GCM field encryption (`src/lib/db/encryption.ts`, format `enc:v1:iv:ct:tag`) |
| MCP | `open-sse/mcp-server/` | Official TS SDK. stdio / SSE / Streamable HTTP. 110 tools with scopes (`docs/frameworks/MCP-SERVER.md`) |

**Data model**:
- `provider_connections` (`migrations/001_initial_schema.sql`) has explicit columns: `auth_type`, `priority`,
  `is_active`, tokens, `test_status`, `error_code`, `last_error`, `last_error_at`, `last_error_type`,
  `last_error_source`, `backoff_level`, `rate_limited_until`, `health_check_interval`, `last_health_check_at`,
  `last_tested`, `provider_specific_data` (JSON), `display_name`, `global_priority`, `default_model`,
  `consecutive_use_count`, `rate_limit_protection`. Later migrations add `max_concurrent` and others.
- `test_status` values used in code: `active`, `unavailable` (cooldown), `degraded`, `unknown`, `error`, and the
  terminal values `expired`, `credits_exhausted`, `banned`, `deactivated` (`src/sse/services/auth.ts::isTerminalConnectionStatus`).
- `combos(id, name, data JSON)`. Schema in `src/shared/validation/schemas/combo.ts`:
  - steps `{kind: "model"|"combo-ref", provider, model, connectionId?, allowedConnectionIds?, weight, label, tags, fallbackOnlyOnQuotaExhaustion}`
    (`combo-ref` allows nested combos)
  - `strategy` is one of 19 values
  - `config`: `maxRetries`, `retryDelayMs`, `fallbackDelayMs`, `timeoutMs`, `targetTimeoutMs`, `comboTimeoutMs`,
    `concurrencyPerModel`, `queueTimeoutMs`, `stickyRoundRobinLimit`, `stickyWeightedLimit`, `healthCheckEnabled`, …
  - `responseValidation`, `shadowRouting`, `scoringWeights`
- Other tables:
  - `domain_circuit_breakers(name, state, failure_count, last_failure_time, options)`
  - `quota_snapshots(provider, connection_id, window_key, remaining_percentage, is_exhausted, next_reset_at, window_duration_ms, raw_data, created_at)` (migration 013)
  - `call_logs` (per-attempt summary: status, provider, account, connection_id, duration, combo_name, combo_step_id,
    error_summary, `detail_state` + `artifact_relpath/sha256` for body artifacts stored on disk)
  - `request_detail_logs` (006), `usage_history`, `request_cost_ledger` (182)
  - `key_value(namespace,key,value)` for settings; `domain_fallback_chains`, `domain_budgets`, `domain_cost_history`

### 1.3 Concept mapping to our engine

| Ours | 9router | OmniRoute |
|---|---|---|
| Provider (Exa, Scite…) | registry entry `open-sse/providers/registry/*` | registry + `open-sse/config/searchRegistry.ts` (search providers are "provider = model") |
| Account | `providerConnections` row | `provider_connections` row |
| Capability (search, fetch, verify…) | *model* | *model* / endpoint type (`/v1/search`, `/v1/web/fetch`, `/v1/rerank`) |
| Chain / policy | combo (`models[]`) | combo (steps + strategy + config) |
| Per-(account, capability) lock | `modelLock_<model>` field | model lockout (in-memory `provider:conn:model`) |
| Provider health | none (per-account only) | provider circuit breaker |

---

## 2. Logic to port to Python

Target states: `READY / COOLING / RATE_LIMITED / EXHAUSTED / AUTH_REQUIRED / PLAN_BLOCKED / DEGRADED / DISABLED`.
Recommended scope keys: **provider** (breaker), **account** (cooldown and terminal states) and
**account×capability** (lock). The two repos use these same three scopes.

### (a) Combo / fallback chains

**9router `open-sse/services/combo.js::handleComboChat`**

Algorithm:
1. `getRotatedModels(models, comboName, strategy, stickyLimit)`. For `round-robin`, in-memory
   `comboRotationState[combo] = {index, consecutiveUseCount}` rotates the list start. The index advances after
   `stickyLimit` uses. `resetComboRotation()` runs whenever the combo or settings change.
2. Optional `reorderByCapabilities()`: a stable 3-tier sort (tier 0 has all hard and soft caps, tier 1 has hard caps
   only, tier 2 is the rest). It never drops a target. **Reusable for us**: requested capability features such as
   `fulltext`, `citations`, `date_filter` or `domain_filter` become hard/soft requirements.
3. Iterate the targets sequentially. For each target, call `handleSingleModel` (which runs the account loop):
   - `ok` → return the result.
   - Otherwise parse the error body (`retryAfter`) and call `checkFallbackError(status, text)`.
   - `shouldFallback=false` → return the error immediately (request-scoped 4xx).
   - A 502/503/504 with a cooldown ≤ 5 s → sleep that long before trying the next target.
   - Track `earliestRetryAfter` and `lastStatus`.
4. When everything fails, return 503 (or the last status) with `retryAfter` and a human-readable
   "reset after 2m 30s" (`formatRetryAfter`).

**Account loop** (`src/sse/handlers/chat.js::handleSingleModelChat`, the inner chain):
```
excluded = {}
loop:
  cred = getProviderCredentials(provider, excluded, model)       # filtered + strategy
  if cred is None/allRateLimited -> 503 with earliest retryAfter  (or 404 if no accounts at all)
  cred = checkAndRefreshToken(cred)                               # OAuth refresh before use
  r = execute(cred)
  if r.success: clearAccountError(cred, model); return
  if markAccountUnavailable(cred.id, r.status, r.error, provider, model, r.resetsAtMs).shouldFallback:
      excluded.add(cred.id); continue
  return r.response
```

**Fusion / quorum-grace fan-out** (9R `combo.js::collectPanel`, `handleFusionChat`; OR `open-sse/services/fusion.ts`,
`combo/fusionPanel.ts`). This is the most valuable piece for us:
- Fire all panel calls in parallel.
- Once `minPanel` successes (default 2) have arrived, start a `stragglerGraceMs` timer (default 8000 ms).
  Finish when it fires or when all calls settle.
- `panelHardTimeoutMs` (default 90000 ms) is the absolute cap.
- The result is a sparse array aligned with the inputs.
- Degradation: 0 answers → 503; exactly 1 → return it directly.
- The LLM judge step is **not** ported. Our fusion step is RRF/rerank.
- Python mapping: `asyncio.wait(tasks, return_when=FIRST_COMPLETED)` loop, counting successes and applying the grace
  deadline and hard deadline. Cancel stragglers or let them finish into the cache.

**OmniRoute additions worth porting**:

| Feature | File | Why |
|---|---|---|
| `combo-ref` nested steps | `schemas/combo.ts` | Chains that reuse other chains (e.g. `scholarly_search` → `[s2, openalex, chain:web_search]`) |
| Per-step `connectionId` pin / `allowedConnectionIds` | same | Plan-gated capabilities: route only to the account whose plan allows the capability |
| `fallbackOnlyOnQuotaExhaustion` | same | Paid backup that is used only when the primary is EXHAUSTED, not on transient errors |
| `targetTimeoutMs`, `comboTimeoutMs`, `fallbackDelayMs`, `maxRetries` | same, `combo/targetTimeoutRunner.ts` | Per-attempt and whole-chain budgets (we have 10–30 s) |
| Combo cooldown-aware retry (`comboCooldownWait {enabled,maxWaitMs,maxAttempts,budgetMs}`) | `combo/comboCooldownRetry.ts`, RESILIENCE_GUIDE §4 | When every target is RATE_LIMITED with a *short* retry-after, wait it out instead of failing. Never waits on EXHAUSTED or AUTH states |
| `responseValidation` (required/forbidden substrings, `minContentLength`, JSON path checks) → synthetic 502 → fallback | `combo/responseValidation.ts`, `validateQuality.ts` | Research analogue: "0 results" or "empty fetch body" counts as a soft failure (see `errorClassifier.ts::isEmptyContentResponse` / `EMPTY_CONTENT`) |
| `shadowRouting {targets, sampleRate, maxTargets, timeoutMs}` | `combo/shadowRouting.ts` | Sample-compare providers offline to tune fusion weights |
| LKGP (last-known-good provider pinned first) | `combo/applyStrategyOrdering.ts`, `recordLkgpPin.ts`, `staleLkgpClear.ts` | Cheap session stickiness |
| Decision trace (per invocation: every target with decision + allowlisted skip reason, no bodies) | `combo/decisionTrace.ts` | Powers "why was this routed/skipped" in our request log UI |
| Dry-run `simulate_route` / `explain_route` | `open-sse/mcp-server/tools/advancedTools.ts` | Useful in the admin policy editor ("preview chain") |

**Python data model**:
```
Chain(id, name, capability, mode: "fallback"|"fanout", strategy, steps: [Step], config)
Step(kind: "provider"|"chain", provider, account_id?|allowed_account_ids[], weight, label,
     fallback_only_on_exhaustion: bool, timeout_ms?)
ChainConfig(chain_timeout_ms, target_timeout_ms, fallback_delay_ms, max_retries,
            sticky_limit, cooldown_wait{max_wait_ms,max_attempts,budget_ms},
            fanout{min_quorum=2, grace_ms=8000, hard_timeout_ms=30000},
            validation{min_results, min_content_chars})
```

### (b) Quota / usage tracking and reset windows

**9router**:
- `open-sse/services/usage.js::getUsageForProvider(connection)` dispatches to per-provider fetchers in
  `open-sse/services/usage/*.js`. They return `{quotas: {<name>: {used,total,remaining,remainingPercentage,resetAt}}}`,
  and the endpoints come from the registry `transport.usage` (`usage/shared.js::U`).
- `parseResetTime()` (`usage/shared.js`) normalizes the formats: Date, number with seconds-vs-ms heuristic
  (`< 1e12` → seconds), numeric string, or ISO.
- Local accounting in `src/lib/db/repos/usageRepo.js`:
  - `saveRequestUsage(entry)` → `usageHistory` plus the `usageDaily` rollup
  - `getUsageStats(period)`, `getChartData(period)` for the dashboard aggregates
  - `trackPendingRequest()` / `getActiveRequests()` for in-flight requests
  - `statsEmitter` (an EventEmitter that drives live SSE updates to the UI)
- Pricing: `pricingRepo.js` stores per provider/model with reset-to-default.
- Hard reset boundaries used in code: `auth.js::githubMonthlyResetMs` returns the next UTC month start for a monthly
  plan wall. This is the pattern for "EXHAUSTED until the billing period rolls".

**OmniRoute**:
- **`QuotaInfo`** (`open-sse/services/quotaPreflight.ts`):
  - `{used, total, percentUsed, resetAt, windows?: {<name>: {percentUsed, resetAt}}, window5h?, window7d?, windowWeekly?, windowMonthly?, limitReached?}`
  - Registries: `registerQuotaFetcher(provider, fn)` and `registerQuotaWindows(provider, names)`
  - `evaluateQuotaCutoff()`: blocks an account before dispatch when **any window** has ≤ `minRemaining%` left
    (default 2%) and warns at 20% remaining. Honors `limitReached`.
  - #14359: a recent success overrides a stale "exhausted" snapshot (`isQuotaHealthy`).
- **Directly reusable fetchers for our providers**:
  - `open-sse/services/firecrawlQuotaFetcher.ts`: `GET https://api.firecrawl.dev/v2/team/credit-usage` →
    `remainingCredits`, `planCredits`, `billingPeriodEnd` (used as resetAt). `extraCreditsInferred`/`overPlan` when
    remaining > plan. 60 s TTL cache, 8 s timeout, fail-open (returns null on error).
  - `open-sse/services/tavilyQuotaFetcher.ts`: `GET https://api.tavily.com/usage` → key usage/limit and account
    plan_usage/plan_limit.
  - `genericQuotaFetcher.ts` (converts usage maps to `QuotaInfo`) and `quotaFetchThrottle.ts` (limits fetch concurrency).
- Window math:
  - `src/lib/quota/dimensions.ts`: `unit ∈ {percent, requests, tokens, usd}`, `window ∈ {hourly, 5h, daily, weekly, monthly}`,
    `WINDOW_MS`, `policy ∈ {hard, soft, burst}`
  - `open-sse/services/dailyQuotaReset.ts::nextDailyResetAtMs(tz, hour)` for configurable daily resets in an IANA TZ
  - `quotaResetParsing.ts` (ISO datetime, day-granularity, month-day "resets on Oct 3")
  - `src/lib/quota/burnRate.ts` (tokens/s and time-to-exhaustion)
  - `src/lib/quota/quotaResetTimers.ts::resetExpiredQuotaWindows()`
- Persistence:
  - `quota_snapshots` time series (migration 013) feeds the quota charts
  - `api_key_quota_counters(api_key_id, dimension_key, bucket_index, consumed)` shows the bucketed local counter
    pattern (migration 182)
  - `request_cost_ledger` has per-request unit prices and `amount_usd`
- `src/domain/providerExpiration.ts`: proactive alerts before OAuth token, subscription or credit expiry.
- `src/lib/credentialHealth/scheduler.ts`: periodic credential test (default 60 min, per-connection
  `health_check_interval`, 0 = never). Failure backoff 5 → 10 → 30 → 120 min, reset on success.

**Port plan**:
- One `QuotaSnapshot` shape: `{account_id, windows: [{name, unit, limit, used, remaining_pct, reset_at, source: "api"|"local"|"header"}], limit_reached, fetched_at}`.
- Sources, in precedence order:
  1. Provider usage API (Firecrawl, Tavily and any other provider that has one), polled with a TTL and fail-open.
  2. Response headers (`x-ratelimit-*`, `Retry-After`).
  3. Local counters per `(account, unit, window)` against plan limits from config.
- Preflight cutoff and a warn threshold per account. Snapshots are persisted for charts.
- Hard reset boundaries: billing period end, next UTC day, next month.

### (c) Circuit breaker / cooldown / backoff / health

OmniRoute defines **three separate layers**, and we should keep them separate
(`OR docs/architecture/RESILIENCE_GUIDE.md`, `AGENTS.md` §Resilience):

1. **Provider circuit breaker** (`OR src/shared/utils/circuitBreaker.ts`, persisted in `domain_circuit_breakers`).
   - States `CLOSED → DEGRADED → OPEN → HALF_OPEN`.
   - Thresholds by profile (`open-sse/config/constants.ts::PROVIDER_PROFILES`):

     | Profile | degraded at | open at | reset |
     |---|---:|---:|---:|
     | OAuth | 5 | 8 | 60 s |
     | API key | 7 | 12 | 30 s |
     | Local | derived | 2 | 15 s |

     `degradationThreshold` defaults to 60% of `failureThreshold`.
   - **Trip only on** `408, 500, 502, 503, 504` and transport errors. A 401/403/429 is never a provider-level failure.
   - **Lazy recovery**: reads (`canExecute`/`getStatus`/`getRetryAfterMs`) turn an expired OPEN into HALF_OPEN. No
     background timer is needed.
   - `halfOpenRequests` sets the number of probe slots. A probe success → CLOSED. A probe failure → OPEN and
     `openCycleCount++`.
   - After `backoffEscalationCount` (3) cycles, the reset timeout doubles per cycle, capped at 16×.
   - Per-failure-kind thresholds (`kindThresholds`, `immediateOpen`) and `cooldownByKind`.
   - DEGRADED → CLOSED once `failureCount ≤ degradationThreshold` after successes.
   - Optional window gate (`providerCooldownTracker.ts`): N failures within a window (10/15 min OAuth, 15/30 min API key).
2. **Account (connection) cooldown**:
   - 9R `accountFallback.js::checkFallbackError` + `config/errorConfig.js`:
     - Ordered **text rules first**: `"no credentials"`→2 min, `"request not allowed"`→5 s, `"improperly formed request"`→2 min;
       `"rate limit"`, `"too many requests"`, `"quota exceeded"`, `"capacity"`, `"overloaded"` → exponential backoff.
     - **Then status rules**: 401/402/403/404 → 2 min; 429 → backoff.
     - Other 4xx → **no fallback and no cooldown** (request-scoped).
     - Anything else → 30 s transient.
     - Backoff is `2000 ms × 2^(level-1)`, capped at 5 min, `maxLevel 15`.
     - Provider-reported reset (`resetsAtMs`) overrides the backoff, capped at 30 min (`MAX_RATE_LIMIT_COOLDOWN_MS`).
   - OR:
     - Base 3 s (API key) / 5 s (OAuth), `base × 2^failureIndex`. Prefers the upstream `Retry-After`, reset headers or
       body hint.
     - **Anti-thundering-herd**: concurrent failures on one account must not double-increment `backoffLevel`.
     - Fields: `rate_limited_until`, `test_status="unavailable"`, `last_error(_type)`, `error_code`, `backoff_level`.
     - **Terminal states are not cooldowns and must never be overwritten by transient ones**:
       - `banned`: account ban signals, or 3 consecutive per-request refusals (`requestRejectedStreak.ts`)
       - `expired`: terminal after `EXPIRED_RETRY_MAX = 3` bounded retries
       - `credits_exhausted`
3. **Account×model lockout** → our **account×capability lock**:
   - 9R stores `modelLock_<model>` ISO expiry on the connection. `clearAccountError` clears the lock for the model
     that succeeded plus any expired locks, and resets error state only when no active locks remain.
   - OR `accountFallback.ts::lockModel / recordModelLockoutFailure / decayModelFailureCount`:
     - Scope by status (`accountFallback/exactModelLock.ts::resolveLockoutScope`): 429/403/402 lock the quota
       family, 404 locks the exact model, 5xx locks the exact tuple.
     - **Success-decay**: each success halves `failureCount` and deletes the entry at 0.
     - Settings (`src/lib/resilience/modelLockoutSettings.ts`): `errorCodes [403,404,429,502,503,504]`, base 120 s,
       max 30 min, 10 backoff steps.
     - State is in-memory. We should persist it (restart-safe).

**Health score** (`OR accountFallback.ts::getAccountHealth`): `100 − 10·backoffLevel − 20·(lastError?) − 30·(cooling?)`,
floored at 0. P2C uses it. Keep it as a UI badge and a tiebreaker.

**Retry-hint parsing** (`OR accountFallback.ts::parseRetryAfterFromBody`, `parseRetryFromErrorText`, `retryAfterJson.ts`):
- Google RPC `details[].retryDelay: "33s"`
- "Please retry after 20s" / "retry in 54.47s"
- Embedded ISO timestamps
- "reset(s) after/in XhYmZs"
- Day-granularity resets
- Everything capped at 30 days (`MAX_PROVIDER_COOLDOWN_MS`); short hints capped separately.

**Status restatement** (`OR open-sse/config/upstreamStatusRestatement.ts`, RESILIENCE_GUIDE §7): a per-provider
`{fromStatuses, toStatus, textMarkers, excludeMarkers, defaultRetryAfterMs}` table applied *before* classification,
for upstreams that report quota as 403/400. Useful for any research API that mislabels credit exhaustion.

### (d) Account selection strategies

Both repos first filter out accounts that are excluded for this request, `isActive=0`, in a terminal status, cooling
(`rateLimitedUntil > now`) or capability-locked, then apply the strategy. 9R serializes selection with a global
promise mutex (`selectionMutex`); in Python use an `asyncio.Lock` per provider.

| Strategy | Source | Algorithm |
|---|---|---|
| `fill-first` / priority (default) | 9R `auth.js`, OR `auth.ts` | First account by `priority` ascending |
| `round-robin` (sticky) | 9R `auth.js` L150-190, OR `auth.ts` ~L1998 | Sort by `lastUsedAt` desc. If the most recent account has `consecutiveUseCount < stickyLimit` (default 3), stay on it and increment. Otherwise pick the LRU account (OR: **lower `backoffLevel` first**, then LRU, then priority) and reset the count to 1. In fallback mode (something excluded), always pick LRU |
| `p2c` | OR `accountSelector.ts`, `auth.ts::compareP2CConnections` | Take 2 random candidates and keep the healthier one (health score, plus quota when known) |
| `random` / `strict-random` | OR `auth.ts`, `getNextFromDeckSync` | Uniform, or a shuffled deck without replacement until exhausted |
| `least-used` | OR `auth.ts` ~L2108 | Lowest `backoffLevel`, then oldest `lastUsedAt`, then priority |
| `cost-optimized` | OR | By priority (account level); at combo level, cheapest target first (`sortTargetsByCost`) |
| `expiry-first` | OR `pickExpiryFirstConnection` | Spend the account whose credits or window expire soonest (use-it-or-lose-it) |
| `reset-aware` | OR `combo/quotaScoring.ts::scoreResetAwareQuota` | See the formula below. Tie band 5% |
| `headroom` / `reset-window` | OR `combo/quotaStrategies.ts`, `headroomRanking.ts` | Most remaining quota first / soonest reset first |
| sticky pin | 9R `options.preferredConnectionId`; OR session affinity (`sessionStickiness.ts`) and LKGP | Prefer the given account if it is available |
| weighted (combo level) | OR `combo/targetSorters.ts::selectWeightedTarget` | Draw one step with probability `w/Σw`, then order the rest by weight descending. A weight of 0 is never drawn unless all weights are 0. `stickyWeightedLimit` |

`reset-aware` formula (`OR combo/quotaScoring.ts::scoreResetAwareQuota`):

```
window_score = remW·remaining + pressureW·urgency·(1−remaining)
urgency      = 1 − msUntilReset/windowMs        # 0.5 when resetAt is unknown
score        = 0.35·session_score + 0.65·weekly_score
```
- The session window uses weights (0.45, 0.55) over 5 h. The weekly window uses (0.25, 0.75) over 7 d.
- `limitReached` gives `score = −∞`.
- Exhaustion guard: if session remaining < guard, the score is multiplied by `max(0.05, remaining/guard)`.

For us this becomes "spend monthly credits that are about to reset first", with `billing_period_end` as the window.

### (e) Error classification → states

OR `open-sse/services/errorClassifier.ts::classifyProviderError(status, body, provider)` returns one of
`PROVIDER_ERROR_TYPES`: `rate_limited`, `unauthorized`, `account_deactivated`, `forbidden`, `server_error`,
`quota_exhausted`, `context_overflow`, `oauth_invalid_token`, `empty_content`, `model_not_found`, `geo_blocked`,
`fingerprint_rejection`, `request_rejected`, …

Signal lists live in `OR accountFallback.ts`:
- `CREDITS_EXHAUSTED_SIGNALS`: `insufficient_quota`, `billing_hard_limit_reached`, `exceeded your current quota`,
  `credit_balance_too_low`, `out of credits`, `payment required`, …
- `ACCOUNT_DEACTIVATED_SIGNALS`, `OAUTH_INVALID_TOKEN_SIGNALS`
- `MODEL_PERMANENTLY_UNAVAILABLE_PATTERNS`, `ACCOUNT_SUSPENDED_BILLING_PATTERNS`
- `MALFORMED_REQUEST_PATTERNS`, `RATE_LIMIT_TEXT_PATTERNS`, `PARAM_VALIDATION_PATTERNS`
- plus `isRequestScoped400()` (`accountFallback/requestScoped400.ts`)

Note #8631: Gemini's "Resource has been exhausted" is transient, so the signals are anchored.
`RateLimitReason` (`open-sse/config/constants.ts`) is one of `quota_exhausted`, `rate_limit_exceeded`,
`model_capacity`, `server_error`, `auth_error`, `unknown`.

Mapping to our states (the scope is in brackets):

| Upstream signal | OR type / 9R rule | Our state [scope] | Exit |
|---|---|---|---|
| 2xx with valid payload | — | READY [account, account×cap]; breaker success | — |
| 2xx but empty / 0 results where results were expected | `EMPTY_CONTENT` / responseValidation | no state change; soft failure → next target; counts toward exact account×cap lock only if it repeats | success-decay |
| 429 plus rate-limit wording, or `Retry-After` | `RATE_LIMITED`, 9R backoff rule | **RATE_LIMITED** [account, or account×cap when the limit is per-endpoint] | `until = retry hint` or `base·2^level` (cap 5 min); lazy expiry → READY |
| 429/402/403/400 plus credits/quota/billing wording; 402 | `QUOTA_EXHAUSTED`, `credits_exhausted` | **EXHAUSTED** [account] | until `resetAt` (quota API `billingPeriodEnd`, parsed text, next UTC day/month); also cleared by a successful quota refresh or a manual reset |
| 401 with expired/invalid token (OAuth) | `OAUTH_INVALID_TOKEN` / `UNAUTHORIZED` | try refresh → if it fails, **AUTH_REQUIRED** [account] (OR: `expired` after 3 bounded retries) | re-auth / new key (manual) |
| 401 (API key invalid) | `UNAUTHORIZED` | **AUTH_REQUIRED** [account] | manual |
| 403 plan/entitlement, "upgrade", model/endpoint access denied; 404 feature not on plan | `FORBIDDEN` + `MODEL_ACCESS_DENIED_PATTERNS`, `MODEL_NOT_FOUND`; OR lock scope `model` (6 h for agentrouter) | **PLAN_BLOCKED** [account×capability] | long TTL (hours) plus manual re-test; plan change clears it |
| 403/401 plus deactivated/suspended/banned | `ACCOUNT_DEACTIVATED`, `banned` | **DISABLED** [account] (terminal, `disabled_reason=provider_suspended`) | manual only |
| operator toggles `is_active=0` | `isActive` | **DISABLED** [account] | manual |
| 408/5xx/timeout/transport | `SERVER_ERROR`; breaker trip codes | account: **COOLING** (transient 30 s, 9R `TRANSIENT_COOLDOWN_MS`); provider breaker `failureCount++` | lazy expiry |
| breaker DEGRADED | `STATE.DEGRADED` | **DEGRADED** [provider] (still routable; deprioritize, UI warning) | failures ≤ degradation threshold |
| breaker OPEN / HALF_OPEN | `STATE.OPEN` | provider **COOLING** (skipped); HALF_OPEN lets one probe through | reset timeout (escalating) |
| 400 malformed / param validation / request-scoped | 9R "other 4xx → no fallback"; OR `isRequestScoped400` | **no state change**; return the error to the caller (and do **not** try other accounts) | — |
| 404 on a resource (paper/URL not found) | OR `isResourceNotFoundResponse` → null | no state change; a normal "not found" result | — |
| repeated per-request refusals | `REQUEST_REJECTED` → streak → `banned` | COOLING, then escalate to DISABLED after N | manual |

Precedence rules taken from both repos:
- Text signals beat status codes.
- Terminal states (`DISABLED`, `AUTH_REQUIRED`, `EXHAUSTED` before `resetAt`) are never downgraded by transient writes.
- A success clears only the scope that succeeded (9R `clearAccountError`).
- Only 408/5xx/transport errors feed the provider breaker.

State transitions (account scope):

```
READY --429--> RATE_LIMITED --(until passes, lazy)--> READY
READY --5xx/timeout--> COOLING(30s, backoff) --> READY
READY --credits/quota/402--> EXHAUSTED --(reset_at | quota refresh shows remaining>0 | manual)--> READY
READY --401--> [refresh] --ok--> READY | --fail--> AUTH_REQUIRED --(reauth/new key)--> READY
READY --deactivated--> DISABLED --(manual)--> READY
(account×cap) READY --403 plan/404 feature--> PLAN_BLOCKED --(TTL/manual re-test)--> READY
(provider) CLOSED --n≥deg--> DEGRADED --n≥open--> OPEN --reset--> HALF_OPEN --ok--> CLOSED / --fail--> OPEN(cycle++)
Effective account state = max-severity(provider, account, account×cap) for the requested capability.
```

### (f) Other reusable logic

| Item | Where | Value for us |
|---|---|---|
| **Search provider registry** | OR `open-sse/config/searchRegistry.ts` | ~20 providers (serper, brave, perplexity, **exa**, **tavily**, **firecrawl**, google-pse, linkup, searchapi, you.com, **searxng**, jina, duckduckgo-free, …). Each entry has `costPerQuery`, `freeMonthlyQuota`, `searchTypes`, `defaultMaxResults`/`maxMaxResults`, `timeoutMs`, `cacheTTLMs`, `fallbackOnly`, `allowClientBaseUrlOverride` (SSRF guard, GHSA-3f8g-pfh9-j687). Good seed for web-search adapters and defaults |
| **Normalized search result** | OR `open-sse/handlers/search.ts` (`SearchResult`, `SearchResponse`, `build*Request`/`normalize*Response` per provider, `NON_RETRIABLE = {400,401,403,404}`, 15 s global timeout, control-char query sanitizing) | Close to our evidence item: `title, url, snippet, position, score, published_at, content{format,text,length}, metadata{author,language,source_type}, citation{provider,retrieved_at,rank}, provider_raw`; response has `usage.search_cost_usd`, `metrics`, `errors[]` |
| **Search cache + request coalescing** | OR `open-sse/services/searchCache.ts` | NFKC+lowercase+whitespace-collapsed query key; in-flight promise dedup (prevents stampede from agent tools); bounded 500 entries; hit/miss stats. Port it for our query cache |
| Web fetch dispatch | OR `open-sse/handlers/webFetch.ts`, `executors/{firecrawl,jina-reader,tavily,…}-fetch.ts` | `{url, provider?, format: markdown|html|links|screenshot, depth, wait_for_selector}` |
| Rerank endpoint | OR `open-sse/handlers/rerank.ts` | Reference for a rerank-API adapter shape |
| Request log | 9R `src/lib/db/repos/requestDetailsRepo.js` (`sanitizeHeaders`, filterable list, detail by id); OR `call_logs` summary row + on-disk body artifact (`artifact_relpath`, `sha256`, `detail_state`), `src/lib/logPayloads.ts`, `logRotation.ts`, `logExport/` | Summary row plus optional redacted body artifact is the right design for "request log + replay" |
| Replay | OR `src/app/api/tools/traffic-inspector/requests/[id]/replay/route.ts` | Re-issues a captured request through the local engine with an `x-*-source: replay` tag, and skips masked auth. Our version: replay by stored normalized request (not raw HTTP) |
| Combo metrics | OR `open-sse/services/comboMetrics.ts` | Per-chain/per-target counts, latency and success; shown in dashboards |
| Cost | OR `request_cost_ledger`, `src/domain/costRules.ts` (daily/monthly budget, warning threshold 0.8), `omniroute_set_budget_guard` (degrade/block/alert) | Per-provider per-call cost (search providers are priced per query or per credit) |
| Config export/import & backups | OR `src/app/api/settings/{export-json,import-json}`, `src/app/api/db-backups/{export,import,exportAll}`, `src/lib/db/backup.ts`, `backupRetention.ts`; 9R `settingsRepo.js::exportSettings` | Admin "export config (no secrets)" and "backup DB" |
| Secret-at-rest encryption | OR `src/lib/db/encryption.ts` (AES-256-GCM, versioned prefix, passthrough when no key) | We already get Fernet from mcp-gateway. Take only the versioned-prefix plus migration idea |
| Credential health scheduler | OR `src/lib/credentialHealth/scheduler.ts`, `probePolicy.ts` | Periodic "test account" (a cheap call per provider) that updates state proactively |
| Provider expiration alerts | OR `src/domain/providerExpiration.ts` | Warn before OAuth refresh-token expiry and before credits or subscription run out |
| Background OAuth refresh | 9R `src/sse/services/backgroundTokenRefresh.js`, `open-sse/services/tokenRefresh.js`; OR `refreshSerializer.ts` (one refresh in flight per account) | Needed for Scite/Elicit/Undermind/Consensus OAuth. Serialized refresh is essential |
| Per-account concurrency cap | OR `accountSemaphore.ts`, `max_concurrent` column | Some research APIs allow 1–5 concurrent calls |
| Local RPM gate / queue admission | OR `rateLimitManager.ts` (Bottleneck), `rollingRpmGate.ts`, `slidingWindowLimiter.ts`, `admission.ts::checkQueueAdmission` (`maxWaitMs` 30 s queue budget, `maxQueueDepth`) | Proactive client-side rate limits per account (e.g. S2 1 rps, arXiv 1 req/3 s) instead of discovering them via 429 |
| MCP server patterns | OR `open-sse/mcp-server/` (`scopeEnforcement.ts`, `toolCardinality.ts` allow/deny lists, `audit.ts`, `httpAuthContext.ts`, `runtimeHeartbeat.ts`) | Tool-level scopes, deny/allow manifest filter, audit log of tool calls. OR's own tools (`omniroute_web_search`, `omniroute_web_fetch`, `omniroute_check_quota`, `omniroute_simulate_route`) are good naming references |
| MCP stdio→SSE bridge | 9R `src/lib/mcp/stdioSseBridge.js` | Reference only (we use python-sdk) |
| Client-IP handling | 9R `custom-server.js` | Trusts forwarding headers only from a loopback proxy. Same rule for us behind cloudflared (trust `CF-Connecting-IP` only when the peer is 127.0.0.1) |
| Degradation wrapper | OR `src/domain/degradation.ts` | Full → reduced → minimal → safe default (e.g. reranker unavailable → RRF only) |

---

## 3. UI

### 3.1 Stack comparison

| | 9router | OmniRoute |
|---|---|---|
| Framework | Next 16 app router, React 19.2, **plain JS** (+prop-types), `@/` alias (`jsconfig.json`) | Next 16 app router, React 19.2, TS with `strict:false` (many components effectively untyped) |
| CSS | Tailwind 4 (`@tailwindcss/postcss`, no config file). `src/app/globals.css` (604 lines): CSS-var tokens (brand scale #E56A4A, surface/border/text/status) via `@theme inline`; dark mode `@custom-variant dark (.dark)` | Same approach. `src/app/globals.css` (856 lines), also imports fumadocs-ui and react18-json-view CSS |
| Components | Hand-rolled primitives in `src/shared/components/` (≈49 files / 9k LOC; Button, Input, Select, Card, Modal+Confirm, Toggle, Badge, Tooltip, SegmentedControl, Pagination, Drawer, Loading, Avatar; `layouts/DashboardLayout.js`, `Sidebar.js` with the nav array at L19-40, `Header.js`). Only dependency is `cn` (clsx + tailwind-merge) | Hand-rolled, 122 files / 27.8k LOC. Adds DataTable, FilterBar, ColumnToggle, EmptyState, CommandPalette, NotificationToast, Collapsible, analytics `TimeRangeSelector`. Several primitives import next-intl |
| Icons | `material-symbols` (ligature spans). Provider logos are images in `public/providers` via next/image | `material-symbols` (1,625 uses). `@lobehub/icons` via `ProviderIcon.tsx` |
| State / data | zustand 5 (`src/store/*`, 260 LOC). Raw `fetch` (397 calls / 82 files). SSE `/api/usage/stream`. No SWR/react-query | zustand (UI only). Raw `fetch` (1,040 calls). Global fetch monkeypatch for CSRF and base path (`src/shared/utils/dashboardCsrf.ts`, `basePathFetch.ts`) |
| Charts / graphs / editors | recharts 3.7 (5 files), @xyflow (topology), monaco (translator only), @dnd-kit (combo list only) | recharts 3.8 (`shared/components/analytics/*`), @xyflow (20 files), monaco, native HTML5 drag in the combo editor |
| i18n | Custom **runtime DOM translator** (`src/i18n/runtime.js`, MutationObserver, `public/i18n/literals/<locale>.json` keyed by English literal, 35 locales). **Components contain plain English** | `next-intl` 4: 540 files / 1,310 `t()` calls, `src/i18n/messages/*.json` (67 locales, en.json 14.4k lines) |
| Dashboard auth | `src/proxy.js` → `src/dashboardGuard.js`. JWT cookie `auth_token`, `requireLogin` toggle, OIDC/SAML | `src/proxy.ts` → `src/server/authz/pipeline.ts`. JWT cookie plus CSRF header |
| Next coupling (files) | next/link 13, next/navigation 14, next/image 20, next/dynamic 2, `@/lib/*` 0, open-sse 7 (static catalogs: capabilities, pricing, providerModels) | next/link 56, next/navigation 56, next/image 7, next/dynamic 13, `@/lib/*` 127 files, open-sse 37. ~25 server pages (mostly redirects; `home/page.tsx` reads the DB) |
| Server actions | none | none |
| Size of in-scope UI | ≈17k LOC | ≈85–95k LOC (providers 34.6k, combos 9.8k, settings ~12k, …) |

Neither repo has a **request replay UI**. We build that ourselves.

### 3.2 Inventory of the parts we need

| Feature | 9router (base) | OmniRoute (cherry-pick) |
|---|---|---|
| Provider cards / list, batch test | `(dashboard)/dashboard/providers/page.js` (1,040), `components/ConnectionsCard.js`, `AddCompatibleModal.js` | `providers/page.tsx`, `components/ProviderCard.tsx` |
| Provider detail and account list (priority reorder, toggle, cooldown timer, test) | `providers/[id]/page.js` (1,965), `ConnectionRow.js`, **`CooldownTimer.js`**, `AddApiKeyModal.js` (427), `shared/components/EditConnectionModal.js` | `[id]/components/ConnectionRow.tsx` (980), **`CoolingConnectionsPanel.tsx`**, `BatchTestResultsModal.tsx`, `ProviderTestSlideOver.tsx` |
| OAuth connect | `shared/components/OAuthModal.js` (1,000) and `src/app/callback/page.js`. Flows: (a) popup → callback relays `{code,state}` via `postMessage` / `BroadcastChannel("oauth_callback")` / localStorage → `POST /api/oauth/:p/exchange`; (b) manual paste of the callback URL for remote hosts; (c) device code (`device-code` + `poll`). Drop the vendor modals (Kiro/Cursor/Zed/IFlow/Xiaomi/GitLab) | `OAuthModal.tsx` + `oauthModal/*Step.tsx` (same flows, split into steps) |
| Quota / limits | `quota/page.js` → `usage/components/ProviderLimits/*` (`ProviderLimitCard`, **`QuotaProgressBar`**, `QuotaTable`, `utils.js` 785); `GET /api/usage/:connectionId?force=1` | `usage/components/ProviderLimits/*`, `quotaParsing.ts`; `settings/components/QuotaPreflightCard` |
| Usage charts | `usage/page.js` tabs: `OverviewCards`, `UsageChart`, `ProviderBarChart`, `TopModelsChart`, `UsageTable`, `shared/components/UsageStats.js` (SSE) | `costs/CostOverviewTab.tsx`, `analytics/*` (ComboHealthTab, ProviderUtilizationTab, **RouteExplainabilityTab**) |
| Request logs / detail | `usage/components/RequestDetailsTab.js` (510), `shared/components/RequestLogger.js` | **`shared/components/RequestLoggerV2.tsx`** (1,761), **`RequestLoggerDetail.tsx`** (1,240), **`RequestTimeline.tsx`** (974), `ConsoleLogViewer.tsx`. Better filtering/detail. Take the detail + timeline |
| Combo (chain) editor | `combos/page.js` (1,211), dnd-kit sortable chain, `shared/components/ComboFormModal.js` | `combos/page.tsx` (5,109, god file); `settings/components/FallbackChainsEditor.tsx` (233), `WeightTotalBar` |
| Health / breaker | — (per-connection cooldown timers only) | **`dashboard/health/*`** (`ProviderHealthMatrixCard.tsx`), **`resilience/connections/*`** (`ConnectionsTable`, `ConnectionDetail`, `BreakerTimeline`), `settings/components/ModelCooldownsCard.tsx` |
| Settings | `profile/page.js` (1,702: security/password, routing strategy, network proxy, DB export/import), `dashboard/settings/pricing/page.js` | **`settings/components/ResilienceTab.tsx` + `ResilienceFields.tsx`**, `RoutingStrategyCard`, `SystemStorageTab.tsx` (backups, export/import JSON, purge, vacuum), `ModelLockoutCard` |
| API keys (client keys) | `endpoint/EndpointPageClient.js` (keys part) | `api-manager/*` (scopes, permissions; bigger than we need) |

**Not needed**:
- 9R: basic-chat, cli-tools, media-providers, mitm, pxpipe, skills, token-saver, translator, proxy-pools, and the promo/donate/changelog modals plus the Google Analytics tag in `layout.js`.
- OR: a2a, acp, agent-skills, batch, cache, chaos, cli-*, cloud-agents, compression, conductor, context, conversations, discovery, free-*, gamification, leaderboard, memory, models, onboarding, orchestration, playground, plugins, radar, relay, system/mitm, tools/traffic-inspector, translator, …

### 3.3 Recommendation: use 9router as the base, cherry-pick OmniRoute

Reasons:
- **Size.** 9router's in-scope UI is ~6× smaller.
- **No i18n rewrite.** 9router components already contain plain English, so we just drop the runtime translator. OmniRoute would need a next-intl → `use-intl` or react-i18next migration across 528 files.
- **Less Next.js coupling.** 9router has no `@/lib` server imports.
- **Simpler primitives.** 9router's primitives are dependency-free.
- **OmniRoute's TypeScript is not a real advantage.** It runs with `strict:false`.

Take from OmniRoute the pieces 9router lacks:
- health/breaker views
- request-log detail and timeline
- resilience settings form
- fallback-chain editor
- backup/storage tab
- DataTable/FilterBar/EmptyState primitives

For these, add a tiny `useTranslations` shim that looks keys up in a trimmed `en.json`, or inline the strings.

### 3.4 Extraction plan (Vite + React 19 + react-router 7 + Tailwind 4)

1. **Scaffold (1 d).**
   - Vite with `@tailwindcss/vite`; copy `globals.css`; add material-symbols, zustand, recharts, @dnd-kit, clsx and tailwind-merge.
   - `@/` alias.
   - Move the dark-mode pre-paint script into `index.html`.
2. **Shims via Vite `resolve.alias` (0.5 d)**, so copied files stay unedited:
   - `next/link` → react-router `<Link to=href>`
   - `next/navigation`:
     - `useRouter` → `{push: navigate, replace, back, refresh: noop}`
     - `usePathname` → `useLocation().pathname`
     - `useSearchParams` → wrapper that returns params only
     - `useParams`
     - `redirect` → `<Navigate>`
   - `next/image` → `<img>`
   - `next/dynamic` → `React.lazy`
3. **Server pages → client routes (0.5 d).** `getMachineId` and similar become API calls.
4. **Replace static catalogs imported from `open-sse/*` (2–3 d)** with `GET /api/catalog/*` loaded into a zustand store at boot. This covers `shared/constants/providers.js`, `models.js`, `providers/capabilities.js` and `pricing.js`.
5. **API client (1–2 d).**
   - `VITE_API_BASE`, `credentials: "include"`, CSRF header, 401 → `/login`.
   - Keep the `/api/...` path shapes so the Python routes mirror them.
   - SSE for live usage/log streams.
6. **Auth (1 d).**
   - Port the login page; add a `RequireAuth` guard calling `/api/auth/status`.
   - Python issues an httpOnly session cookie.
   - The OAuth consent pages stay in mcp-gateway, per the requirements.
7. **Page ports (8–11 d).**
   - providers + accounts + modals + test + generic OAuth: 3–4 d
   - chain editor with dnd-kit, adapted to steps/strategy/config: 1 d
   - usage + quota: 2 d
   - settings/keys/pricing (split `profile/page.js`): 2 d
   - OmniRoute health/resilience/log-detail/chain pieces: 2–3 d
8. **Cleanup / QA (1–2 d).** Sidebar nav array, remove promo/GA/tunnel/MITM/CLI links, lint.
9. **New pages that neither repo has.**
   - replay (request detail → "Replay" → diff view): 1–2 d
   - fusion/rerank policy editor (weights, reranker toggle): 1–2 d

**Estimate:** ≈ **15–20 person-days** for the extraction, plus 2–4 d for the new pages, plus 3–5 d if converting to TypeScript. The same scope on an OmniRoute base would be ≈ 35–50 d.

**Alternative:** Next static export (`output: "export"`) would keep the Next.js imports. It is rejected because dynamic routes (`providers/[id]`) need `generateStaticParams` or catch-all hacks, and it keeps the Next toolchain for what is a pure SPA.

### 3.5 Admin REST API the SPA expects (implement in Python)

These are derived from the UI fetch calls, filtered to our scope and renamed to our nouns. Keeping the 9router path shapes minimizes UI edits: 9R `providers/:id` = connection, `combos` = chain.

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/status`, (`GET /api/auth/csrf`) |
| Catalog | `GET /api/catalog/providers` (static provider defs: auth types, capabilities, plan windows, icons), `GET /api/catalog/capabilities`, `GET /api/pricing`, `PUT /api/pricing` |
| Accounts (9R "providers"/connections) | `GET /api/providers` (list accounts, filter by provider), `POST /api/providers` (create API-key account), `GET/PUT/DELETE /api/providers/:id` (PUT covers priority/toggle/name), `POST /api/providers/validate` (validate a key before save), `POST /api/providers/:id/test`, `POST /api/providers/test-batch`, `POST /api/providers/:id/reset-state` (clear cooldown/terminal state; OR `DELETE /api/resilience/model-cooldowns`) |
| OAuth connect | `GET /api/oauth/:provider/authorize` → `{authUrl, state, codeVerifier?}`, `POST /api/oauth/:provider/exchange` `{code, state, redirectUri}`, `GET /api/oauth/:provider/device-code`, `POST /api/oauth/:provider/poll`, SPA route `/callback` (relays to the opener). For MCP-OAuth upstreams (Scite/Elicit/…), `authorize` wraps mcp-gateway's upstream OAuth client (DCR + PKCE) |
| Quota | `GET /api/usage/:accountId?force=1` (live quota snapshot), `GET /api/usage/provider-limits` (all accounts), `GET /api/quota/snapshots?account=&from=&to=` (charts) |
| Usage / cost | `GET /api/usage/stats?period=`, `GET /api/usage/chart?period=`, `GET /api/usage/stream` (SSE), `GET /api/usage/providers` |
| Request logs / replay | `GET /api/logs?provider=&account=&status=&capability=&q=&page=`, `GET /api/logs/:id` (summary + attempts + decision trace + redacted bodies), `POST /api/logs/:id/replay` → `{new_log_id}`, `GET /api/logs/stream` (SSE), `GET /api/logs/console` |
| Chains (9R "combos") | `GET/POST /api/combos`, `GET/PUT/DELETE /api/combos/:id`, `POST /api/combos/reorder`, `POST /api/combos/:id/test`, `POST /api/combos/:id/simulate` (dry-run with skip reasons), `GET /api/combos/metrics` |
| Health / resilience | `GET /api/monitoring/health` (provider breakers + account states), `DELETE /api/monitoring/health?provider=` (reset breaker), `GET /api/resilience/connections?windowMs=`, `GET/DELETE /api/resilience/model-cooldowns` (→ account×capability locks), `GET/PUT /api/resilience` (thresholds, backoff, cooldown-wait) |
| Settings / policy | `GET/PATCH /api/settings` (routing strategy, per-provider strategy, fusion weights, reranker toggle, cache TTLs, fan-out quorum/grace/timeout) |
| Client keys | `GET/POST /api/keys`, `PUT/DELETE /api/keys/:id` (if we expose non-OAuth bearer keys for Claude Code / agentRT) |
| Backup / config | `GET /api/settings/export-json` (no secrets), `POST /api/settings/import-json`, `GET/POST /api/db-backups`, `GET /api/db-backups/export` |

**Not provided (the UI calls must be removed):**
- `/api/translator/*`, `/api/cli-tools/*`, `/api/headroom/*`, `/api/pxpipe/*`, `/api/tunnel/*`, `/api/mcp/*`, `/api/proxy-pools/*`
- the vendor-specific OAuth import routes
- `/api/usage/:id/{claude-reset,codex-reset-credits}`

---

## 4. Do NOT copy

**LLM-specific (irrelevant):**
- `open-sse/translator/*`, `transformer/*`, `executors/*` (chat formats)
- thinking/reasoning handling (`reasoning*`, `thinkingBudget`, `*Thinking.ts`)
- token usage extraction/estimation (`9R open-sse/utils/usageTracking.js`)
- prompt/semantic cache
- Auto-Combo 16-factor LLM scoring and task fitness (`OR open-sse/services/autoCombo/*`), Arena-ELO sync
- context relay/handoff, pipeline/agentic orchestration, the fusion **judge** prompt
- media endpoints (image/tts/video), embeddings
- model catalogs and pricing sync (models.dev/LiteLLM), A2A/ACP, memory, skills, cloud agents, gamification,
  Notion/Obsidian, evals, guardrails/PII for prompts

**Token compression:**
- 9R `open-sse/rtk/*`, `headroom`, `pxpipe`, caveman/ponytail
- OR `open-sse/services/compression/*` and the MCP `descriptionCompressor.ts`
- All the related dashboard pages

**ToS-problematic — explicitly exclude, never port:**

| Feature | Location | Why excluded |
|---|---|---|
| MITM proxy / TLS interception of vendor CLIs and IDEs | 9R `src/mitm/*`, `/dashboard/mitm`; OR `src/mitm/*`, traffic inspector | Intercepts and reroutes official clients' traffic |
| Client impersonation / fingerprint stealth / cloaking | OR `claudeCodeFingerprint.ts`, `claudeCodeObfuscation.ts`, `claudeCodeCCH.ts`, `*TlsClient.ts`, `open-sse/utils/tlsClient.ts` (wreq-js Chrome-124 JA3), `config/cliFingerprints.ts`, `antigravityHeaderScrub.ts`, `docs/security/STEALTH_GUIDE.md`; 9R `open-sse/utils/claudeCloaking.js`, `claudeSignature.js`, `opencodeFingerprint.js`, `cursorChecksum.js`, `bypassHandler.js` | Evades provider client detection |
| CAPTCHA/Turnstile solving, WAF clearance | OR `claudeTurnstileSolver.ts`, `grokClearance.ts`, `wafRateLimit.ts` | Circumvention |
| Web-session scraping as "providers" (consumer web UIs, cookies) | OR `browserBackedChat*`, `chatgptWebCodexAdmin.ts`, `notion*`, `perplexityTlsClient.ts`, `lmarena*`, `zaiWebCredentials.ts`, `inAppLoginService.ts`, `*BrowserLogin.ts`; 9R `/api/oauth/iflow/cookie`, `xiaomi-mimo/login` | Uses consumer sessions programmatically |
| Bulk credential import / auto-import of other tools' tokens | OR `/api/providers/{claude,codex,agy}-auth/import-bulk`, `/api/oauth/cliproxy-import`; 9R `/api/oauth/codex/bulk-import`, `grok-cli/bulk-import`, `kiro|cursor|zed/auto-import` | Account-farming / pooling pattern |
| Embedded third-party "public" OAuth client IDs of vendor CLIs | OR `open-sse/utils/publicCreds.ts`, `docs/security/PUBLIC_CREDS.md` | Poses as another vendor's app. We register our own OAuth clients (DCR with Scite/Elicit/…) |
| Free-proxy scraping and IP rotation pools | OR `src/lib/freeProxyProviders/*` (proxifly, webshare, oneproxy), `proxyAutoSelector.ts`, `sessionPool`; 9R `proxyPools` + `rotateStrategy` | Rate-limit evasion |
| Quota "reset credit" or usage-wall exploitation | OR `claudeLimitReset.ts`, `claudeLowPriority.ts`, `grokResetCredits*.ts`, `antigravityCredits.ts`; 9R `consumeCodexRateLimitResetCredit`, `consumeClaudeResetGrant` | Subscription-plan gaming |
| Free-tier harvesting / "free providers" rankings | OR `freeProviderRankings*`, `alibabaFreeTier*`, `openrouterFreeWindow.ts`, `/api/free-models`, `radar` | Built around stacking free tiers |
| Account-ban evasion heuristics | OR `docs/security/BAN_DETECTION.md` consumers that auto-rotate away from bans | Our policy: DISABLED requires a human |
| 3rd-party tunnel relay | 9R `src/lib/tunnel/cloudflare/config.js` `WORKER_URL=https://abc-tunnel.us` | Unknown third party in the path. We use our own named Cloudflare Tunnel |

Our account-pool rule (requirements: "No ToS-violating rotation"): we take **failover among legitimately owned
keys/plans** and **plan-gated routing**. We do not take round-robin used to multiply free quotas. Strategies such as
round-robin and P2C are allowed only across accounts the operator legitimately holds, and per-provider policy can
restrict a provider to `fill-first` (failover only).

---

## 5. License / attribution

- **Both repos are MIT.** Copying code or components requires keeping the copyright notice and permission text in
  "all copies or substantial portions":
  - OR `LICENSE`: "Copyright (c) 2026 diegosouzapw"
  - 9R `LICENSE`: "Copyright (c) 2024-2026 decolua and contributors"
- OR is itself a fork of 9R, so code copied from OR that originated in 9R carries both lineages. **Credit both.**
- Recommended:
  - `THIRD_PARTY_NOTICES.md` in our repo with both MIT texts verbatim, plus a list of the ported or copied files
    (source path → our path, upstream commit `666ea59` / `f01fb90`).
  - SPDX header or comment on each copied or ported file: `# Ported from OmniRoute (MIT, © 2026 diegosouzapw) open-sse/services/combo/quotaScoring.ts @666ea59`.
  - README "Credits" section (requirements task 9).
- **OR `THIRD_PARTY_NOTICES.md` (349 lines) covers assets we might copy by accident:**
  - `public/providers/*.svg` provider logos: 6 derived from `@lobehub/icons` 5.10.0; 65 byte-exact from theSVG (MIT
    code, but **trademarks reserved**; `azure`/`ovhcloud` are brand-use, `minimax` custom, `huggingface`
    unlicensed/HOLD)
  - `lipis/flag-icons` (i18n flags)
  - `wreq-js` native (excluded anyway)
  - `codex-chatgpt-web` (excluded)
  - `gcf-typescript`
  - **Do not copy provider logos wholesale.** Use our own or official press-kit icons for our ~12 providers, or a
    neutral icon set, with nominative-use disclaimer text like OR's §"Trademark and affiliation disclaimer".
- 9R has no THIRD_PARTY_NOTICES. Its `public/providers` and `public/icons` have no stated provenance, so the same
  caution applies.
- OR renders logos through the `@lobehub/icons` npm package (`src/shared/components/ProviderIcon.tsx`,
  `lobeProviderIcons.ts`). If we reuse that component, keep the package's license and apply the same trademark
  caution.
- UI npm dependencies (recharts, @xyflow/react MIT; monaco-editor MIT; @dnd-kit MIT; material-symbols Apache-2.0;
  zustand MIT) are compatible. Record them in our notices through the package manager's license report.
- OR's `AGENTS.md` and `CLAUDE.md` contain operator-specific paths and rules. Do not copy them.
