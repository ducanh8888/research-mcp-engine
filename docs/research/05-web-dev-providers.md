# 05 — Web & Developer Providers (Exa, Firecrawl, GitHub, Tavily, Perplexity, + Jina/Brave/Serper/Linkup/Parallel)

> Historical investigation, recorded before the specialist/OmniRoute bridge pivot.
> Current authority: [design v4](../design.md), [roadmap](../roadmap.md) and
> [cleanup](../cleanup.md). Recommendations, pricing, protocol and account observations
> below are dated evidence, not current requirements or live validation.

Status: research draft, 2026-09-29. Capabilities in scope: WEB_SEARCH, WEB_READ, NEWS_SEARCH, SITE_MAP,
SITE_CRAWL, SITE_INTERACT, DEVELOPER_SEARCH, REPO_SEARCH, plus the places where web providers touch academic search.

**Evidence tags used below:**

| Tag | Meaning |
|---|---|
| **[src]** | Read from source cloned on 2026-09-29. Paths are under `scratchpad/repos/` unless absolute. |
| **[doc]** | Read from the official docs, OpenAPI spec or PyPI wheel on 2026-09-29. The URL is given. |
| **[U]** | Unverified, or the sources conflict. Check before hard-coding. |

**Repos inspected** (shallow clones, HEAD date where known):

| Repo | Commit / date |
|---|---|
| `exa-labs/exa-mcp-server` | f3d71fb, 2026-09-22 |
| `exa-labs/exa-py` | v2.22.2 |
| `firecrawl/firecrawl-mcp-server` | 4fce275, 2026-09-29 |
| `firecrawl/firecrawl` `apps/python-sdk` | v4.45.0 |
| `tavily-ai/tavily-mcp` | 1c1d54c |
| `spences10/mcp-omnisearch` | ee2b6bd, 2026-09-28 |
| `metatool-ai/metamcp` | ff4ff2d |
| `vvzvlad/research-mcp` | 11f297d |

---

## 0. TL;DR

- **Exa: default semantic WEB_SEARCH.**
  - Use `type:"auto"` with `contents:{highlights:true}`, as the official MCP does. Its `/contents` endpoint (100 QPS) is a strong WEB_READ.
  - Use `deep` / `deep-reasoning` only when the caller asks for thoroughness (4–15 s / 12–40 s).
  - **Breaking changes in 2026:**
    - `/research` retired on 2026-04-01.
    - `/findSimilar` deprecated.
    - `neural` / `keyword` types removed.
    - `livecrawl` replaced by `maxAgeHours`.
    - Category `research paper` replaced by `publication`; `github` / `pdf` categories being deprecated.
    - `resolvedSearchType` removed from responses.
- **Firecrawl: default for WEB_READ, SITE_MAP, SITE_CRAWL and SITE_INTERACT,** and the best DEVELOPER_SEARCH index (`/v2/search/developer`).
  - `scrape` defaults to markdown with main content only. The cache window `maxAge` defaults to **2 days**. Set `maxAge:0` when the caller needs a fresh fetch.
  - **Always pass explicit limits.** The API defaults are crawl `limit` = **10000** pages and map `limit` = 5000.
- **GitHub REST: authoritative for REPO_SEARCH and code search.**
  - Rate limits: search 30/min, code search 10/min, semantic/hybrid issue search 10/min.
  - **New (GA 2026-04-02):** `search_type=semantic|hybrid` on `/search/issues`. It is a strong DEVELOPER_SEARCH source.
  - A 403 or 429 is a rate limit, not an auth failure, when `x-ratelimit-remaining: 0` or `retry-after` is present.
- **Tavily:** second WEB_SEARCH / NEWS_SEARCH provider and a cheap WEB_READ fallback via `/extract`.
  - **Pitfall:** the Python SDK maps 429 → `UsageLimitExceededError` and 432/433 → `ForbiddenError`. Classify by HTTP status, not by exception name.
- **Perplexity: use the Search API only.**
  - `search_type:"fast"` costs $1/1k; the default web search costs $5/1k.
  - **Sonar Chat Completions support ended on 2026-09-27.** Sync calls are being reformulated into the Agent API; async Sonar calls are gone.
  - The Agent API (`/v1/agent`) is an LLM-answer capability and stays optional. It breaks the "no LLM in engine" rule for evidence.
  - Running out of credits returns **401**, not 402.
- **Credit exhaustion is signalled differently by every vendor.** Classify on status **plus** body/tag (§9):

  | Provider | Exhaustion signal |
  |---|---|
  | Exa | 402 with tag |
  | Firecrawl | 402 |
  | Tavily | 432 / 433 |
  | Perplexity | 401 |
  | Serper | 400 "Not enough credits" |
  | Linkup | 429 "run out of credit" |
  | Parallel / Jina | 402 |

- **Multiple accounts / ToS:**
  - GitHub: one free personal account per person, plus one machine account. "You may not share API tokens to exceed GitHub's rate limitations."
  - Brave and Serper explicitly forbid multiple accounts.
  - Exa: the free grant goes only to a user's first team. Exa and Firecrawl forbid resale or proxying to third parties without consent.
  - Rate limits are **team-wide across keys** for Exa and Firecrawl. Several keys in one team give cost attribution and budget caps, **not** more throughput.
  - The account pool should model "N legitimate orgs/plans; keys inside an org share quota."

---

## 1. Capability × provider matrix

Legend: ● primary · ◐ supported, fallback · ○ possible, poor fit · — not supported.

| Capability | Exa | Firecrawl | GitHub | Tavily | Perplexity | Jina | Brave | Serper | Linkup | Parallel |
|---|---|---|---|---|---|---|---|---|---|---|
| WEB_SEARCH | ● `/search` auto | ◐ `/v2/search` | — | ● `/search` | ◐ Search API | ◐ `s.jina.ai` | ● own index | ● Google SERP | ◐ flash/standard | ● `/v1/search` |
| NEWS_SEARCH | ◐ `category:"news"` + published dates | ◐ `sources:["news"]`, `tbs` | — | ● `topic:"news"`, `time_range` | ◐ `search_recency_filter` | — | ● `/news/search` | ● `/news` | ○ date filters | ○ |
| WEB_READ | ● `/contents` (cache + crawl, 100 QPS) | ● `/v2/scrape` | ○ README / contents API | ◐ `/extract` | — | ● `r.jina.ai` (works without a key) | — | ○ | ◐ `/fetch` | ◐ `/v1/extract` |
| SITE_MAP | ○ `subpages` | ● `/v2/map` | — | ◐ `/map` | — | — | — | — | — | — |
| SITE_CRAWL | ○ `subpages` 0–100 | ● `/v2/crawl` (async, webhooks) | — | ◐ `/crawl` (sync) | — | — | — | — | — | — |
| SITE_INTERACT | — | ● `/v2/interact`, `/v2/scrape/{id}/interact`, `actions` | — | — | — | ○ POST with JS / selectors | — | — | — | — |
| DEVELOPER_SEARCH | ◐ `type:"fast"` + highlights (recipe from the old `get_code_context_exa`) | ● `/v2/search/developer` | ● `/search/issues` semantic/hybrid | ○ | ○ | — | ○ | ○ | — | — |
| REPO_SEARCH | ○ (`github` category being deprecated) | ◐ developer `types=[readme]`, `min_stars` | ● `/search/repositories`, `/search/code` | — | — | — | — | — | — | — |
| ACADEMIC touch-points | ◐ `category:"publication"` | ◐ `/v2/search/research/papers` (about 43M abstracts: PubMed/bioRxiv/medRxiv/arXiv) + citation graph | — | — | ○ (`academic` mode gone with Sonar) | — | — | ● `/scholar` | — | — |
| Async agent (optional, LLM) | `/agent/runs` | `/v2/agent` | — | `/research` | `/v1/agent` background mode | DeepSearch | — | — | `/research` | Task API |

