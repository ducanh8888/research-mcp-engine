# 01 — Deep read: `vvzvlad/research-mcp`

Status: research input for the design doc. Written 2026-09-29.
Source: `github.com/vvzvlad/research-mcp`, HEAD `11f297d` (2026-09-19, shallow clone, 1 commit visible), MIT.
All paths below are relative to the repo root. Line numbers refer to that commit.
Local copy: `/tmp/claude-1000/-home-testosterone-build-research-mcp-engine/3f05c6cc-0eb2-4378-ac56-515dad3cdba9/scratchpad/repos/research-mcp`.

**Verified locally:** the full suite passes (Python 3.12, `requirements-dev.txt`): **496 passed in 27 s, 93 % line coverage of `src/`**.

---

## TL;DR

- This is a small codebase (~3.1 kLOC in `src/`, ~6.8 kLOC of tests). It is well commented and defensively written. It is a **stateless web search/read facade**: 13 search types and 7 read types behind 4 MCP tools (`web_search`, `read_page`, `read_pages`, `search_and_read`).
- **Worth copying almost verbatim:** `_http.py` (retry, 402/429 and credit-body detection), `_url_guard.py` (SSRF), `failure_reason.py` (with the taxonomy extended), `rerank.py` (Jina rerank with strict validation), `pdf.py`, the read-chain algorithm (probe → PDF → thin-content cascade → best-thin fallback), `search_and_read` waves, `ClientManager`, and all **20 provider adapters**. They are cheap to port because every adapter is roughly 50–150 lines of parse logic with tests.
- **Must be replaced or generalized:** `pipeline_config.py` (config in code, driven by env var names), the `SearchProvider`/`ReadProvider` protocols (two hard-coded kinds, web-only result shape), first-wins URL dedup (replace with canonicalize → cluster → RRF), `formatting.py` (Russian, and text-only), `server.py` (4 fixed tools), and in-memory accounting and throttling (move to the account pool and health).
- **Missing entirely:** academic providers, identifier canonicalization, account state machine, circuit breaker and cooldown memory, capability routing, per-call cost gating on the search fan-out, caching, handles, async jobs, auth, persistence, structured MCP output.

---

## 1. Architecture summary

### 1.1 Layers

```
main.py ─ loguru sinks → build_server(settings) → FastMCP.run("streamable-http")
src/server.py          4 @mcp.tool wrappers, caps, try/except → str / dict
src/pipeline.py        Pipeline.build (instance loader), ClientManager, search(), read(), search_and_read()
src/pipeline_config.py INSTANCES / SEARCH_PIPELINE / READ_PIPELINE / PAID_TYPES (Python constants)
src/providers/*        20 adapters + base.py (protocols) + registry.py + _http.py + _url_guard.py + pdf.py
src/rerank.py          JinaReranker (post-merge)
src/failure_reason.py  exception → category
src/formatting.py      category → Russian label; status lines; result list rendering
src/settings.py        pydantic-settings, non-secret knobs only
src/config_errors.py   ValidationError → readable exit(1)
```

### 1.2 Type/instance registry

- `providers/registry.py:12-26`: a global `REGISTRY: dict[str, type]` and a `@register("type")` class decorator that raises on duplicates. `providers/__init__.py:11-31` imports every module for the side effect.
- A **type** is a class. An **instance** is `Instance(name, type, url_env, api_key_env, token_env, proxy_env, optional_api_key)` (`pipeline_config.py:22-39`). The instance holds **env var names**, never values. `optional_api_key` exists for keyless Jina.
- `_resolve_instance` (`pipeline.py:200-221`) calls `os.getenv` on those names. A missing required var returns `None`, and the instance is **silently disabled** with a log line. The proxy is always optional.
- `Pipeline.build` (`pipeline.py:355-447`) does the rest:
  - It copies shared knobs (`request_timeout`, `fallback_min_chars`, `retries`) into `ProviderConfig`.
  - It injects the per-type `options` through a hard-coded `if inst.type == "jina"` (`:377-380`).
  - It instantiates each class and guards `__init__` exceptions.
  - It orders instances by `SEARCH_PIPELINE`/`READ_PIPELINE`.
  - It raises `ConfigError` if either list is empty.
  - It wires the reranker directly from `os.getenv("JINA_API_KEY")` (`:431-438`).
- Multiple instances of one type are supported (`tavily-1`/`tavily-2`, `pipeline_config.py:119-120`). This is the only "account pool" that exists: a static, ordered failover list.
- `ProviderConfig` (`base.py:40-59`) holds `name, request_timeout, fallback_min_chars, retries, url, api_key, token, proxy, options: dict[str,str]`. Generic slots are overloaded: `token` means a Bright Data zone, an XMLRiver user id, or a Crawl4AI bearer, depending on the type.

### 1.3 `pipeline_config.py`

- `INSTANCES` (`:50-132`): 13 search instances and 7 read instances.
- `SEARCH_PIPELINE` (`:159-173`) is a **dedup preference order, not a cost gate**. Every enabled search instance fires on every query (`:134-158`), and the comments say so explicitly.
- `READ_PIPELINE` (`:177-185`) **is** a cost gate. It runs sequentially and stops at the first sufficient answer, which is why Bright Data is last.
- `PAID_TYPES` (`:195-213`) is used only for log accounting (the `paid_calls`/`paid_pct` fields).
- The in-code comments hold valuable operational facts: prices, measured quotas, and shared credit pools. For example, Tavily and Firecrawl search and extract share one monthly pool (`:68-83`), and Brave's free plan is 1 req/s and 2000/month. Move these into the provider notes and YAML defaults.

### 1.4 Search pipeline (`Pipeline.search`, `pipeline.py:486-612`)

1. `asyncio.gather` over **all** enabled search instances (`:522`). Each instance uses the httpx client bound to its proxy (`:508`).
2. `_one` never raises. It returns `(name, hits|None, reason)`, and both `ProviderError` and any other `Exception` are classified (`:503-517`).
3. Instances are bucketed into disjoint `answered` / `empty` / `failed(+reasons)` groups (`:526-541`). This distinction between an honest empty result and a broken search is a good design point; keep it.
4. **Merge and dedup:** iteration follows pipeline order. The key is `_normalize_url`:
   - lowercases scheme and host, strips the fragment and trailing `/`, and keeps the query (`:188-197`);
   - **the first occurrence wins**, and a later provider's copy is dropped completely, along with its snippet and its "source" credit (`:543-548`);
   - no cross-provider agreement is recorded.
