# Providers and operations

A provider is registered in code; a route determines whether it is selected for a capability. First boot seeds the routes below. Operators can change enabled providers, accounts, and routes in `/admin` without exposing provider IDs to MCP clients. Empty API keys do not create credentialed accounts. Subscription, quota, and API-plan eligibility are controlled upstream.

| Provider / transport | Implemented capabilities | Initial route / access |
|---|---|---|
| OmniRoute `duckduckgo-free`, `exa-search`, `serper-search`, `ollama-search`, `tavily-search`, `firecrawl`, `nimble-search` | `web_search` | Seeded. One OmniRoute URL/key; account availability upstream. |
| OmniRoute `serper-search`, `tavily-search`, `firecrawl`, `nimble-search` | `news_search` | Seeded through the same connection. |
| OmniRoute `jina-reader`, `firecrawl`, `tavily-search`, `nimble-search` | `web_read` | Seeded, tried sequentially for the same URL. |
| OmniRoute `brave-search`, `jina-search`, `linkup-search`, `anysearch-search` | Search and/or fetch as registered | Implemented bridge mappings, **not seeded**. Enabling requires an eligible upstream and operation check. |
| trafilatura (local) | `web_read` | Seeded last; no provider credential. |
| OpenAlex | Paper search/read/metadata/related, citation verification/graph, editorial notices | Direct, seeded. Public API; optional key. |
| Crossref | Paper search/metadata, citation verification, editorial notices | Direct, seeded. Public API; optional Plus token. |
| Semantic Scholar | Paper search/read/metadata/related, citation verification/graph | Direct, seeded. Public API; optional key. |
| arXiv | Paper search/read/metadata, citation verification | Direct, seeded. Public API; no key. |
| Scite REST | Paper metadata, citation verification/tallies, editorial notices | Direct, seeded where applicable; endpoint/plan eligibility varies. |
| Consensus API | Paper search | Direct, seeded; requires an eligible API key. |
| Elicit API | Paper search, systematic-review/deep-search jobs | Direct; review job seeded, search/deep-search have adapters but no seeded routes. Requires eligible API access. |
| Scite and Elicit hosted MCP | Paper search | Direct, registered but not seeded; require account OAuth and compatible upstream tool schema. |
| GitHub REST | Developer/repository search and supported URL reads | Direct, seeded. Public requests or optional token; code search may require auth/plan. |
| Firecrawl direct | Site map and persistent crawl job | Direct, seeded; Firecrawl key required. Search/fetch routes use OmniRoute instead. |

Registered direct Exa, Tavily, Brave, Serper, Jina, Firecrawl search/fetch and DuckDuckGo adapters are retained for compatibility and non-commodity gaps. Covered direct search/fetch operations are **not** a backup route when OmniRoute is unavailable. Undermind is not registered. Source registration does not establish a working live account or an independently validated provider operation.

## Configuration

- `research-engine init` imports nonempty values in `.env` into encrypted SQLite account records. `import-env` repeats the import after first boot. `OMNI_ROUTE_API_URL` sets the bridge connection's API root; `OMNI_ROUTE_API_KEY` authenticates that connection.
- OmniRoute chooses its own commodity accounts; the gateway requests an explicit upstream provider ID and checks returned identity. Only the upstream provider counts as evidence.
- A direct-provider variable may hold several comma-separated keys, for example `CONSENSUS_API_KEY=key1,key2`. Import creates accounts `environment`, `environment-2`, … with ascending priority; removing a key from the list disables its `environment-N` account. `OMNI_ROUTE_API_KEY` is a single connection credential.
- Direct specialist accounts are managed in `/admin`. Priority, round-robin, and quota-aware selection choose a usable account **within** a provider for authentication/rate/quota availability. They never add an evidence vote.
- Public providers can be keyless but still have limits or require credentials for particular operations. A skipped or failed provider is visible in `coverage`; it is not an empty successful result.

See [routing and fusion](routing-and-fusion.md) for execution and [deployment](deployment.md) for bootstrap and security settings.
