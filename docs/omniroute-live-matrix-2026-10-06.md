# Installed OmniRoute operation matrix — 2026-10-06

Observed against the configured private `/v1` endpoint and the installed local
OmniRoute container (`/app/package.json`: **3.8.51**). API-scoped credential
only; provider/account management endpoints return 403, so commodity account
counts, ownership and same-provider rotation cannot be asserted. No tokens,
private account IDs, returned content or full URLs are recorded.

`POST /v1/search` used the exact provider ID, the same generic nonsensitive
query and `max_results=8`; a second generic/academic query checked usefulness.
`news` was requested separately only for advertised providers. The server's
20-ID catalog is discovery, not entitlement evidence. These are explicit
live calls, not fixture tests or multiple votes inferred from catalog entries.

| Search ID | Observed web outcome / exact provenance | News | Classification / routing decision |
|---|---|---|---|
| `exa-search` | HTTP 200, 8 ranked/attributed hits, outer/citation IDs match; cached varies | Not advertised in tested route | **ACTIVE_GENERIC**; independent source observed |
| `ollama-search` | 200, 8 useful/attributed hits | Not advertised | **ACTIVE_GENERIC** in new-install defaults; live source available, not proven independent beyond inspected results |
| `jina-search` | 200 with 1 hit, later 504 at limit 8, then 200 with 8 hits of which some lacked useful snippets | HTTP 400 for news | **AVAILABLE_INACTIVE**; unstable eight-hit response and thin snippets. Distinct from Jina Reader |
| `tavily-search` | 200, 8 attributed hits, academic top results were sometimes less relevant | 200, 8 attributed news hits | **ACTIVE_GENERIC** in new-install defaults; quality varies by query |
| `firecrawl` | 200, 8 attributed hits | 200, 8 attributed news hits | **ACTIVE_GENERIC** in new-install defaults; direct Firecrawl map/crawl remains a separate specialist gap |
| `serper-search` | 200, 7–8 attributed hits | 200, 8 attributed news hits | **ACTIVE_GENERIC**; sole Google-wrapper family in defaults. SearchAPI not added as second Google vote |
| `linkup-search` | HTTP 429 with upstream_error, no provider/results, no Retry-After or reset headers | Not probed | **BROKEN/UNVERIFIED** under current quota; no silent substitution |
| `anysearch-search` | 200, 8 attributed hits; 4–7/8 URL overlap with Firecrawl on tested queries | HTTP 400 for news | **AVAILABLE_INACTIVE** as a search vote pending independent-source/quality evidence; exposed as exact provider operation for opt-in |
| `nimble-search` | 200, 8 attributed hits; partially distinct URLs from Firecrawl | 200, 8 attributed news hits | **ACTIVE_GENERIC** in new-install defaults |
| `duckduckgo-free` | 200, 8 useful/attributed hits | Not advertised | **ACTIVE_GENERIC** in new-install defaults |
| `brave-search` | HTTP 400, upstream credential unavailable | Not re-probed | **BROKEN/UNVERIFIED**; removed from new-install defaults, remains on previously saved live route until route edit approved |

Every HTTP 200 above returned the **requested** outer provider and matching
per-hit `citation.provider` with numbered positions; no failed explicit
provider returned a different provider. Catalog flags alone were not treated
as source independence. Exact URL overlap is expected to be conservatively
deduped by the engine; high overlap prevented activating AnySearch as another
RRF source. A separate public MCP fixture checks actual-provider alias
normalization, plain RRF, concurrent fanout and partial/cache behavior.

`POST /v1/web/fetch` used an exact `provider`, markdown format and two public
URLs (`example.com` and MCP docs). Generic-doc checks required matching outer
provider, returned URL and useful source text for the MCP documentation URL.