5. **Rerank** runs before the trim, on the full merged list, only if a reranker exists and `len > 1` (`:557-571`). On any error the merge order is kept.
6. The list is trimmed to `num_results` (`:572`).
7. Accounting: `billed = answered (+ "jina-rerank")` (`:583`). The counters are in-memory and reset on restart. There is one structured log line per request (`:586-601`).

Concurrency and latency facts:
- There is **no overall deadline**. `gather` waits for the slowest provider, bounded only by the httpx timeout of 25 s per request × (1 + retries) plus backoff.
- There is no per-provider semaphore.
- Local throttles are **skip-not-wait**, with state inside the provider object:
  - searxng and duckduckgo allow 1 query per 45 s (`searxng.py:25,79-84`; `duckduckgo.py:72,173-178`);
  - brave allows 1 per 1.1 s (`brave.py:106,170-175`).
  A skip raises `ProviderError("throttled")`, which is classified as `rate-limit`. These throttles are process-local and not persisted.

### 1.5 Read pipeline (`Pipeline.read`, `pipeline.py:616-795`)

1. **SSRF entry check** (`:626`).
2. **Probe**: one GET through the guarded direct client (`:667`, `_probe` `:818-854`, `_probe_fetch` `:856-885`).
   - `looks_like_pdf` checks the `.pdf` suffix, the Content-Type, and the `%PDF` magic (`pdf.py:30-36`). A PDF goes to `extract_pdf_text` (pypdf).
   - HTML keeps the body for trafilatura, so there is no second GET (`_read_one` `:797-816`).
   - A TLS verification failure triggers **one retry with `verify=False`**, still SSRF-hooked (`:869-885`).
   - A probe failure never hard-fails; it defers to the chain.
   - A scanned PDF (no text layer) falls through to the chain, and the "no text layer" notice is kept only as a last resort (`:693-706`, `:759-773`).
3. **Cost-gated chain** (`:712-747`): providers run sequentially in `READ_PIPELINE` order.
   - An exception → `failures.append((name, classify(exc)))`, then continue.
   - A return value counts as a billed call. If `len >= fallback_min_chars` (400), it wins.
   - Otherwise the result is remembered as `best_thin` (the longest) and tagged `EMPTY`.
4. The final answer is one of: `best_thin` (`thin=True`), the PDF notice, or `raise ReadFailed(msg_ru, tried, failures)` (`:791-795`).
5. **Nested escalation inside Jina** (`providers/jina.py:49-80,137-187`), keyed mode only:
   - plain request, then ReaderLM-v2 on the browser engine (3×), then a residential proxy (5×), then `jina-ocr-v1` for `.pdf` paths only (40×);
   - requests use `retries=0`, the longest text wins, and there is an `X-Token-Budget` cap (`settings.jina_token_budget`).
   This is a ready-made example of an in-provider cost ladder.
6. `search_and_read` (`:889-982`): an over-fetched search (`candidates = min(2n+2, 50)`, computed in `server.py:260`), followed by read **waves**. Each wave is exactly as wide as the number of pages still missing, runs under `Semaphore(read_pages_concurrency)`, and never raises for an individual URL. Pages that opened come first, then failures.

### 1.6 `failure_reason` taxonomy (`failure_reason.py`)

- There are 10 constants (`:32-41`): `timeout, rate-limit, no-credits, access-denied, bot-protection, tls, dns, network, empty, other`.
- `classify(exc)` works in two passes:
  1. It walks the exception chain (`__cause__`/`__context__`, max 6) for `httpx.TimeoutException`, `ssl.SSLCertVerificationError`/`CERTIFICATE_VERIFY_FAILED`, `socket.gaierror`/DNS text, and `httpx.TransportError` (`:65-84`).
  2. It falls back to **substring matching on the project's own `ProviderError` messages**, such as `"rate limited"`, `"throttled"`, `"HTTP 429"`, `"out of credits"`, `"HTTP 402"`, `401/403`, `"bot protection"`, and the empty markers (`:87-119`).
- `dominant_reason` (`:122-137`) returns the most frequent category, with ties going to the earliest.
- This is a **stringly-typed** protocol. It works because the same codebase writes the messages, but it breaks silently if a message is reworded. Our port should put a `reason` enum on typed exceptions and keep the text matching only as a fallback for foreign exceptions.

### 1.7 `_http.request_with_retry` (`providers/_http.py:64-115`)

- **Transient errors are retried** `retries` times with a fixed 0.3 s backoff (`:32`). These are `TransportError`, `TimeoutException`, `RemoteProtocolError`, and 5xx.
- **402 and 429** raise immediately with no retry (`:93-96`). This is the failover trigger: tavily-1 → tavily-2.
- **Any other 4xx** raises `client error (HTTP n)`. If the body contains one of `_CREDIT_MARKERS` (`"not enough credits"`, `"insufficient credits"`, `"insufficient balance"`, `"out of credits"`, `:45-50`), it is instead reported as `out of credits (HTTP n)`. This covers Serper returning 400 and Octen returning 403 for an empty balance (`:34-44`).
- What is missing:
  - `Retry-After` and `x-ratelimit-*` headers are ignored;
  - there is no jitter or exponential backoff;
  - there is no distinction between 401 (auth) and 403 (plan or permission);
  - the response is not attached to the error, so a caller cannot read the headers.
- Providers override retries per call where retrying hurts: searxng, duckduckgo, and brave use `retries=0`, and so do rerank and the Jina escalations.
- DuckDuckGo's soft block is HTTP 202 with a "Ratelimit" body, which is handled in the provider (`duckduckgo.py:210-211`). This is an example of a provider-specific quota signal that the shared helper cannot see. Our interface needs a per-provider `classify_response` hook.

### 1.8 `_url_guard` SSRF (`providers/_url_guard.py`)

