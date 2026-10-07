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

`fresh=true` bypasses the gateway's query/document cache only; an upstream provider may still use its own cache. Providers that cannot honor a requested upstream bypass report an error instead of claiming fresh evidence. See [architecture](architecture.md) and [providers](providers.md).
