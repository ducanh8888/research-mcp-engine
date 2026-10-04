# Frozen main — code audit

Reviewed 2026-10-05 at `8112a5475fcf7f8ec5339fa8c80e5a3c95e5f2b1`.
This is evidence for [cleanup](cleanup.md) and [roadmap](roadmap.md), not a claim that
the implementation is fixed or accepted. `main` remains frozen.

## Scope and method

Followed public MCP tools through request validation, routing, account selection, HTTP/adapters,
aggregation, cache, request persistence and async jobs; also reviewed admin/auth/OAuth boundaries,
schema initialization/migrations, CLI/container and the relevant fixtures.
Source/test/packaging files matched the frozen remote Git blob hashes, accounting for the
extra newline introduced by snapshot retrieval; no source mutation was used for the probes.

Eight deterministic probes executed the original functions extracted from their source AST,
with controlled fake transports/providers/storage and the installed Pydantic models. They made
no upstream calls and changed no application code. They demonstrate local logic; they do not
exercise SQLAlchemy, FastMCP, the locked SDK, a real DB or network integration.

Python was 3.12.14; installed Pydantic was 2.13.5, not a verified locked environment.
SQLAlchemy, HTTPX, MCP/FastMCP, pytest and Ruff were unavailable in this execution environment.
The local audit snapshot did not materialize the repository's remote `uv.lock`.
Full frozen-lock lint/tests, container and live acceptance remain pending, never PASS.

## Reproduced source behavior

| Input/fault | Observed behavior | Cleanup |
|---|---|---|
| Default HTTP helper, POST, first response 500 then 200 | Two POST submissions | C15 |
| Metadata A from provider one, B from provider two | Both records returned; only provider one in `coverage.ok` | C18 |
| Metadata B unresolved, later provider times out | Partial result still labels B `not_found` | C18 |
| Sequential read, first provider returns `Result()` | Complete/ok, `document=null`; second provider skipped | C19 |
| Bibliographic match from one source, mismatch from another | Aggregate says `match` | C20 |
| Internal `RuntimeError` after request creation | Only initial request write, no terminal result | C26 |
| Target error, three eligible accounts for same provider | Same target called with all three accounts | C24 |
| Firecrawl start receives public `max_depth=5` | Sends `maxDiscoveryDepth=2` | C16 |

Implementing changes must turn these cases into maintained regressions using actual boundaries.
The temporary fake harness is diagnostic evidence, not a new production/testing abstraction.

## Additional static findings

| Source location | Concrete finding | Cleanup / priority |
|---|---|---|
| `storage/db.py:Account`; `router/execute.py:_availability/test_account`; admin Reset | List-backed ORM field has incompatible dict writers | C14 / P0.1 |
| `providers/scholar/elicit_api.py:start`; `common.py:json_request`; `providers/http.py:request` | Async start reaches retrying helper before ambiguity is classified | C15 / P0.1 |
| `server/tools.py`; Firecrawl/S2/scholar search adapters | Public depth/claim/year names do not reach their intended behavior | C16 / P0.1 |
| OpenAlex/S2 `call`; scholarly metadata lookup paths | Related modes/later seeds ignored; plain citation metadata resolver absent | C17 / P3 |
| `server/schemas.py:Hit/Result`; `_call/_execute` | Optional payload bag and implicit extra `id` hide capability errors | C19 / P0.1 |
| `_payload(CITATION_GRAPH)` | Provider graphs concatenated without endpoint/identity reconciliation | C21 / P3 |
| `config.py`; `server/tools.py`; `_validate/_execute` | Response-byte setting unused; ordinary deadline differs from design | C22 / P0.1 |
| Scholar options; `_call`; graph/related subrequests | Declared rate/concurrency settings are not consumed by actual limiter | C23 / P0.2 |
| `jobs/runner.py:create/get/cancel/_poll_once/_load/_persist` | Synchronous transactions run inside async execution | C25 / P0.2 |
| `_call/test_account`; `JobRunner._error_text`; Firecrawl poll errors | Redaction differs by entry point and misses non-top-level/token secrets | C27 / P0.1 |
| `storage/migrations/v0001_core.py`, `v0002_cache.py`, `v0003_jobs.py` | Historical DDL depends on mutable current model; create-only is not upgrade | C28 / P0.1 |
| `storage/db.py:Database.initialize`; admin route validation | Insert-only catalog leaves shipped capabilities stale on existing DBs | C29 / P0.2 |
| `cache.py`; `storage/blobs.py:cleanup`; request/job tables | Lazy expiry, no blob-cleanup caller, no explicit result retention | C30 / P0.2 |
| `tests/test_mcp_e2e.py`; direct adapter fixtures | Missing `json` import; fixtures bypass public field names | C31 / P0.1 |
| `Dockerfile` | Runtime install does not use the committed lock | C32 / P0.1 |

C01–C13 remain open from the earlier source review. A static finding is not described as
a reproduced ORM, concurrency, migration, secret-leak or container failure.

## Refactor boundaries for the fix plan

The concrete problem is repeated, inconsistent ownership of contracts/state: tool parameters
versus adapter dictionaries; list versus dict account blocks; adapter calls versus actual HTTP
limits; merged evidence versus attempt coverage; router versus Test/job redaction and persistence.

Correct behavior first, then extract only the boundaries needed by those corrections:

- Validated capability requests/results and explicit provider translation (C16/C17/C19).
- One account availability/error/limit path shared by normal calls, Test and applicable jobs
  (C04–C06/C14/C23/C24), preserving explicit account choice and restart state.
- Separate evidence aggregation from attempt coverage (C18/C20/C21).
- Short storage operations and one request/error finalization/redaction boundary (C25–C29).

Keep one execution pipeline and small ordinary functions/modules. Do not introduce generic
workflow/plugin frameworks, nested routing, a worker control plane or speculative future hooks.
Module length and a broad exception alone are not findings; the observable broken contracts above
determine each change and its regression check.