- Only `http` and `https` schemes are allowed (`:39`). A missing host is denied.
- There is an explicit CIDR blocklist: 0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16, ::1, fc00::/7, and fe80::/10 (`:45-59`). A catch-all also blocks anything where `not ip.is_global or ip.is_multicast` (`:62-69`).
- A hostname is resolved with `getaddrinfo`, and **every** returned address must pass (`:118-129`).
- An **unresolvable host is allowed through**, deliberately, for proxy-side DNS (`:92-96,118-125`).
- It is applied twice:
  - once at the entry of `Pipeline.read` (`pipeline.py:626`);
  - once as an httpx `event_hooks["request"]` on the "guarded" clients, which runs on every redirect hop (`pipeline.py:273-304`).
- Only the probe and trafilatura use guarded clients (`pipeline.py:667,815`). Third-party readers (Jina, Tavily, and so on) receive the URL but fetch it server-side.
- `settings.allow_private_network` disables the address check but not the scheme check.
- Caveat: **TOCTOU / DNS rebinding.** The guard resolves the name, then httpx resolves it again. See §6.

### 1.9 Rerank (`src/rerank.py`)

- `JinaReranker.rerank` POSTs to `api.jina.ai/v1/rerank` with the model `jina-reranker-v3.5`. Each document is `title\nsnippet` (or the URL if both are empty), capped at 1000 chars (`:74`).
- It uses `retries=0` and a 5 s timeout (`:39,102-106`), because it is a serial step and the fallback is graceful.
- Strict response validation (`:117-149`):
  - a non-int, bool, or out-of-range index raises;
  - duplicate indices are dropped;
  - an empty ranking for a non-empty input raises;
  - a partial ranking is **padded** with the unranked items so that it reorders and never filters.
- The relevance scores are **discarded**. Only the order is kept, so a score cannot become a signal.
- It is not pluggable. The concrete class is wired in `Pipeline.build`, and there is no interface.

### 1.10 Formatting (`src/formatting.py`)

- It is pure and does no I/O.
- The Russian labels for the failure categories are at `:23-34`.
- It renders search results as a numbered Markdown list (`:64-92`). The code distinguishes "search failed" (every instance raised) from "nothing found" (`:73-80`).
- There is one **status line** per tool answer (search, read, read-failure, batch, search+read; `:95-187`) giving answered/attempted, hits before and after dedup, empties, error categories, and elapsed time.
- `truncate_markdown` (`:145-156`) appends an explicit "dropped N chars" marker. Every user-facing string here is **Russian**.

### 1.11 Server tool definitions (`src/server.py`)

- `FastMCP` from `mcp.server.fastmcp`, SDK 1.28.0, streamable-http, with the host and port from settings (`:80-85`). A lifespan hook closes the httpx clients.
- **There is no auth.** Traefik basicAuth is expected in front.
- Tools:

  | Tool | Signature | Returns | Notes |
  |---|---|---|---|
  | `web_search` | `(query, num_results=8, page=1, language=None) -> str` | Markdown list + status line | `num_results` is clamped to 1..50 (`:116`) |
  | `read_page` | `(url) -> str` | full Markdown + `---` + status | never truncated |
  | `read_pages` | `(urls: list[str]) -> {summary, pages[]}` | per-URL `{url, ok, markdown\|error, reason}` | capped at 20 URLs (`:186`); each page capped at 20k chars |
  | `search_and_read` | `(query, num_results=5, page=1, language=None) -> {summary, results[]}` | `{title, url, snippet, ok, markdown\|error, reason}` | over-fetch `2n+2` |

- The descriptions are English and cross-reference each other as a small routing graph (`:87-248`). `tests/test_server.py:145-178` pins the exact wording.
- Numeric strings are coerced by FastMCP's lax pydantic validation (`:56-61`, pinned by a test at `test_server.py:456`).
- There is no `outputSchema` or structured content, no annotations (`readOnlyHint`), and no ChatGPT `search`/`fetch` shape.

### 1.12 Settings (`src/settings.py`, `config_errors.py`)

- `Settings(BaseSettings)` reads `.env` with `extra=ignore`. Every field has a default:
  - `mcp_host`, `mcp_port`;
  - `log_level`, `log_file`, `log_rotation`, `log_retention`;
  - `request_timeout=25`, `fallback_min_chars=400`, `read_pages_concurrency=5`, `read_batch_max_chars=20000`, `retries=1`;
  - `search_rerank_enabled`, `jina_token_budget=100000`, `allow_private_network`.
- Secrets are deliberately **not** settings fields. They are read by name in the loader.
- `config_errors.load_settings_or_exit` turns a pydantic `ValidationError` into a readable message that names the env var, then calls `exit(1)`. It is small and reusable.
- `main.py` sets up two loguru sinks: stderr, and a rotating file at `data/research-mcp.log` with `enqueue=True`.

### 1.13 Provider inventory

Two kinds of provider: search and read.

**Search** providers return a `SearchResult` of title, url, snippet, and source only:
- `searxng` (self-hosted);
- `duckduckgo` (keyless HTML scrape, lxml);
- `brave`;
- `jina_search`;
- `tavily_search`;
- `firecrawl_search`;
- `xmlriver_search` (Yandex XML SERP);
- `parallel_search`;
- `octen_search`;
- `linkup_search`;
- `youcom_search`;
- `serper`;
- `exa`.

**Read** providers return a Markdown string:
- `trafilatura` (local);
- `jina` (escalation ladder);
- `crawl4ai` (self-hosted);
- `tavily` (extract);
- `firecrawl` (scrape v2);
- `brightdata` (Web Unlocker).

**The providers throw metadata away.** Several upstream fields are dropped by the adapters:
- Tavily: `score`, `published_date`;
- Exa: `publishedDate`, `author`, `score`;
- Octen: `authors`, `time_published`;
- Parallel: `publish_date`;
- You.com: `page_age`.

We need those fields for signals (freshness, provider score).

Four adapters (parallel, octen, linkup, xmlriver) were written from the vendor docs without a live key. Their docstrings say "NOT verified against a live call" (`parallel_search.py:5`, `octen_search.py:4`, `linkup_search.py:7`).

---

## 2. Module-by-module map

Target module names follow our planned repo boundary: `server`, `auth`, `providers`, `router`, `accounts`, `normalization`, `canonicalization`, `dedup`, `fusion`, `reranking`, `health`, `storage`.