---

## 2. Exa

**Sources:**
- Code [src]: `exa-mcp-server/src/tools/*.ts`, `exa-py/exa_py/api.py`.
- Docs [doc]:
  - https://exa.ai/docs/reference/search
  - https://exa.ai/docs/reference/get-contents
  - https://exa.ai/docs/exa-spec.yaml
  - https://exa.ai/docs/changelog.md
  - https://exa.ai/docs/admin/pricing.md
  - https://exa.ai/docs/admin/billing.md
  - https://exa.ai/docs/reference/error-codes

### 2.1 Endpoints and the `exa-py` SDK (v2.22.2)

Auth: `x-api-key` or `Authorization: Bearer`.

| Endpoint | exa-py | Notes |
|---|---|---|
| `POST /search` | `Exa.search(query, *, contents, num_results, type, category, include_domains, exclude_domains, start/end_published_date, user_location, moderation, additional_queries, system_prompt, output_schema, stream, betas)`; `stream_search` | See parameters and defaults below. |
| `POST /contents` | `get_contents(urls, text, highlights, summary, max_age_hours, livecrawl_timeout, subpages, subpage_target, extras)` | 1–100 URLs or ids. Per-URL `statuses[]` carry `source: cached\|crawled` and an error tag. |
| `POST /answer` | `answer`, `stream_answer` | LLM answer, $5/1k. Optional. |
| `POST /findSimilar` | `find_similar*` | **Deprecated** (`Deprecation` header). Don't build on it. |
| `/agent/runs*` | `exa.agent.runs.create / create_and_wait / get / poll_until_finished / stream_agent_events / cancel / stop` | Async. See below. |
| `/research/v1` | `exa.research.*` | **Retired 2026-04-01.** Use `/search type:"deep-reasoning"`. The SDK still ships the module. |
| `/monitors`, `/v0/websets`, `/batches` (beta), `/chat/completions`, `/responses` | — | Out of scope. |

**`/search` parameters and defaults:**
- `type`: `instant | fast | auto` (default) `| deep-lite | deep | deep-reasoning`.
- `numResults`: default 10, max 100 (1000 on Enterprise).
- `category`: `company | publication | news | personal site | financial report | people`.
  - `company` and `people` reject published-date filters and `excludeDomains`.
  - `research paper` became `publication` on 2026-07-23. `pdf`, `github` and `tweet` are being deprecated.
- Domain filters: up to 1200 each. Accepts a host, a host with a path prefix, or `*.sub` wildcards.
- `includeText` / `excludeText` are deprecated. `start/endCrawlDate` are silently ignored.
- `outputSchema` (deep types): at most 10 properties, depth 2, adds about 2 s. Returns `output.content` and `output.grounding`.
- Omitting `contents` still returns text capped at 10,000 characters (`api.py` docstring). **Always set `contents` explicitly.**

**`contents` options:**
- `text`: `true` or `{maxCharacters ≤1M, includeHtmlTags, verbosity: compact|standard|full}`.
- `highlights`: `true` gives dynamic sizing, or `{query, maxCharacters}`. `numSentences` and `highlightsPerUrl` are deprecated.
- `summary`: `{query, schema}`.
- `maxAgeHours`: range −1…720. `0` always crawls, `−1` serves cache only. This **replaces `livecrawl`**.
- `livecrawlTimeout`: default 10000 ms, max 90000.
- `subpages`: 0–100, with `subpageTarget`.
- `extras`: `{links, imageLinks, codeBlocks}`.
- `snapshotAsOf`: returns a historical version.

**Agent runs:**
- `effort`: minimal | low | medium | high | xhigh | auto | ultra.
- Also `outputSchema`, `input.data` / `exclusion`, `dataSources`, and `budget.{maxCostDollars, maxDurationSeconds}`.
- The MCP polls every 4 s, sends a 15 s heartbeat and caps each call at an 800 s window (`agentRun.ts`).

### 2.2 Defaults in the official MCP [src]

- **Tool set.** Only `web_search_exa` and `web_fetch_exa` are enabled by default. Advanced search, code, company, people, deep search, deep research and agent tools are opt-in, and most are marked deprecated (`toolRegistry.ts`).
- **`web_search_exa`:** `{type: "auto", numResults: 10, contents: {highlights: true}}`. It parses a `category:<x>` prefix out of the query and returns Title / URL / Published / Author / Highlights.
- **`web_search_advanced_exa`:**
  - Uses `contents.text` (true or `{maxCharacters}`), plus `maxAgeHours` when given.
  - Otherwise it sends `livecrawl:"fallback"`, which is legacy. Our engine should omit both (cache with crawl fallback is the server default).
  - Also exposes `summary`, `highlights{maxCharacters, query}`, `subpages`, `additionalQueries` and `context`.
- **`web_fetch_exa`:** calls `/contents` with `text.maxCharacters` default **3000** and batches URLs.
- **Code-context recipe** (`exaCode.ts`): `type:"fast"`, `highlights:{query}`, `text:{maxCharacters:300}`.
- **Timeouts:** 60 s for search/fetch, 300 s for advanced search.
- **Retries:** only on 500/502/503/504, at most 2, with delay 1 s·2ⁿ (`utils/errorHandler.ts`).

### 2.3 Engine defaults (proposal)

- `WEB_SEARCH`: `type="auto"`, `num_results=10`, `contents={"highlights": True}`.
- `WEB_SEARCH` with `depth=deep`: `type="deep"`, `additional_queries` optional.
- `WEB_READ`: `get_contents(urls, text={"max_characters": 20000})`.
  - Add `max_age_hours=0` when the caller sets `fresh=true`.
  - Map the per-URL tags `CRAWL_NOT_FOUND | CRAWL_HTTP_{n} | CRAWL_TIMEOUT | CRAWL_LIVECRAWL_TIMEOUT | SOURCE_NOT_AVAILABLE | UNSUPPORTED_URL` to URL-level failures. These are not account failures.
- Parameters to expose to consumers: `num_results`, `include/exclude_domains`, `start/end_published_date`, `category`, `depth ∈ {normal, deep}`.

### 2.4 Output fields

- **Per result:** `id`, `url`, `title`, `publishedDate`, `author`, `text`, `highlights[]`, `highlightScores[]`, `summary`, `subpages[]`, `image`, `favicon`, `extras`.
- **Top level:** `requestId`, `searchTime` (ms), `costDollars{total, breakdown}`.
- **Response headers:** `x-request-id`, `x-exa-queued`, `x-exa-queue-ms`. There are no rate-limit or credit headers.
- **Cost tracking:** store `costDollars.total` per call. Team usage (`total_cost_usd`, 180-day history) needs a service key that support enables.

### 2.5 Pricing, limits, latency, errors, ToS [doc]

**Pricing** (pay-as-you-go, prepaid):

| Operation | Price |
|---|---|
| `instant` search | $4/1k |
| `fast` / `auto` search (text and highlights for 10 results included) | $7/1k |
| `deep-lite` / `deep` | $12/1k |
| `deep-reasoning` | $15/1k |
| Each result beyond 10 | +$1/1k |
| Summary | +$1/1k pages |
| `/contents` | $1/1k pages per content type |
| `/answer` | $5/1k |
| Agent | $0.012 (minimal) to $1.00 (xhigh) per run |