| Fetch ID | Observed outcome | Classification / routing decision |
|---|---|---|
| `jina-reader` | 200, public documentation text approximately 7,320 chars | **READ_ONLY**, first new-install reader |
| `firecrawl` | 200, approx. 4,110 chars | **READ_ONLY**, second new-install reader |
| `tavily-search` | 200, approx. 3,036 chars | **READ_ONLY**, third new-install reader |
| `tinyfish` | 200, approx. 2,286 chars | **AVAILABLE_INACTIVE**; generic URL fetch worked, but no direct adapter marker was added without additional cost/entitlement evidence |
| `anysearch-search` | 200, approx. 3,908 chars | **AVAILABLE_INACTIVE** read: exact operation works; held out pending provider-family and quality assessment |
| `nimble-search` | 200, approx. 6,587 chars | **READ_ONLY**, fourth new-install reader |
| `context7` | HTTP 400 for generic URL | **SPECIALIST**, library-doc semantics, explicit-only markdown; not a generic web-search/read RRF vote |
| `exa-search` | HTTP 400 `bad_request` on fetch | **BROKEN/UNVERIFIED** for generic fetch: catalog/UI `webFetch` claim conflicts with current dispatcher. Exa search remains separately working |

No `provider` was omitted, no OmniRoute automatic selection was requested,
and no commodity account was imported into Research Engine. Fetch results
with text but no attested source/URL would not qualify as a generic read.

## Final saved routes and live MCP acceptance

The owner explicitly approved the exact saved SQLite route replacements.
The three routes were changed in one transaction with a prior-value guard,
without touching other routes, account secrets or provider enablement:

- `web_search` fanout: `omni:duckduckgo-free`, `omni:exa-search`,
  `omni:serper-search`, `omni:ollama-search`, `omni:tavily-search`,
  `omni:firecrawl`, `omni:nimble-search`.
- `news_search` fanout: `omni:serper-search`, `omni:tavily-search`,
  `omni:firecrawl`, `omni:nimble-search`.
- `web_read` sequential: `omni:jina-reader`, `omni:firecrawl`,
  `omni:tavily-search`, `omni:nimble-search`, `trafilatura`.

Brave is absent from the saved active routes. Jina Search, AnySearch,
Linkup, TinyFish, Context7 and SearchAPI remain inactive. Serper is the only
Google-result-wrapper vote. Commodity credentials/account pools remain in
OmniRoute; the engine retains one encrypted service connection.

**Actual authenticated MCP:** one uncached web query started all seven
configured providers; all succeeded in approximately **3.94 s** while their
recorded provider latencies totaled approximately **20.36 s** (evidence of
concurrent starts, not sequential fallback). Output was `complete`, eight
ranked items, seven distinct upstream provider votes, all marked
`transport=omniroute`. Eight merged hits had multi-source exact clusters;
each final score matched plain `Σ 1/(60 + best_rank_per_actual_provider)`.
No item lacked a canonical handle or contained a duplicate same-provider
vote. A separate live news query returned `partial` when Nimble failed;
Serper, Tavily and Firecrawl remained in `coverage.ok`, with Nimble in
`coverage.failed`, without a direct commodity retry. This verifies truthful
partial outcomes, not a manufactured complete cache entry.

A normal `web_read` on public MCP documentation returned Jina Reader source
text and a handle from the first sequential reader; a second source was not
called after usable text. A `fresh=true` read returned explicit
`NO_PROVIDER_AVAILABLE`: all four bridged readers reject upstream cache
bypass, and local trafilatura also failed that request. Do not claim live
fresh-source acceptance. Other reader mappings were verified through their
explicit upstream fetch endpoints with useful public documentation text, but
MCP fallback to a second reader was not observed live; a controlled MCP
fixture covers sequential progression.

**Final post-cutover checks:** `uv sync --frozen --extra dev`, frozen pytest
**403 passed** (855 dependency deprecation warnings), Ruff and
`git diff --check` passed. The rebuilt Docker/Compose service remained
healthy and listed 17 MCP tools. Linkup's 429 reset/account facts and
OmniRoute account inventory remain unknown; no purchase or top-up was made.