| File (LOC) | What it does | Verdict | How | Target |
|---|---|---|---|---|
| `main.py` (45) | loguru sinks, build, run streamable-http | **DROP** | The app shell comes from mcp-gateway (ASGI, auth, lifespan). Keep the loguru file-sink idea if we stay on loguru. | server |
| `src/settings.py` (69) | pydantic-settings, non-secret knobs | **MODIFY** | Keep it as process-level settings (host, port, log, timeouts, SSRF flag). Provider, account, and pipeline config move to YAML plus an encrypted DB. Rename the knobs into a `defaults:` block. | server (config) |
| `src/config_errors.py` (66) | readable config-error exit | **KEEP** | Extend it to report YAML validation errors with a path. | server (config) |
| `src/pipeline_config.py` (213) | instances + pipeline orders + paid types, in code | **DROP (data → YAML)** | Port the *content*: instance list, orders, prices, and quota comments. Instances become `accounts` rows. Orders become router policy (`combo`/fallback chains). `PAID_TYPES` becomes a per-provider `cost_model`. | accounts, router (policy YAML) |
| `src/pipeline.py` (982) | loader, ClientManager, search/read/search_and_read | **SPLIT + MODIFY** | See the sub-rows below. | several |
| ↳ `Pipeline.build` / `_resolve_instance` (`:200-221,355-447`) | env→config, instantiate, order | **REWRITE** | Replace with an `AccountRegistry` that loads from YAML and the DB. Instantiate adapters per (provider, account) lazily. Replace the `if type=="jina"` options hack with typed per-provider option schemas. | accounts, providers |
| ↳ `ClientManager` (`:224-312`) | httpx client per proxy, guarded variant | **KEEP** | Move as-is. Add pool-limit settings. Key by (proxy, guarded). | providers (transport) |
| ↳ `search()` (`:486-612`) | gather all, bucket, first-wins dedup, rerank, trim, accounting | **MODIFY heavily** | Keep: gather-never-raise, the answered/empty/failed buckets, and rerank-before-trim with graceful fallback. Replace the fan-out selection with router + account pool + cost budget + deadline (`asyncio.wait(timeout=)`, keeping partial results). Replace dedup with canonicalize → cluster → RRF. Replace the accounting with usage events to storage. | router, fusion, dedup, health |
| ↳ `read()` / `_read_one` / `_probe*` (`:616-885`) | probe, PDF, sequential cost-gated chain, best-thin | **KEEP (algorithm), MODIFY (plumbing)** | This becomes the `fetch`/`read` capability executor. Replace the `isinstance(TrafilaturaRead)` special case with a `accepts_prefetched_body` capability flag. Hand the probe verdict (is_pdf) down to providers, which fixes the Jina OCR gate (`:684-692`). Add a max-bytes cap to the probe. Cache results by canonical URL. | router (read executor), providers |
| ↳ `search_and_read` (`:889-982`) | over-fetch + read waves | **KEEP** | Generic as a "search then enrich top-k" composer. It should also be usable as an async job. | router |
| ↳ `_normalize_url` (`:188-197`) | URL dedup key | **MODIFY** | Superseded by a real URL canonicalizer: strip `utm_*`/`fbclid`/…, drop `www.`, http→https equivalence, sort the query, decode percent-encoding safely. Also DOI, arXiv, and PMID extraction from URLs. | canonicalization |
| ↳ `ReadOutcome`/`SearchOutcome`/`ReadItem`/`ReadFailed` | telemetry DTOs | **MODIFY** | Keep the telemetry fields (`attempted/answered/empty/failed/failed_reasons/elapsed_ms/tried/failures/thin`). Generalize them into `CapabilityOutcome` and per-attempt `AttemptRecord`s, persisted to the request log (needed for admin "request log + replay"). | normalization, storage |
| `src/providers/base.py` (105) | `SearchProvider`/`ReadProvider` protocols, `SearchResult`, `ProviderConfig`, `ProviderError`, UA | **REWRITE (generalize)** | See §4. Keep `ProviderError` as the root of a typed hierarchy and keep `BROWSER_USER_AGENT`. | providers |
| `src/providers/registry.py` (26) | `@register` + `REGISTRY` | **KEEP, extend** | Register `ProviderSpec` (capabilities, auth kind, cost model, option schema) and not only the class. Optionally use entry points. | providers |
| `src/providers/__init__.py` (31) | side-effect imports | **KEEP** (or `pkgutil` auto-discovery) | | providers |
| `src/providers/_http.py` (115) | retry, 402/429 failover, credit-body markers | **KEEP + MODIFY** | Return typed errors carrying `status`, `retry_after`, `headers`, and a `reason` enum. Distinguish 401 → `AUTH_REQUIRED` from 403 → `PLAN_BLOCKED`/access-denied. Parse `Retry-After` and `x-ratelimit-*` into health hints. Add jittered exponential backoff. Keep `_CREDIT_MARKERS` and extend it (Linkup 429 "insufficient credits", Tavily wording unverified). | providers (transport), health |
| `src/providers/_url_guard.py` (129) | SSRF guard | **KEEP** | Replace the Russian messages with English. Optionally pin the resolved IP to close the DNS rebinding gap (§6). | providers (transport) / security |
| `src/providers/pdf.py` (51) | PDF detect + pypdf text | **KEEP** | Translate the notice. Consider `pypdf` → `pymupdf` or GROBID later for scholarly PDFs (sections, references). | providers (read) |
| `src/providers/trafilatura.py` (65) | local fetch + extract | **KEEP** | | providers |
| `src/providers/jina.py` (190) | Reader + escalation ladder | **KEEP** | Expose the ladder steps as cost tiers, so the per-call budget decides how far to climb. | providers |
| `src/providers/crawl4ai.py` (47) | self-hosted headless | **KEEP** (optional provider) | | providers |
| `src/providers/tavily.py` (63) | Tavily extract | **KEEP** | Bind it to a Tavily account shared with `tavily_search`: the same credit pool (see `pipeline_config.py:68-83`). | providers, accounts |
| `src/providers/firecrawl.py` (50) | Firecrawl scrape v2 | **KEEP or MODIFY** | The requirements say implement via `firecrawl-py`. The raw httpx adapter works and is tested, so decide per provider in the design doc (raw httpx keeps uniform error handling). | providers |
| `src/providers/brightdata.py` (78) | Web Unlocker | **KEEP** (optional, last-resort read) | | providers |
| `src/providers/searxng.py` (125) | SearXNG JSON | **KEEP**; **MODIFY** the throttle | Move `_last_call` skip-throttle state into the account rate limiter (health), persisted. Make the interval configurable. | providers, health |
| `src/providers/duckduckgo.py` (292) | keyless DDG HTML scrape | **KEEP** (zero-config floor) | Same throttle move. The markup-change detection (`:214-292`) is excellent; keep it. | providers |
| `src/providers/brave.py` (242) | Brave API + language mapping | **KEEP**; move the throttle | Extract the returned `age`/`page_age` into metadata. | providers |
| `src/providers/serper.py` (72) | Serper Google | **KEEP** | Add `/scholar` (Google Scholar) as an extra capability later. | providers |
| `src/providers/exa.py` (76) | Exa search | **MODIFY** | Keep `publishedDate`, `author`, and `score`. Add `contents`/`highlights`, `category: "research paper"`, and a find-similar capability. The requirements say `exa-py`, but raw httpx is fine. | providers |
| `src/providers/{tavily,firecrawl,jina,linkup,parallel,octen,youcom}_search.py`, `xmlriver_search.py` | web search adapters | **KEEP** | Preserve the dropped fields (score, published date, authors). The four "unverified" adapters need live verification before we trust them. | providers |
| `src/rerank.py` (150) | Jina rerank | **KEEP + MODIFY** | Put it behind a `Reranker` protocol (Jina API, Cohere API, local cross-encoder). **Return the scores** and do not discard them. Rerank runs *after* RRF fusion, on cluster representatives. | reranking |
| `src/failure_reason.py` (137) | exception → category | **KEEP + MODIFY** | Its categories become the basis of the health/account state transitions (§3.2). Add `auth-required`, `plan-blocked`, `quota-exhausted`, `upstream-5xx`, `parse-error`, `bad-request`, and `throttled-local` (separate from remote 429). Classify from typed error fields first. | health |
| `src/formatting.py` (187) | Russian status lines + Markdown list | **MODIFY → rewrite in English** | Keep the idea of a one-line status line (answered x/y, hits → results, errors by category, elapsed) as a structured `coverage` object plus an optional text rendering. Keep `truncate_markdown` with an English marker. | server (presentation) |
| `src/server.py` (307) | 4 FastMCP tools | **MODIFY → rewrite** | Replace with one tool per capability, with structured output, annotations, handles, and the compact+expand shape. Reuse the description style (cross-referenced routing hints) and the caps-as-constants rule. The over-fetch formula and wave logic move to the router. | server |
| `tests/*` (6.8 kLOC) | respx-mocked unit + pipeline tests | **KEEP** (per kept module) | Port the provider tests together with their adapters. Rewrite `test_pipeline.py`/`test_server.py` against the new router and tools, reusing their scenarios as a spec. | tests |
| `tests/fixtures/read_cases.yaml` | manual benchmark of 20 hard URLs | **KEEP** | Seed for an opt-in live read-quality benchmark. | tests (bench) |
| `Dockerfile`, `docker-compose.yml`, `Makefile`, `.gitea/workflows/*`, `server.json` | ops | **REFERENCE** | Keep the `make` + venv discipline and a separate test workflow. Compose and Traefik are replaced by the Cloudflare Tunnel setup. | — |