- Free tier: $10 of credit, reset monthly, **first team only**, plus a one-time $10 bonus.

**Rate limits** (team-wide across all keys; per-key caps are possible):

| Endpoint | Default limit |
|---|---|
| `/search`, `/answer` | 10 QPS |
| Deep types | 5 QPS |
| `/contents` | 100 QPS |
| `/agent/runs` | 5 QPS and 50 active runs |

- Pay-as-you-go rises to 25 QPS for 90 days after buying $1k or more.

**Latency:**

| Search type | Latency |
|---|---|
| `instant` | <150 ms (openbenchmarks p50 386 ms) |
| `fast` | p50 <425 ms (openbenchmarks 569 ms) |
| `auto` | ~1 s [U] |
| `deep-lite` | ~4 s |
| `deep` | 4–15 s |
| `deep-reasoning` | 12–40 s |

**Errors** (body `{requestId, error, tag}`):

| Status | Tags / meaning | Engine classification |
|---|---|---|
| 400 | `INVALID_NUM_RESULTS`, `INVALID_JSON_SCHEMA`, … | bad request |
| 401 | `INVALID_API_KEY` | auth |
| **402** | **`NO_MORE_CREDITS`** | EXHAUSTED |
| **402** | **`API_KEY_BUDGET_EXCEEDED`** | EXHAUSTED for this key only |
| **402** | **`TEAM_BUDGET_EXCEEDED`** | EXHAUSTED |
| 403 | `FEATURE_DISABLED` | PLAN_BLOCKED |
| 403 | `PROHIBITED_CONTENT` | query-level |
| **429** | `RATE_LIMIT_EXCEEDED`, `CONCURRENCY_LIMIT_REACHED` | RATE_LIMITED |
| 503 | `SERVICE_OVERLOADED` | transient |
| 504 | — | transient |

- Honour `Retry-After` when it is present; otherwise use exponential backoff.

