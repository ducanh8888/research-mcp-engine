# Routing and result fusion

The public MCP tool fixes the capability. Saved SQLite routes hold an ordered provider list and required execution mode; registration alone does not enable a provider. The order is relevant for reads and single-job starts, but it is **not** a search fallback ranking. Operator route edits keep existing accounts and state.

## Search evidence

`web_search`, `news_search`, `paper_search`, `paper_related`, `developer_search`, and `repo_search` fan out to all eligible independent providers in their routes concurrently under a shared deadline. One successful or zero-hit list does not stop another provider. Covered web operations call a selected, explicit OmniRoute provider ID; an OmniRoute outage does not silently invoke a retained direct commodity adapter. Direct academic and developer providers run independently.

Results carry actual provider, rank, URL and identifier provenance. The router rejects double-counting direct/bridge aliases of the same actual upstream operation. Retrying another account for auth/rate/quota availability within one provider does not create another source list. A failing, unavailable, or timed-out peer is reflected in `coverage` and `status: partial`; successful peers survive. A fully failed route returns an explicit unavailable error. Reduced-coverage results cannot be stored as complete query-cache entries.

Successful search hits are normalized, conservatively clustered using exact identifiers and URLs with strong-identifier conflict guards, then scored by plain reciprocal rank fusion, `Σ 1/(60 + rank)`, with one best rank per actual provider. RRF scores are retrieval signals, not confidence or truth claims. Optional rerank runs **after** fusion only when configured, validates indices/scores, and retains RRF order on failure. Weighted and hierarchical fusion and fuzzy relationship helpers in the source are disabled experiments, not the active search policy.

## Other operations

- `web_read` and `paper_read` try applicable sources sequentially until one returns a usable document; returned text can be paginated with a cursor. Search pointers are not full text.
- `paper_metadata` tries later sources for unresolved identifiers. It retains per-input source assertions and distinguishes an ambiguous or unsupported citation from a definitive miss; matching title/year with a strong identifier is needed to establish plain citation text.
- `citation_verify`, `citation_graph`, `editorial_check`, and `site_map` aggregate attributable records from applicable sources. RRF does not resolve conflicting bibliographic or editorial claims. A citation tally is not validation of a specific claim, and no reported notice means unknown rather than clearance.
- `site_crawl` and `systematic_review` have seeded providers and initiate one recoverable upstream job when an eligible account is available. `deep_literature_search` has an Elicit API adapter but no seeded route; it requires an explicit route and an eligible account. `get_job` checks client ownership and returns persisted state; an ambiguous start does not trigger another submission.

`fresh=true` bypasses the gateway's query/document cache only. It is never forwarded to an adapter, so it cannot make an otherwise valid request fail; an upstream provider may still answer from its own cache.

## Search filters

OmniRoute's `/v1/search` accepts `filters.include_domains` and `time_range` but silently ignores them for providers whose handler does not apply them (its `strict_filters` flag is not enforced). From the installed handler source, domain filters reach Exa, Tavily, Nimble and Firecrawl, and `time_range` reaches Nimble and Firecrawl; Serper, Ollama and DuckDuckGo receive neither, and no routed provider supports absolute date ranges. The router forwards a filter only to verified providers. A routed provider that cannot apply a requested filter is **skipped** (`coverage.skipped`, reason `unsupported filter: …`, status `partial`) and never contributes unfiltered results; if no routed provider can apply it, the request fails with `INVALID_INPUT` before any upstream call. `code_search scope=docs` uses the same rule: GitHub's developer search returns issues, so only Firecrawl's developer index serves documentation.

## Public workflow dispatch

The MCP surface exposes nine workflow tools. Each validates its operation/scope and arguments, then dispatches to exactly one internal capability: `search` → `web_search`/`news_search` (by `focus`); `read` → `web_read`/`paper_read` (DOIs, arXiv IDs, doi.org/arxiv.org links and scholarly handles count as papers under `source_type=auto`); `paper_search` → `paper_search`; `paper_explore` → `paper_metadata`/`paper_related`/`citation_graph`; `verify` → `citation_verify`/`editorial_check`; `code_search` → `developer_search` (`docs`) or `repo_search` (`repositories`/`code`/`issues`); `site_research` → `site_map`/`site_crawl`; `deep_research` → `deep_literature_search`/`systematic_review`; `get_job` polls jobs. Routing, fusion and coverage described above are unchanged by the dispatch layer, and an invalid combination fails before routing.