---

## 3. Gaps vs our requirements

### 3.1 Scope and domain

| Requirement | research-mcp today | Gap / action |
|---|---|---|
| **Academic providers** (OpenAlex, Crossref, Semantic Scholar, arXiv; Scite, Elicit, Undermind, and Consensus via MCP) | none; web SERP and readers only | New adapters. The result model must carry DOI, arXiv id, PMID, authors, year, venue, OA status, and citation counts. A new *transport kind* is needed: an **MCP client over OAuth** (upstream MCP tools as a provider). There is no precedent in this repo; take it from mcp-gateway. |
| **Capability routing** (one MCP tool per capability, consumer-chosen, rule fallback) | two hard-coded kinds (search, read) and 4 fixed tools | Introduce a `Capability` enum and a `ProviderSpec.capabilities` set. The router selects candidate (provider, account) pairs per capability. See §4. |
| **Account pool state machine** READY / COOLING / RATE_LIMITED / EXHAUSTED / AUTH_REQUIRED / PLAN_BLOCKED / DEGRADED / DISABLED | none. An instance is either built or not, at startup. A failure affects only the current call and is **re-attempted on every request**. | New `accounts` + `health` modules. Mapping from existing signals: see §3.2. |
| **Circuit breaker / cooldown** | none. Local skip-throttles exist only for searxng, ddg, and brave, and are in-memory. | Port from OmniRoute/9router (task 3). Generalize the skip-throttle into a per-account token bucket or minimum interval, persisted. |
| **Config in code → YAML** | `pipeline_config.py`, with secrets via env var names | YAML for providers, accounts (secret refs), and routing policy (chains, fan-out width, fusion weights, reranker toggle). Secrets go in Fernet-encrypted SQLite (mcp-gateway), with an env-ref option for bootstrap. Keep the rule "config holds references, never values". |
| **Stateless → cache / handles / jobs** | stateless; only a log file | Query cache (normalized query + capability + params → fused result, TTL); document cache (canonical id → content, TTL); **evidence handles** (stable canonical IDs); **async jobs** persisted in SQLite; a request log for replay. |
| **Russian strings** | status lines, labels, `ReadFailed`, SSRF errors, PDF notice, `"Непредвиденная ошибка"` | Grep targets: `formatting.py:23-34,76-80,82,84,103-187`, `pipeline.py:792,930`, `server.py:204`, `_url_guard.py:75,100,104,115,129`, `pdf.py:24-27`. Replace with English and machine codes; the consumer renders them. |
| **Per-call cost gating** | Read: an order-based cascade (good). Search: **every enabled provider on every query**, and the cost scales with the number of enabled providers. Jina has a token budget. | The router needs a per-call `budget` (money, credits, or a tier), a per-provider `cost_model.estimate(request)`, and a fan-out policy (e.g. "free tier always, paid up to N, or until k results with agreement"). Credit pools are shared across capabilities (Tavily and Firecrawl search+extract). |
| **RRF fusion instead of first-wins dedup** | first occurrence by pipeline order wins; later copies are discarded | Keep each provider's rank list. Canonicalize → cluster (DOI > arXiv > PMID > canonical URL > fuzzy title+year) → RRF (`Σ w_p / (k + rank_p)`) → cluster representative with merged metadata and full provenance (which providers and at which rank). This also yields the **cross-provider agreement** signal. |
| **Signals / provenance** | only `source` (the winning instance). Scores and dates are dropped. | Keep the provider rank and score, published date, domain, and all providers per cluster. Keep the rerank score. |
| **Structured MCP output / ChatGPT search+fetch** | text and ad-hoc dicts | Pydantic output models → `structuredContent` + `outputSchema`. `search`/`fetch` compatibility is decided in the design doc (task 6). |
| **Auth** | none (Traefik basicAuth) | OAuth 2.1 AS from mcp-gateway. |
| **Admin / observability** | log lines with `paid_calls`/`paid_pct`; in-memory counters | Persist usage events (provider, account, capability, cost, outcome, reason, latency) → health/quota dashboard, request log, replay. |
| **Deadline / latency** | none (the slowest provider gates the answer) | The router needs an overall deadline (10–30 s is acceptable) with partial results; each attempt has a timeout. |
| **Pagination / language** | `page` and `language` are in every search signature; each provider has its own mapping or refusal | Keep the language-mapping helpers (brave, youcom, octen, ddg, xmlriver). Make pagination a capability parameter with declared support (`supports_paging`). |