**ToS** (https://exa.ai/terms):
- §4.2(e): no resale or sublicensing without consent.
- §4.2(a): no redistributing or selling Output.
- §4.2(f): no competing product.
- §4.2(i): no circumvention.
- Multiple keys per team are supported but share the team's limits. There is no explicit multi-account clause.

---

## 3. Firecrawl

**Sources:**
- Code [src]: `firecrawl-mcp-server/src/index.ts`, `developer.ts`, `research.ts`, `usage.ts`; `fc/apps/python-sdk/firecrawl/v2/{client.py, types.py, utils/error_handler.py}`.
- Docs [doc]:
  - https://docs.firecrawl.dev/api-reference/v2-openapi.json
  - https://docs.firecrawl.dev/billing.md
  - https://docs.firecrawl.dev/rate-limits.md
  - https://docs.firecrawl.dev/api-reference/errors.md
  - https://www.firecrawl.dev/pricing

### 3.1 Endpoints and the `firecrawl-py` SDK (v4.45.0)

Construct with `Firecrawl(api_key, api_url, timeout, max_retries=3, backoff_factor=0.5)`. `AsyncFirecrawl` is the async client.

| Capability | Endpoint | SDK | Mode |
|---|---|---|---|
| WEB_READ | `POST /v2/scrape` | `scrape(url, formats, only_main_content, max_age, min_age, wait_for, timeout, actions, proxy, parsers, location, mobile, block_ads, store_in_cache, …)` | sync |
| WEB_READ (batch) | `POST /v2/batch/scrape` | `batch_scrape`, `start_batch_scrape`, `get_batch_scrape_status`, `get_batch_scrape_errors`, `cancel_batch_scrape` | async job |
| WEB_SEARCH / NEWS | `POST /v2/search` | `search(query, sources, categories, include/exclude_domains, limit, tbs, location, country, highlights, scrape_options, timeout)` | sync |
| DEVELOPER_SEARCH | `/v2/search/developer` (the MCP uses GET; the docs show POST) | `developer_search(query, k≤100, passages 1–5, types=[doc,issue,pull_request,readme], repos, sources, language, topic, license, min_stars, max_stars, archived, fork, skills)` | sync |
| ACADEMIC | `GET /v2/search/research/papers`, `/papers/{id}`, `/papers/{id}/similar` | `search_papers`, `inspect_paper`, `read_paper(id, query)`, `related_papers(id, intent, mode)` | sync |
| SITE_MAP | `POST /v2/map` | `map(url, search, sitemap∈{include,skip,only}, include_subdomains, limit, ignore_query_parameters, ignore_cache)` | sync |
| SITE_CRAWL | `POST /v2/crawl`, `GET /v2/crawl/{id}` (+ `/errors`, `/active`, `/params-preview`, cancel) | `start_crawl`, `get_crawl_status(_page)`, `get_crawl_errors`, `cancel_crawl`, `crawl` (blocking), `crawl_params_preview(url, prompt)` | **async job**, signed webhooks |
| Extraction | `/v2/extract` | `extract`, `start_extract`, `get_extract_status` | async; superseded by agent |
| SITE_INTERACT | `POST /v2/interact`, `/v2/interact/{sessionId}/execute`, `/v2/scrape/{jobId}/interact` (DELETE stops) | `interact`, `stop_interaction`, `browser`, `browser_execute`, `list_browsers`, `delete_browser` | stateful session (`ttl` 30–3600 s, default 300) |
| Agent | `/v2/agent` | `agent`, `start_agent`, `get_agent_status`, `cancel_agent`, `get_agent_trace` | async |
| Usage | `/v2/team/credit-usage[/historical]`, `/team/token-usage`, `/team/queue-status` | `get_credit_usage`, `get_queue_status`, `get_concurrency` | sync |

### 3.2 Parameters and defaults

**Scrape:**
- `formats`: default `["markdown"]`. Other values: summary, html, rawHtml, links, images, screenshot, json (`{prompt, schema}`), query/question, changeTracking, branding, audio, highlights.
- `onlyMainContent`: default true.
- **`maxAge`: default 172800000 ms (2 days).** `minAge` makes the call cache-only.
- `timeout`: default 60 s, max 300 s.
- `parsers`: default `["pdf"]`.
- `proxy`: `basic | enhanced | auto` (default). The MCP also lists `stealth`.
- `actions`: wait, click, write, press, scroll, screenshot, scrape, executeJavascript, pdf.
- The official MCP says to set `maxAge:0` for a live fetch.
- Fetches made through `search.scrapeOptions` **ignore `maxAge`** (`index.ts:2794`).

**Search:**
- `limit`: 1–100 per source.
- `sources`: web, news, images, alexandria.
- `categories`: developer, research, pdf. There is **no `github` category**.
- `tbs`: `qdr:h|d|w|m|y` or `cdr:1,cd_min:…,cd_max:…`.
- `highlights`: default true. `description` then holds a query-relevant excerpt.
- `includeDomains` and `excludeDomains` are mutually exclusive.
- `country`: default US.
- Operators work in the query: `"…"`, `-term`, `site:`, `inurl:`, `intitle:`, `related:`.
- When authenticated, the MCP defaults `sources` to `['web','alexandria']`. **Our engine should send `sources:["web"]`.**
- The MCP posts `/v2/search` raw because `client.search()` strips `id` and `creditsUsed` (`index.ts:2845`). Do the same.

**Map:**
- `limit`: default 5000, max 100000.
- `sitemap`: include (default) / skip / only.
- `search` ranks URLs by relevance.
- The sitemap cache lasts up to 7 days; `ignoreCache` bypasses it.

**Crawl:**
- Options: `limit` (**default 10000**), `maxDiscoveryDepth`, `includePaths` / `excludePaths` (regex), `regexOnFullURL`, `sitemap`, `crawlEntireDomain`, `allowExternalLinks`, `allowSubdomains`, `delay`, `maxConcurrency`, `deduplicateSimilarURLs`, `ignoreQueryParameters`, `prompt` (natural language to params), `scrapeOptions`, `webhook` (HMAC `X-Firecrawl-Signature`).
- Results are paginated in 10 MB chunks via `next` and kept for 24 h.
- The MCP polls every 2 s until done (`index.ts:3584`).
- Our engine should return a `job_id`, run a background poller and page through results.

**Agent:**
- Options: `prompt`, `urls`, `schema`, `model` (`spark-2`), `effort` low/medium/high, `strictConstrainToURLs`, `threadId`.
- **`maxCredits` defaults to 2500.** Hitting it gives `status:"failed"` with no charge and no HTTP error.
- 5 free runs per day.

**Interact:**
- Takes a natural-language `prompt` or `code` (bash/python/node), with a timeout of 1–300 s.
- It can have side effects, so the MCP drops write actions and webhooks in `SAFE_MODE` (`index.ts:1972, 2037`).

**Developer search:**
- `k` defaults to 10.
- Hit ids look like `issue:owner/repo#123` or `doc:<hash>` and come with markdown `passages[]` (`developer.ts`).
- The `/v2/developer/search` mount "may be withdrawn".

**Research:**
- `search_papers` `k` defaults to **40** (max 500), with `authors`, `categories` (e.g. `cs.LG`) and `from`/`to` filters.
- `related_papers` modes: similar, citers, references, with `rerank`.
- `read_paper` returns 4 passages by default.
- Accepts ids such as `arxiv:`, `pmid:`, `doi:`.
- Experimental, and the credit cost is undocumented [U].

**Engine defaults (proposal):**
- `WEB_READ`: `scrape(url, formats=["markdown"], only_main_content=True, proxy="auto", timeout=45000)`, using the default `max_age`. Set `max_age=0` when the caller sets `fresh=true`. (research-mcp already sends `{"formats":["markdown"],"proxy":"auto"}`, in `src/providers/firecrawl.py`.)
- `SITE_MAP`: `map(url, limit=500)`.
- `SITE_CRAWL`: `start_crawl(url, limit=25, max_discovery_depth=2, deduplicate_similar_urls=True, ignore_query_parameters=True, scrape_options={formats:["markdown"], onlyMainContent:true})`. Enforce a policy maximum of 100 pages.
- `DEVELOPER_SEARCH`: `developer_search(q, k=10, passages=3)`, passing through `types`, `repos`, `language` and `min_stars`.
- `AGENT` (if enabled): always pass `max_credits`, e.g. 200.

### 3.3 Pricing and limits [doc]

**Credit costs:**
- Scrape and crawl: 1 credit per page. A cached result still costs 1.
- Add-ons:
  - PDF: +1 per PDF page.
  - json, query, highlights, audio, video, redactPII: +4 each.
  - ZDR: +1.
- Map: 1 per call.
- Search: 2 per 10 results, plus scrape costs if scraping.
- Interact: 2 per browser-minute (7 with a prompt).
- Monitor: 7 per page-check.
- Zero cost when no document is returned. A target-site 403/404 still returns a document and costs 1.

**Plans** (annual price per month):

| Plan | Price | Credits/month | Concurrent browsers | Pay-as-you-go |
|---|---|---|---|---|
| Free | $0 | 1k | 2 | — |
| Hobby | $16 | 5k | 5 | $5/1k credits |
| Standard | $83 | 100k | 25 | $2.50/1k |
| Growth | $333 | 500k | 50 | $2/1k |
| Scale | $599 | 1M | 100 | $1/1k |

- A keyless tier (per-IP daily cap) covers scrape, search, interact and parse.

**Rate limits** (requests per minute, team-wide across keys):

| Plan | scrape / map / search | crawl / agent / extract | interact |
|---|---|---|---|
| Free | 10 | 2 | 2 |
| Hobby | 100 | 20 | 20 |
| Standard | 500 | 100 | 100 |
| Growth | 5000 | 1000 | 1000 |
| Scale | 10000 | 2000 | 1500 |

- Status polling has a much higher limit (500 to 500k).
- Batch scrape: the docs conflict on whether it shares the crawl limit or the scrape limit [U].
- Queue: at most 50k jobs (more on higher plans). Jobs expire after 48 h, and queue time counts against `timeout`.

**Latency:**
- Search: avg 510 ms, p50 471 ms (openbenchmarks, 2026-09-12).
- Scrape: p50 about 2.3 s live [U, competitor benchmark]. Cache hits take milliseconds.
- Agent: minutes.

### 3.4 Errors and quota [doc + src]

Body shape: `{"success":false, "error", "details"?}`.

| Status | Meaning | SDK exception | Engine classification |
|---|---|---|---|
| 400 | Bad request | `BadRequestError` | bad request |
| 401 | `Unauthorized: Invalid token` | `UnauthorizedError` | auth |
| **402** | **`Payment Required: Insufficient credits`** | `PaymentRequiredError` | EXHAUSTED |
| 403 | `PROVIDER_TERMS_REQUIRED` (Alexandria) | `ProviderTermsRequiredError` | PLAN_BLOCKED |
| 403 | `SCRAPE_PROMPT_INJECTION_DETECTED` or "website not supported" | `WebsiteNotSupportedError` | URL-level |
| 408 | Timeout | — | retryable |
| **429** | `Rate limit exceeded` / `Concurrency limit reached` + `Retry-After` | `RateLimitError` | RATE_LIMITED |
| 5xx | Server error | — | retryable; the SDK retries 502 |

- 402 fires at zero balance, when pay-as-you-go is off or capped, or when a per-key spend limit is hit.
- Retryable set: {408, 429, 500, 502, 503, 504}.
- Keyless 429 responses carry `reason: requests|credits` and `retry_after_seconds` (MCP `index.ts:3016–3120`).
- **Meters:** `creditsUsed` in each response, plus `GET /v2/team/credit-usage` → `{remainingCredits, planCredits, billingPeriodStart, billingPeriodEnd}` (MCP `usage.ts`). There are no credit headers.

**ToS** (https://www.firecrawl.dev/terms-of-service, 2024-11-05):
- No commercial use except as authorized; no selling, distributing or creating derivative works; no sharing credentials.
- Multiple keys per team are supported, with per-key spend limits and shared rate limits.
- A personal, single-user gateway is fine. Exposing it to third parties is not.

---

## 4. GitHub (REPO_SEARCH, DEVELOPER_SEARCH)

**Sources:**
- Code [src]: `mcp-omnisearch/src/providers/search/github/index.ts`.
- Docs [doc]:
  - https://docs.github.com/en/rest/search/search
  - https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
  - https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
  - https://github.blog/changelog/2026-04-02-improved-search-for-github-issues-is-now-generally-available/
  - https://github.com/github/github-mcp-server

### 4.1 Endpoints

| Endpoint | Use | Notes |
|---|---|---|
| `GET /search/repositories` | REPO_SEARCH | Qualifiers: `stars:>100 language:python topic:mcp pushed:>2026-01-01 archived:false fork:false in:name,description,readme`. `sort=stars\|forks\|help-wanted-issues\|updated`. |
| `GET /search/code` | code | **Requires auth** and at least one search term. **Legacy syntax only**: no regex, no new `path:` (use `filename:` / `extension:`). Default branch only; files under 384 KB. `sort=indexed`. |
| `GET /search/issues` | issues + PRs | **`search_type=lexical` (default) `\|semantic\|hybrid`** (GA 2026-04-02). The response reports the type used and `lexicalFallbackReason`. `advanced_search=true` enables AND/OR nesting. App user tokens must include `is:issue` / `is:pull-request` or get a 422. |
| `GET /search/commits \| users \| topics \| labels` | misc | — |
| GraphQL `search(query, type, first≤100)` | repos / issues / discussions | `SearchType`: ISSUE, ISSUE_ADVANCED, **ISSUE_SEMANTIC, ISSUE_HYBRID**, REPOSITORY, USER, DISCUSSION. No code or commit search. |
| `GET /repos/{o}/{r}/readme`, `/contents/{path}` | WEB_READ for repos | Use `Accept: application/vnd.github.raw+json`. |

**Common behaviour:**
- `per_page` max 100. At most **1000 results** per query.
- Query limit: 256 characters and 5 AND/OR/NOT operators.
- `incomplete_results: true` means the search timed out.
- `Accept: application/vnd.github.text-match+json` adds `text_matches[].fragment`, which is the snippet (omnisearch uses it).
- Pin `X-GitHub-Api-Version: 2022-11-28`. Version `2026-03-10` introduces breaking changes.

**Output fields:**
- Repos: `full_name, html_url, description, stargazers_count, forks_count, pushed_at, language, topics, license, archived`.
- Code: `name, path, html_url, repository.full_name, text_matches`.
- Issues: `title, number, state, html_url, user, labels, created_at, closed_at, pull_request.merged_at, body`.

**GitHub MCP server** (`https://api.githubcopilot.com/mcp/`):
- Search tools: `search_code`, `search_repositories` (`minimal_output`), `search_issues`, `search_pull_requests`, `search_users`, `search_orgs`, each taking `query/sort/order/page/perPage`.
- No `search_type` parameter is visible [U].
- It is a thin wrapper, so call REST directly (`httpx` or `githubkit`).

### 4.2 Limits, errors, ToS [doc]

**Limits:**

| Scope | Limit |
|---|---|
| Search, authenticated | **30/min** |
| Code search | **10/min** |
| Semantic/hybrid issue search | **10/min** |
| Unauthenticated search | 10/min |
| Core, PAT | 5000/h |
| Core, unauthenticated | 60/h |
| Enterprise-Cloud org apps | 15000/h |
| GraphQL | 5000 points/h; secondary 2000 points/min |
| Secondary | 100 concurrent requests; 900 points/min REST |

**Headers:** `x-ratelimit-{limit,remaining,used,reset,resource}`. `resource` is one of `search`, `code_search`, `core`, `graphql`. Keep one bucket per (account, resource).

**Classification:**

| Response | Classification | Action |
|---|---|---|
| 403 or 429 with `x-ratelimit-remaining:0` | primary rate limit | wait until `reset` |
| 403 or 429 with `retry-after` or a "secondary rate limit" message | secondary rate limit | honour `retry-after`, else wait ≥60 s with exponential backoff |
| **GraphQL: HTTP 200** with an `errors` body and `remaining:0` | primary rate limit | wait until `reset` |
| 401 | auth | — |
| 403 without rate-limit markers | permission / SSO | — |
| 422 | invalid query | e.g. unauthenticated code search, or missing `is:issue` |

**Latency and cost:** about 0.3–1.5 s [U]; code and semantic search are slower. Free with a PAT. A fine-grained PAT with no scopes is enough for public search.

**ToS:**
- ToS §B.3: "One person or legal entity may maintain no more than one free Account (…a machine account…can only be used for running a machine)."
- ToS §H: "You may not share API tokens to exceed GitHub's rate limitations."
- Acceptable Use Policies §4 prohibits excessive automated bulk activity.
- **Conclusion:** use one personal PAT, optionally one machine-account PAT, and/or one GitHub App installation (its own bucket, legitimate). No token farming.

---

## 5. Tavily

**Sources:**
- Code [src]: `tavily-mcp/src/index.ts`; research-mcp `src/providers/tavily_search.py`, `tavily.py`.
- Docs [doc]:
  - https://docs.tavily.com/documentation/api-reference/endpoint/search
  - https://docs.tavily.com/documentation/api-credits
  - https://docs.tavily.com/documentation/rate-limits
  - SDK: https://github.com/tavily-ai/tavily-python (0.8.4)

### 5.1 Endpoints

**SDK:** `TavilyClient` / `AsyncTavilyClient` with `search`, `extract`, `crawl`, `map`, `research`, `get_research`. `TavilyHybridClient` mixes in a local DB.
- **Keyless mode:** if no key is set, the SDK sends `X-Tavily-Access-Mode: keyless`. Only search and extract work keyless; limit errors return `{error:{code, retry_after_seconds, next_actions}}`. The keyless quota is undocumented [U].

**`POST /search`:**
- `search_depth`:

  | Value | Credits | Notes |
  |---|---|---|
  | `basic` (default) | 1 | reranked chunks |
  | `fast` | 1 | reranked chunks |
  | `ultra-fast` | 1 | returns page summaries |
  | `advanced` | 2 | best relevance, slowest |

- `chunks_per_source`: 1–3.
- `max_results`: 0–20. The API default is 10; the MCP and best-practices page use 5 [U].
- `topic`: general / news / finance.
- Dates: `time_range` (day/week/month/year) or `start_date` / `end_date`.
- `include_answer`: false / basic / advanced (LLM, avoid).
- `include_raw_content`: false / markdown / text.
- Domains: `include_domains` (≤300), `exclude_domains` (≤150), `include_domains_mode` restrict / prefer.
- Also `country`, `language`, `exact_match`, `auto_parameters` (can cost 2), `safe_search`, `include_usage`.
- Queries should be under 1500 characters.
- **Response:** `{query, answer, images, results:[{title, url, content, score, raw_content, published_date, favicon}], response_time, usage, request_id}`.

**`POST /extract`:**
- `urls` (≤20), `extract_depth` basic / advanced, `format` markdown / text.
- `query` + `chunks_per_source` (1–5) give query-focused chunks.
- `timeout` 1–60 s.
- Response: `results[{url, raw_content}]` and `failed_results[{url, error}]`. Failed URLs are free.

**`/map` and `/crawl`:**
- `max_depth` (default 1), `max_breadth` (default 20), `limit` (default 50).
- `instructions` doubles the map cost. Also `select_paths` / `exclude_paths`, `allow_external`.
- Sync but slow.

**`POST /research` (async):**
- Returns 201 `{request_id, status: pending}`. `GET /research/{id}` returns 202 until the task is completed or failed.
- `model` mini / pro / auto; `output_schema`; `citation_format`; `output_length`.
- Limited to 20 RPM. The MCP polls with exponential backoff (`index.ts:803`).
- Cost: pro 15–250 credits, mini 4–110.

**Engine defaults:**
- WEB_SEARCH: `search_depth:"basic"`, `max_results:10`, no answer, no raw content.
- NEWS: `topic:"news"`, `time_range:"week"`.
- WEB_READ fallback: `extract(depth="basic")`, escalating to `advanced` when the result is empty. research-mcp uses advanced + markdown.
- The MCP supports `DEFAULT_PARAMETERS` env JSON merged into every call (`index.ts:123`).

### 5.2 Pricing, limits, errors, ToS [doc]

**Credits:**

| Operation | Credits |
|---|---|
| Search | basic 1, advanced 2 |
| Extract | 1 per 5 URLs (basic), 2 per 5 URLs (advanced) |
| Map | 1 per 10 pages (2 with instructions) |
| Crawl | map + extract |

**Plans** (credits reset on the 1st of each month):

| Plan | Price | Credits/month |
|---|---|---|
| Free | $0 | 1k |
| Project | $30 | 4k |
| Bootstrap | $100 | 15k |
| Startup | $220 | 38k |
| Growth | $500 | 100k |
| Pay-as-you-go | $0.008/credit | — |

**Rate limits:**

| Scope | Limit |
|---|---|
| Development key | 100 RPM |
| Production key (paid) | 1000 RPM |
| Crawl | 100 RPM |
| Research | 20 RPM |
| Usage endpoint | 10 per 10 minutes |

**Errors:**

| Status | Meaning | Engine classification |
|---|---|---|
| **429** + `Retry-After` | rate limited | RATE_LIMITED |
| **432** | plan / key limit | EXHAUSTED |
| **433** | pay-as-you-go cap | EXHAUSTED |
| 401 | invalid key | auth |
| 400 / 422 | bad request | bad request |
| 5xx | server error | transient |

- The error message lives in `detail.error`.
- **SDK trap:** 429 → `UsageLimitExceededError` and 403/432/433 → `ForbiddenError`.
- research-mcp's `_http.py` only knows 402/429. **Add 432/433.**

**Latency:** ultra-fast is "near-instant" and advanced is slowest. The docs give no numbers [U].

**ToS** (https://www.tavily.com/terms):
- §4.1: more than one account per customer may require an Order Form.
- §2: no sharing keys with third parties.
- §3.2(ix): no exceeding limits.
- Multiple keys per account are allowed through the dashboard.

---

## 6. Perplexity

**Docs [doc]:**
- https://docs.perplexity.ai/api-reference/search-post
- https://docs.perplexity.ai/docs/search/fast-search.md
- https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview
- https://docs.perplexity.ai/docs/admin/rate-limits-usage-tiers
- https://docs.perplexity.ai/docs/resources/faq.md

**Search API (`POST https://api.perplexity.ai/search`) — the part we use:**
- `query`: a string, or up to 5 queries. Multiple queries are billed as one request but use N rate units.
- `max_results`: 1–20, default 10.
- `search_type`: `web` (default, $5/1k) `| fast` ($1/1k, about 160 ms median [U]) `| people`.
- Page budget: `search_context_size` (default high), or `max_tokens` / `max_tokens_per_page`. The two styles are mutually exclusive.
- Filters:
  - `country`
  - `search_domain_filter` (≤20; a `-` prefix excludes)
  - `search_language_filter`
  - `search_recency_filter` hour / day / week / month / year
  - `search_after/before_date_filter` and `last_updated_*` (format MM/DD/YYYY)
- **Response:** `results[{title, url, snippet, date, last_updated}]`, `id`, `server_time`. No token charges.
- Python SDK 0.43.x needs `extra_body={"search_type":"fast"}`.

**Sonar Chat Completions:**
- **Support ended 2026-09-27.** Sync and streaming calls are being reformulated as Agent API calls; **async Sonar is no longer supported**.
- Old model → Agent API preset mapping: sonar / sonar-pro → `fast`, sonar-reasoning-pro → `low`, sonar-deep-research → `high`.
- `search_mode:"academic"` has **no equivalent** in the Agent API, and neither does `return_related_questions`. Do not build on Sonar.

**Agent API (`POST /v1/agent`), optional ANSWER capability only:**
- Choose `preset` (fast / low / medium / high / xhigh) or `model`. Other fields: `tools:[{type:"web_search", filters:{…}, max_results 1–50}]`, `instructions`, `max_steps`, `response_format`.
- Async runs use `background:true` and are polled via `GET /v1/agent/{id}`. Status is one of queued, in_progress, completed, failed, cancelled, incomplete.
- Output `output[]` includes `search_results{url, title, snippet, date}`. Harvest these as evidence tagged `provenance: llm-selected`.
- Pricing: model tokens at pass-through rates, plus web_search $0.0025/call and fetch_url $0.0005/call.

**Limits:**
- Usage tier is set by lifetime spend and never goes down.

  | Tier | Lifetime spend | Agent API limit |
  |---|---|---|
  | 0 | $0 | 1 QPS |
  | 1 | $50 | 3 QPS |
  | 2 | $250 | 8 QPS |
  | 3 | $500 | 17 QPS |
  | 4 | $1000 | 33 QPS |

- Search API: 50 query units/s (burst 50) at every tier.
- 429 includes `Retry-After`, and **rejected 429s are not billed**.

**Errors:**
- **401 means an invalid key *or an account out of credits*.** No 402 exists.
- Tell the two 401 cases apart by probing another key from the same org, or by checking the dashboard balance. Default to `EXHAUSTED_OR_AUTH`, cool down 1 h and alert.

**ToS:** the API terms page returned 403 to our fetcher [U]. No explicit multi-account clause was found.

---

## 7. Already in research-mcp (brief)

**Sources:**
- Provider docstrings in `research-mcp/src/providers/*.py`, written by the author between 2026-06 and 2026-09.
- Web check on 2026-09-29:
  - https://jina.ai/reader/
  - https://api-dashboard.search.brave.com/documentation/guides/rate-limiting
  - https://api-dashboard.search.brave.com/documentation/resources/terms-of-service
  - https://serper.dev/terms
  - https://docs.linkup.so/pages/documentation/platform/errors.md
  - https://docs.parallel.ai/getting-started/pricing
  - https://docs.parallel.ai/resources/warnings-and-errors.md

| Provider | Endpoint / key params | Price | Limits | Exhaustion signal | Multi-account ToS |
|---|---|---|---|---|---|
| **Jina Reader** (Elastic acquired Jina Oct 2025; API unchanged) | `GET r.jina.ai/{url}`, works keyless. Headers `X-Return-Format: markdown`, `X-Engine: browser`, `X-Respond-With: readerlm-v2` (3× cost), `x-proxy: auto` (5×), `jina-ocr-v1` (40×), `X-Token-Budget`, `X-Timeout`, `X-No-Cache`, `X-Target-Selector`, `X-Wait-For-Selector` | Billed on output tokens; 10M free per key; about $0.05/1M | keyless 20 RPM; free/paid 500; premium 5000 | 402 (spurious 402s reported: reader #1136/#1192, so retry once); 429 | none found |
| **Jina Search** | `POST s.jina.ai` (**key required**). `X-Respond-With: no-content` returns the SERP only | 10k tokens/request | 100 RPM (premium 1000) | 402/429 | none found |
| **Brave** | `GET /res/v1/web/search`, `/news/search`, `/llm/context`. `q` ≤400 chars; `count` ≤20; `offset` ≤9; `freshness` pd/pw/pm/py/range; `extra_snippets`; `country`; `search_lang` | $5/1k; $5 free credit/month (about 1000 requests; card required) | 50 QPS paid. Free plan headers observed: `1;w=1, 2000;w=2678400` | 429 + `X-RateLimit-Remaining/Reset` (per-second and per-month pairs); 402 billing; 422 invalid token | **Forbids multiple accounts to bypass limits**; no storage beyond transient; no AI training |
| **Serper** | `POST google.serper.dev/{search, news, scholar, images, patents, …}`. `q`, `gl`, `hl`, `num`, `page`, `tbs` | $1.00→$0.30/1k prepaid (valid 6 months); 2500 free | 50–300 QPS by pack | **HTTP 400 `{"message":"Not enough credits"}`** (verified live by the research-mcp author) | **"Register more than one account" forbidden** |
| **Linkup** | `POST /v1/search`: `q`, `depth` flash/fast/standard/deep, `outputType` searchResults/sourcedAnswer/structured, domains (≤100), `fromDate/toDate`, `maxResults`. Also `/fetch`, `GET /credits/balance` | search $0.005 (deep $0.05); fetch $0.001–0.01; $20/month free top-up | 10 QPS/org | **429 for both** "run out of credit" and "too many requests" (match the message) | none found |
| **Parallel** | `POST /v1/search`: `search_queries[]` (required), `objective`, `mode` turbo/fast/basic/advanced (default advanced, about 3 s), `advanced_settings.max_results` (10). Also `/v1/extract` (≤20 URLs), Task API | turbo/fast $1/1k; basic/advanced $5/1k; extract $1/1k URLs | 600/min for search and extract | **402 = insufficient *available* balance** (in-flight tasks reserve credit); 429; 408 means use async. Body `{type:"error", error:{ref_id, message}}` | none found |

---

## 8. Recommended routing and fallback chains

**Principles:**
- Quality matters more than latency; a 10–30 s budget is fine.
- Search capabilities fan out in parallel and fuse results with RRF.
- Read capabilities fail over sequentially.
- The account router picks the account within a provider. The chain picks the provider.
- A chain step is skipped when the provider's breaker is open or its quota is marked exhausted.

| Capability | Mode | Parallel fan-out set | Sequential fallback when results < k after dedup | Notes |
|---|---|---|---|---|
| WEB_SEARCH | fan-out | Exa (auto, highlights) + Brave + Tavily (basic) + Parallel (fast) | Serper → Perplexity Search (fast) → Firecrawl search → Jina search → Linkup (flash) | Semantic (Exa) plus independent keyword indexes (Brave, Serper) give diversity. Tavily and Parallel return snippets ready for an LLM. |
| WEB_SEARCH, deep | fan-out | Exa (deep) + Parallel (advanced) + Tavily (advanced) + Linkup (standard) | Brave, Serper | Only when the consumer sets `depth=deep`. |
| NEWS_SEARCH | fan-out | Brave news + Tavily (topic=news, time_range) + Serper news | Exa (category=news, startPublishedDate) → Firecrawl (sources=news, tbs) → Perplexity Search (recency) | Normalise publish date into a freshness signal. |
| WEB_READ | sequential | — | cache → Firecrawl scrape (markdown, default maxAge) → Exa contents → Jina Reader (works keyless) → Tavily extract (basic→advanced) → Parallel extract → local trafilatura/crawl4ai | Keep research-mcp's URL guard, PDF path and escalation when content is thin. A URL-level error (Exa tag, Firecrawl 403 "not supported") moves to the next provider **without** penalising the account. |
| WEB_READ, fresh | sequential | — | Firecrawl (maxAge=0) → Exa (max_age_hours=0) → Jina (`X-No-Cache`) | — |
| SITE_MAP | sequential | — | Firecrawl map (limit 500) → Tavily map → local sitemap.xml parse | — |
| SITE_CRAWL (async job) | sequential | — | Firecrawl start_crawl → Tavily crawl (small limit) → local crawl4ai | Returns `job_id`. Policy caps `limit` at 100 or fewer. |
| SITE_INTERACT | single | — | Firecrawl interact only | Off by default. Needs an admin toggle **and** a per-call `confirm` flag. |
| DEVELOPER_SEARCH | fan-out | Firecrawl developer_search + GitHub `/search/issues?search_type=hybrid` + Exa (fast, highlights, includeDomains docs/stackoverflow/github) | Serper (`site:stackoverflow.com`) → Brave | Dedup on canonical GitHub URLs; Firecrawl ids look like `issue:owner/repo#n`. Local limiter: semantic/hybrid issue search is 10/min. |
| REPO_SEARCH | fan-out | GitHub `/search/repositories` (+ GraphQL for README/topics) + Firecrawl developer (types=[readme], min_stars) | — | GitHub is authoritative for stars, forks and pushed_at. |
| CODE_SEARCH (sub-mode) | single | GitHub `/search/code` | Firecrawl developer (types=[doc]) | Throttle locally to 10/min per account. The syntax is the legacy one. |

---

## 9. Quota observability: unified classification

**Classifier.** Map (provider, status, headers, body tag or message) to one of:
`OK | RATE_LIMITED(retry_at) | EXHAUSTED(scope=key|team, until) | PLAN_BLOCKED | AUTH_INVALID | BAD_REQUEST | URL_FAILED | TRANSIENT`.
This extends `research-mcp/src/failure_reason.py` and `_http.py` (`_CREDIT_MARKERS`: "not enough credits", "insufficient credits", "insufficient balance", "out of credits").

| Provider | RATE_LIMITED | EXHAUSTED | PLAN_BLOCKED / other |
|---|---|---|---|
| Exa | 429 `RATE_LIMIT_EXCEEDED`, `CONCURRENCY_LIMIT_REACHED`; `Retry-After` | 402 `NO_MORE_CREDITS` / `TEAM_BUDGET_EXCEEDED` (team scope) / `API_KEY_BUDGET_EXCEEDED` (key scope) | 403 `FEATURE_DISABLED`; per-URL crawl tags → URL_FAILED |
| Firecrawl | 429 (+ `Retry-After`) | 402 "Insufficient credits" | 403 `PROVIDER_TERMS_REQUIRED`; 403 not-supported or prompt-injection → URL_FAILED |
| GitHub | 403/429 with `remaining:0` (→ reset) or `retry-after`; GraphQL 200 + errors | n/a | 401; 403 without markers; 422 |
| Tavily | 429 + `Retry-After` | **432** (plan), **433** (pay-as-you-go cap) | 401 |
| Perplexity | 429 + `Retry-After` | **401** (also means a bad key, so treat as ambiguous) | 400 |
| Jina | 429 | 402 (retry once; reported spurious) | — |
| Brave | 429 + `X-RateLimit-Reset` | monthly `X-RateLimit-Remaining` = 0; 402 | 422 invalid token |
| Serper | 429 [U] | **400 "Not enough credits"** | 401/403 |
| Linkup | 429 "too many" | **429 "run out of credit"** | 402 is x402-only |
| Parallel | 429 | 402 (available balance) | 401/422 |

**Proactive meters** (feed into the OmniRoute-style quota tracker and cool down *before* a 402):
- Exa `costDollars`
- Firecrawl `creditsUsed` + `/v2/team/credit-usage`
- Tavily `usage` + usage endpoint (10 per 10 min)
- Linkup `/credits/balance`
- Brave monthly remaining header
- GitHub `x-ratelimit-*`
- Parallel `usage`

**Key scope vs team scope.** Exa and Firecrawl rate limits are **team-wide**, so a 429 on one key must cool down **all keys of that team**. A per-key budget 402 (`API_KEY_BUDGET_EXCEEDED`) cools down only that key. The account model therefore needs `provider → org/team → keys`.

---

## 10. Patterns worth borrowing

### 10.1 From `spences10/mcp-omnisearch` (TS, MIT)

1. **Declarative provider definitions** (`src/server/provider-definitions.ts`, `provider-registry.ts`).
   - Each provider is `{id, category, api_key_name, tools, modes, capabilities[], create()}`.
   - A missing key registers the provider as `status:'unavailable', unavailable_reason:'missing_api_key'` instead of failing.
   - Port as a Python dataclass registry, with `capabilities` set to our capability enum and `accounts[]` added.
2. **Processing providers keyed as `provider:mode`** (`make_processing_provider_key`, e.g. `firecrawl:crawl`, `tavily:map`). This maps directly onto the (capability, provider) routing table.
3. **Normalised error taxonomy** (`src/common/errors.ts` `normalize_provider_http_error`).
   - 400/422 → INVALID_INPUT, 401/403 → AUTH, 408 → TIMEOUT, 429 → RATE_LIMIT, 5xx → TRANSIENT, each with a `retryable` flag.
   - Tool errors go out as JSON `{error, type, provider, retryable}`.
   - Adopt this, but add EXHAUSTED and PLAN_BLOCKED, which omnisearch lacks.
4. **Retry with jitter** (`src/common/retry.ts`).
   - Exponential backoff from 1 s, 3 retries, 0.2 jitter, and a `should_retry` predicate.
   - Better than research-mcp's fixed 0.3 s. Keep research-mcp's rule of never retrying on 402/429 and failing over instead.
5. **Search-operator parsing and per-provider translation** (`src/common/search-operators.ts`, `docs/search-operators.md`).
   - Parses `site: -site: filetype: intitle: inurl: before: after: "exact" lang: loc: +/-term AND/OR/NOT`.
   - Brave and Kagi pass operators through.
   - Tavily maps them to `include_domains`, `start/end_date`, `exact_match` and `country`.
   - Exa gets domains as request params and the rest stays semantic.
   - GitHub uses its own qualifiers.
   - Our fan-out needs this: the consumer writes one query and each provider receives a translated one.
6. **Large-result handling** (`src/common/results.ts`).
   - Assumes 4 characters per token and caps at 20k tokens.
   - Oversized results are offloaded with a section index `{title, line}` and a read hint.
   - Our equivalent: the compact + expand pattern using evidence handles, plus section offsets in the document cache.
7. **Firecrawl job poller** (`src/common/firecrawl-utils.ts` `poll_firecrawl_job{max_attempts, poll_interval, timeout}`). Reuse its semantics in the job worker.
8. **Caveat:** omnisearch makes the **consumer choose `provider`** (the `web_search.provider` enum) and does **no cross-provider merge or rerank**; there are no hits for merge, dedup or rerank in `src/`. Take the adapters and the operator translation, not the orchestration. Fusion comes from research-mcp.

### 10.2 From `metatool-ai/metamcp` (TS, MIT)

1. **Functional middleware chain** (`apps/backend/src/lib/metamcp/metamcp-middleware/functional-middleware.ts`).
   - Handlers are wrapped `handler → handler`, with request and response transformers.
   - Context: `{namespaceUuid, sessionId, endpointName, auth{method, apiKeyUuid, oauthUserId}}`.
   - Composed in `metamcp-proxy.ts:644–666`: list = overrides ∘ filter; call = audit ∘ filter ∘ overrides. Rate-limit and authorization slots exist but are stubbed.
   - Map to FastMCP middleware or a small Python `compose()` covering auth, audit, policy, cache and quota.
2. **Namespaces and endpoints.** A namespace groups tools; an endpoint exposes a namespace at a URL with its own auth. For us this means per-client profiles, e.g. `/mcp/chatgpt` exposes only `search`/`fetch` while `/mcp/full` exposes every capability.
3. **Tool filtering** (`filter-tools.functional.ts`).
   - Stores ACTIVE/INACTIVE per tool per namespace in the DB, behind a TTL cache.
   - Enforced on **both list and call**, so a hidden tool cannot be invoked by name.
4. **Tool overrides** (`tool-overrides.functional.ts`).
   - Per-namespace `override_name`, `override_description` and merged `override_annotations`, with a reverse-name cache for routing.
   - Lets the admin UI tune capability-tool descriptions per client with no code changes.
5. **`{Server}__{tool}` naming** (`tool-name-parser.ts`, split on the first `__`). Use internally for logs and replay (`exa__search`, `firecrawl__scrape`), even though upstream tools stay hidden.
6. **Audit middleware** (`audit-requests.functional.ts`). Records tool, arguments, `durationMs`, result or error, and auth identity. This is the basis for the request log and replay.
7. **Server error tracker** (`server-error-tracker.ts`). Counts crashes per upstream and marks the upstream ERROR after N. A minimal breaker for the hosted-MCP upstreams (Scite, Elicit, Undermind, Consensus); OmniRoute's breaker stays the main one.
8. **Upstream session pool** (`mcp-server-pool.ts`, `list-handler-recovery.ts`, `session-error.ts`).
   - Keeps N pre-warmed idle sessions per upstream, with a connection cap.
   - On a recoverable session error it invalidates and reconnects once.
   - Relevant to the OAuth MCP upstreams.
9. **Tool-sync hash** (`tools-sync-cache.ts`). A sha256 of sorted tool names per upstream. Use it to detect upstream schema drift and alert.
10. **Header forwarding allowlist** (`header-forwarding.ts`). Per-upstream `forward_headers` with sanitised values.
11. **OpenAPI exposure** (`routers/public-metamcp/openapi/*`). Serves the same tools as REST with generated schemas. Useful for non-MCP clients and the admin "test" button.

### 10.3 From the vendor MCP servers

- **Exa:**
  - Keep the default tool surface minimal (`toolRegistry.ts`).
  - Parse a `category:` prefix out of the query.
  - Sanitise responses by stripping `requestTags` (`exaResponseSanitizer.ts`).
  - Set per-tool timeouts.
  - Send an `x-exa-integration` header for attribution.
- **Firecrawl:**
  - Put machine-readable `next_actions` in errors, e.g. `{kind:'retry_later', after_seconds:30}` (`index.ts:1600`). Adopt this for consumer-facing RATE_LIMITED responses.
  - `SAFE_MODE` strips parameters that can write.
  - Use an idempotency `requestId` on costly calls.
  - Post raw requests so `creditsUsed` survives.
- **Tavily:**
  - `DEFAULT_PARAMETERS` env JSON works like per-provider policy defaults; implement it in the policy editor.
  - For research: exponential poll backoff and a 404 check.

---

## 11. Open items

1. Firecrawl: credit cost of `/v2/search/developer` and `/v2/search/research/*`. It is undocumented; measure it with `creditsUsed`.
2. Firecrawl: HTTP method for developer search. The MCP uses GET and the docs show POST; the SDK handles it.
3. Exa: whether `category:"github"` or `"pdf"` still works during deprecation, and whether rate-limit headers exist.
4. Perplexity: how to tell "out of credits" from "bad key" on a 401; whether the per-org balance is readable through the API; and the API ToS text (the page returned 403 to our fetcher).
5. GitHub: whether the MCP server exposes `search_type` (semantic); latency of semantic issue search.
6. **Policy for the design doc:** one account per provider per legal entity. Multiple keys inside it are for cost labels and per-key budgets only. For GitHub, one personal PAT plus at most one machine account or App. No free-tier multiplication. Keep the gateway single-user and do not expose it to third parties, because of Exa/Firecrawl resale clauses.