### 3.2 Mapping existing failure signals → account states

| Signal in research-mcp | Where | Proposed state transition |
|---|---|---|
| HTTP 429 `rate limited` | `_http.py:93-96` | → `RATE_LIMITED` until `Retry-After` or a policy cooldown, then `READY` |
| Local skip `throttled` | `searxng.py:81`, `duckduckgo.py:174`, `brave.py:171` | not an account state; the limiter skips the account for this call (`COOLING`-lite) |
| DDG 202 "Ratelimit" | `duckduckgo.py:210` | → `RATE_LIMITED` (7–8 min per the measurements in `searxng.py:18-25`) |
| HTTP 402 / `_CREDIT_MARKERS` body / xmlriver error 200 | `_http.py:93,109`; `xmlriver_search.py:35` | → `EXHAUSTED` until the quota window resets or a manual top-up |
| Linkup 429 "Rate limit exceeded **or insufficient credits**" | `linkup_search.py:37-38` | ambiguous; needs body parsing → `RATE_LIMITED` or `EXHAUSTED` |
| HTTP 401 | `_http.py:111` (today a generic `client error`) | → `AUTH_REQUIRED` (for OAuth MCP providers: refresh, then re-consent) |
| HTTP 403 | same | → `PLAN_BLOCKED` if the capability is plan-gated, else access-denied for that URL (a read: **not** an account problem) |
| Brave page > 10, Tavily/Firecrawl/Linkup `page > 1` | `brave.py:143`, `tavily_search.py:75`, … | not a failure; the capability is unsupported, so the router should not select the account |
| Repeated timeout / 5xx / parse error | `failure_reason` timeout/network/other | → `DEGRADED` (circuit half-open after N failures in a window) |
| `ProviderError` in `__init__` (missing url/key/zone) | `Pipeline.build:395-399` | → `DISABLED` (misconfigured) |

Important: for **read**, a per-URL failure (403 or bot protection on the target site) must **not** degrade the reader account. research-mcp does not have to care because it keeps no state. We do.

---

## 4. Extension points: generalizing the provider interface

### 4.1 What the current interface bakes in

- There are exactly two operation shapes: `search(client, query, num_results, page, language) -> list[SearchResult]` and `read(client, url) -> str` (`base.py:62-98`).
- The pipeline passes in the httpx `client` (proxy-bound). That is good for testability; keep it as part of a context object.
- `SearchResult` has only title, url, snippet, and source.
- Errors are a single `ProviderError` with a message; the category comes from string parsing.
- There is no declared cost, no rate limit, no capabilities, and no health probe.

### 4.2 Proposed capability-based interface (sketch)

```python
# providers/spec.py
class Capability(StrEnum):
    WEB_SEARCH        = "web.search"
    WEB_READ          = "web.read"           # url -> markdown (research-mcp read chain)
    SCHOLAR_SEARCH    = "scholar.search"     # OpenAlex/S2/Crossref/arXiv/Exa research/Scite/Elicit/Consensus/Undermind
    SCHOLAR_RESOLVE   = "scholar.resolve"    # id/DOI/title -> canonical work metadata
    SCHOLAR_CITATIONS = "scholar.citations"  # cites / cited_by
    FULLTEXT_FETCH    = "fulltext.fetch"     # work -> OA pdf/text
    CLAIM_EVIDENCE    = "claim.evidence"     # Scite smart citations, Consensus
    RERANK            = "rerank"             # also a provider (Jina/Cohere/local)

class TransportKind(StrEnum):
    HTTP = "http"; SDK = "sdk"; MCP_OAUTH = "mcp_oauth"; LOCAL = "local"

@dataclass(frozen=True)
class CostModel:
    unit: Literal["usd", "credits", "tokens", "free"]
    per_call: float = 0.0
    per_result: float = 0.0
    pool: str | None = None            # shared credit pool id, e.g. "tavily:<account>"
    def estimate(self, req: "CapabilityRequest") -> float: ...

@dataclass(frozen=True)
class RateLimit:
    min_interval_s: float = 0.0        # generalizes searxng/ddg/brave skip-throttles
    rps: float | None = None
    monthly_quota: int | None = None
    on_busy: Literal["skip", "wait"] = "skip"

@dataclass(frozen=True)
class ProviderSpec:
    type: str                                  # registry key, e.g. "brave", "openalex", "scite_mcp"
    capabilities: frozenset[Capability]
    transport: TransportKind
    auth: Literal["none", "api_key", "api_key+id", "oauth", "bearer_url"]
    options_model: type[BaseModel]             # typed per-provider options (replaces ProviderConfig.options/token overloading)
    cost: CostModel
    rate_limit: RateLimit
    supports: frozenset[str] = frozenset()     # "paging", "language", "date_filter", "prefetched_body", "pdf", ...
    fetches_target_itself: bool = False        # True → needs SSRF-guarded client (trafilatura-like)
```

```python
# providers/base.py
@dataclass
class CallContext:
    http: httpx.AsyncClient          # from ClientManager (proxy / guarded chosen by router from spec)
    account: "AccountHandle"         # resolved secrets (never logged), account id, plan
    deadline: float                  # monotonic; provider must respect
    budget: "Budget"                 # remaining per-call spend; provider may refuse tiers (Jina ladder)
    hints: dict[str, Any]            # e.g. {"is_pdf": True, "prefetched_html": "..."} — fixes pipeline.py:684-692 / :805

@dataclass
class CapabilityRequest:
    capability: Capability
    query: str | None = None
    url: str | None = None
    ids: list["WorkId"] = field(default_factory=list)   # DOI/arXiv/PMID/OpenAlex/S2
    limit: int = 10
    page: int = 1
    language: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)  # year range, OA only, venue, domain include/exclude

class Provider(Protocol):
    spec: ClassVar[ProviderSpec]
    def __init__(self, options: BaseModel) -> None: ...
    async def execute(self, ctx: CallContext, req: CapabilityRequest) -> "ProviderResult": ...
    def classify_response(self, resp: httpx.Response) -> "ProviderFailure | None":
        """Provider-specific quota/soft-block detection (DDG 202, xmlriver <error code>, jina code!=200,
        octen code!=0, linkup 429 body). Default: None → generic _http policy."""
    async def probe(self, ctx: CallContext) -> "HealthProbe":
        """Optional cheap liveness/quota check (e.g. Serper GET /account balance, Brave x-ratelimit headers)."""
```

```python
# normalization/models.py
@dataclass
class RawHit:                         # what a provider returns, one per item, rank preserved
    provider: str; account: str; rank: int
    title: str; url: str | None; snippet: str
    ids: dict[str, str]               # {"doi": ..., "arxiv": ..., "pmid": ..., "openalex": ...}
    authors: list[str] = field(default_factory=list)
    year: int | None = None; published_at: str | None = None; venue: str | None = None
    provider_score: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)   # citations, OA url, scite tallies, retraction flags

@dataclass
class ProviderResult:
    hits: list[RawHit] = field(default_factory=list)      # search-like capabilities
    document: "Document | None" = None                    # read/fulltext capabilities
    cost_actual: float | None = None                      # e.g. firecrawl creditsUsed, jina usage
    quota_hint: "QuotaHint | None" = None                 # parsed x-ratelimit-*, remaining credits

class ProviderFailure(ProviderError):                     # typed, replaces string-matching
    reason: FailureReason            # enum (failure_reason.py constants + auth/plan/quota/parse)
    scope: Literal["account", "request", "target"]       # target = this URL is unreadable; don't punish account
    retry_after: float | None
    status: int | None
```

### 4.3 How the existing code slots in

- **Adapters:** each `search()` becomes `execute()` for `WEB_SEARCH` and each `read()` becomes `execute()` for `WEB_READ`.
  - The parse bodies stay unchanged; they just emit `RawHit(rank=i, ...)` with the preserved fields.
  - A thin generic `LegacySearchAdapter` wrapper can port all 13 search providers first, with the fields enriched afterwards.
- **`_http.request_with_retry`:** it raises `ProviderFailure` with `reason`, `scope="account"`, `retry_after` (from headers), and `status`. Providers call `self.classify_response` before the generic policy.
- **Skip-throttles** become `spec.rate_limit.min_interval_s` (45 / 45 / 1.1), enforced by the `accounts` limiter before `execute()` is called. The in-object `_last_call` code is removed, but the semantics are kept: skip, stamp before the request, fail closed.
- **The read chain** becomes a `SequentialExecutor` policy: ordered candidates, success predicate `len >= min_chars`, best-thin fallback. **The search fan-out** becomes a `ParallelExecutor` policy: candidates filtered by health and budget, a deadline, partial results. Both live in `router`. `search_and_read` becomes a `Composite` (search → enrich top-k) usable for any "search then fetch" capability, e.g. scholar.search → fulltext.fetch.
- **The Jina escalation ladder** stays inside the provider, but consults `ctx.budget` before each tier.
- **Rerank** becomes `Reranker.rerank(query, items) -> list[(item, score)]`. JinaReranker keeps its strict index validation and padding (`rerank.py:117-149`).
- **`failure_reason.classify`** stays as the fallback for untyped exceptions, e.g. from SDKs.

---

## 5. Dependencies and test infrastructure

### 5.1 Runtime (`requirements.txt`, all pinned)

| Package | Version | Used for | Keep? |
|---|---|---|---|
| `mcp` | 1.28.0 | `mcp.server.fastmcp.FastMCP` | Decided in task 2 (FastMCP standalone vs SDK). mcp-gateway uses FastMCP 4; align on one. |
| `httpx[socks]` | 0.28.1 | all outbound HTTP, per-proxy clients, SOCKS | **Yes** (core transport) |
| `trafilatura` | 2.1.0 | HTML → Markdown | Yes |
| `lxml` | 6.1.3 | DuckDuckGo SERP parse | Yes |
| `pypdf` | 6.14.2 | PDF text layer | Yes (consider an additional scholarly PDF parser later) |
| `pydantic-settings` | 2.7.0 | Settings | Yes (plus a YAML loader, e.g. `pydantic` models + `ruamel`/`pyyaml`) |
| `loguru` | 0.7.2 | logging | Align with mcp-gateway's logging choice; loguru is fine |

Runtime is Python 3.11 in the Dockerfile and CI. The suite also passes on 3.12 (verified here).

### 5.2 Dev

- `pytest==9.0.3`
- `pytest-asyncio==1.4.0` (`asyncio_mode=auto`, function loop scope; `pytest.ini`)
- `respx==0.23.1`

`pythonpath = .` is used so that `import src...` works.

### 5.3 Test infrastructure worth keeping

- **respx-based HTTP mocking** of every provider. Every adapter has a test file:
  - `test_duckduckgo.py`: 30 tests, including markup-change and ad-only cases;
  - `test_brave.py`, `test_xmlriver_search.py`, `test_octen_search.py`, `test_jina_read.py`: 26 tests covering the escalation ladder and cost guards.
  These can be ported together with their adapters.
- **`conftest.py` fixtures:**
  - `capture_logs` (a loguru sink → list; pytest `caplog` cannot see loguru records);
  - `settings`;
  - `make_config`;
  - `_clear_provider_env` (isolation from the developer's shell);
  - `_mock_duckduckgo_*` (the always-on instance must be mocked).
  The last two become obsolete with YAML config, but the *pattern* (hermetic config per test, always-on providers explicitly mocked) stays.
- **`test_http_policy.py`**: credit-marker and 402/429/5xx policy. Port it as the contract test of the transport layer.
- **`test_url_guard.py`**: the SSRF matrix with DNS patched via the module-level `_resolve_host`.
- **`test_failure_reason.py`**: the classification matrix.
- **`test_rerank.py`**: malformed, partial, duplicate, and empty rankings.
- **`test_pipeline.py`** (43 tests): an executable spec of failover, dedup preference, thin fallback, PDF and TLS paths, over-fetch waves, and billing. Rewrite it against the router, but keep the scenarios.
- **`tests/fixtures/read_cases.yaml`**: 20 real hard URLs (Cloudflare, paywall, PDF-SSL, JS-rendered) as an opt-in live benchmark.
- **CI pattern:** `.gitea/workflows/tests.yml` runs only on PRs, with no secrets. Image publishing is a separate workflow.

Coverage gaps from local measurement:
- `firecrawl.py` 38 %;
- `tavily.py` 78 %;
- `crawl4ai.py` 81 %;
- `serper.py` 80 %;
- `exa.py` 83 %;
- `config_errors.py` 29 %.

There are no tests against real APIs; that is intentional.

---

## 6. Risks and quality notes

**Overall quality: high for its scope.**
- Defensive parsing throughout: `isinstance` checks on every JSON field; bool is excluded from int indices.
- Explicit rationale comments, including measured production facts.
- Consistent error discipline: never raise out of a fan-out; empty and failed are distinguished.
- 93 % coverage and 496 tests.
- The heavy comments (often 2–3× the code) are a maintenance cost but a good onboarding asset. Keep the facts and trim the prose when porting.

**Fragile or risky points**

1. **Stringly-typed failure classification.** `failure_reason._from_text` (`:87-119`) depends on the exact wording of `ProviderError` messages across 20 files. Changing one message shifts categories silently. → Use typed errors (§4.2).
2. **No memory of failure.** An exhausted, 401'ed, or blocked account is called again on every request (the fan-out is unconditional, `pipeline.py:522`). This wastes latency and, for metered APIs, can cost money on vendors that bill failures. → Account state machine and circuit breaker.
3. **Unbounded search latency.** There is no deadline around `gather` (`pipeline.py:522`). One provider hanging for 25 s × 2 attempts stalls every search, and the rerank is an added serial step (capped at 5 s).
4. **The search fan-out cost scales with the number of configured keys.** This is documented (`pipeline_config.py:134-141`), but it is the opposite of what we want with an account pool.
5. **First-wins dedup loses information.**
   - The snippet, title, and provenance of later providers are discarded, and the `source` field names only the first provider (`pipeline.py:543-548`).
   - `_normalize_url` does not strip tracking params, `www.`, or http/https differences, and does not recognise DOI or arXiv URLs. Near-duplicates survive, and true duplicates across schemes are counted twice.
6. **Metadata is dropped in the adapters.** Published date, score, and authors from Tavily, Exa, Octen, Parallel, and You.com are discarded (see §1.13). Rerank scores are discarded (`rerank.py:137`).
7. **SSRF residuals:**
   - (a) DNS rebinding / TOCTOU: the guard's `getaddrinfo` and httpx's own resolution are separate lookups (`_url_guard.py:78-82,118-129`).
   - (b) An unresolvable host is allowed. That is acceptable for the direct client, since the connection then fails, but a proxied, guarded client would resolve the name remotely, where the name *could* point at the proxy's private network.
   - (c) The probe has **no response-size cap** and reads the whole body into memory (`pipeline.py:863-865,882`). A multi-GB URL is a memory DoS.
   - (d) The `verify=False` retry (`:869-885`) is a conscious trade-off, but for a research evidence engine it should be recorded as a provenance flag (`tls_unverified=True`).
8. **Leaky special cases in the pipeline:**
   - `isinstance(provider, TrafilaturaRead)` (`pipeline.py:805,810`);
   - `if inst.type == "jina"` options injection (`:377`);
   - the reranker hard-wired to `JINA_API_KEY` (`:432`);
   - the xmlriver `engine` option unreachable from config (`xmlriver_search.py:60-66`).
   The generic spec, options, and hints in §4 remove all of these.
9. **Overloaded config slots.** `ProviderConfig.token` means a zone, a user id, or a bearer token depending on the type (`base.py:40-59`; `brightdata.py:46-51`; `xmlriver_search.py:131`). → Use typed options models.
10. **Secrets in query strings.** XMLRiver sends `key`/`user` as URL params (`xmlriver_search.py:160-163`). Any logging of request URLs (httpx debug logging, our future request log) would leak them. → Add a redaction layer in the request log.
11. **Process-local state.** Throttle timestamps and billing counters live in memory and reset on restart. A restart right after a DDG block walks straight back into it. → Persist them in `health` and `storage`.
12. **Unverified adapters.** parallel, octen, and linkup were written from docs with no live key. Octen's `highlight` shape is guessed (`octen_search.py:78-94`), and Tavily's credit-exhaustion status is unknown (`pipeline_config.py:76-80`). Verify before relying on quota detection.
13. **Scraping ToS.** `duckduckgo` scrapes the HTML SERP. It is fine for a personal deployment, but flag it in the provider-ToS task (task 5) as "keyless, fragile, non-API".
14. **Russian user-facing strings** are scattered across 5 modules (see §3.1). They must be removed; they are small but easy to miss (`pipeline.py:792,930`, `server.py:204`).
15. **Upstream sync.** The repo shows one squashed commit via shallow clone, and development is active (dates through 2026-09-19). Per the decision there is no upstream sync. Record the source commit `11f297d` in the README credits so that later cherry-picks can diff against it.

**Porting order suggestion**
1. `_http`, `_url_guard`, `pdf`, `failure_reason` (typed), and `ClientManager`.
2. The 20 adapters behind a legacy wrapper.
3. The read executor, with probe, chain, and best-thin.
4. The parallel executor, with deadline and RRF.
5. The rerank protocol.
6. The new tools.

Tests go along with each step.
