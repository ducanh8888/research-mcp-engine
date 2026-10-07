# Research MCP

An MCP research gateway for AI agents and developers who need evidence from multiple web, academic, and specialist sources through one interface. Connect one server instead of wiring every provider's API, authentication, and output format into your agent. The engine queries independent configured sources concurrently, keeps source attribution, conservatively deduplicates results, and returns partial evidence when a provider fails.

**Status:** Active development (Research MCP is the project name; the Python package and CLI are `research-engine`, version 0.1.0). Provider access depends on your accounts, upstream availability, and configured routes; tool discovery alone does not imply a provider is ready.

## Why

Research providers expose different APIs, credentials, limits, and result shapes. Provider-specific agent integrations are hard to combine, and first-success fallback discards independent evidence. This gateway offers stable MCP capabilities with explicit coverage and provenance so the consumer can inspect sources rather than assume that a search hit or citation tally is true.

## Architecture

```text
MCP client (agent, editor, or application)
            │ Streamable HTTP /mcp (Bearer token)
            ▼
Research MCP (FastMCP + FastAPI)
  capability routes → concurrent independent providers
  normalized results → conservative exact dedup → plain RRF
  optional validated rerank → attributed output + coverage
            │
            ├── OmniRoute: explicit-provider web search/fetch
            ├── Direct academic, developer, and site adapters
            └── Local reader / optional local reranker

SQLite: routes, encrypted account secrets, handles, cache, jobs
SQLAdmin /admin: password-protected operator configuration
```

Searches fan out; reading one URL, resolving unresolved metadata IDs, and starting one asynchronous job use sequential execution where required. There is no automatic query classifier or autonomous research planner.

## Capabilities

**Implemented:** 17 MCP tools for web/news, academic, citation/editorial, site, developer/repository, and job operations. Search results are pointers, not full text; use a read tool on a returned URL or handle. Search-like routes execute enabled, configured independent providers concurrently. Ordinary results include `status`, `coverage`, and source attribution. Reads are paginated, and asynchronous operations use persisted jobs where a provider is configured. An optional reranker is wired into search and disabled by default.

**Conditional / experimental:** Hosted Scite and Elicit MCP search adapters validate known tool schemas but require eligible accounts and successful OAuth connection. `deep_literature_search` has an Elicit API adapter but no default seeded route; it requires an explicitly configured route and an eligible upstream account. Some direct commodity adapters remain registered for compatibility but are not seeded as fallback routes for covered OmniRoute operations. Local/OmniRoute reranking needs deployment-specific configuration and evaluation before enabling.

**Not provided:** automatic research planning, interpretation of evidence, a client-facing OAuth authorization server, or guaranteed access to paid providers. Undermind is not registered; unavailable hosted tools do not become usable merely because their MCP names appear upstream.

## Providers

Default routes are seeded on first initialization; an operator can edit them in `/admin`. Enabled routes and usable accounts determine what actually runs. The upstream provider, not an account or transport alias, is one evidence source. For the complete capability/transport matrix and optional adapter details, see [providers](docs/providers.md).

| Source | Seeded use | Connection |
|---|---|---|
| DuckDuckGo, Exa, Serper, Ollama Search, Tavily, Firecrawl, Nimble | Web search via explicit OmniRoute provider IDs | OmniRoute service key and URL; upstream accounts managed there |
| Serper, Tavily, Firecrawl, Nimble | News search via OmniRoute | Same connection; provider access varies |
| Jina Reader, Firecrawl, Tavily, Nimble | Sequential web reading via OmniRoute | Same connection |
| trafilatura | Local web extraction after configured readers | No provider key |
| OpenAlex, Crossref, Semantic Scholar, arXiv | Direct academic search, metadata, reading, relations, citations as supported | Public endpoints; optional keys for applicable APIs |
| GitHub | Direct repository, code/developer search and supported reads | Public access or optional token; API entitlements/rate limits apply |
| Firecrawl | Direct site map and asynchronous crawl | Firecrawl key |
| Scite REST, Consensus API, Elicit API, Scite/Elicit hosted MCP | Optional academic/editorial or hosted search/review operations | Appropriate API account or OAuth/plan; hosted MCP search and deep search are not seeded |

Other implemented bridge IDs and retained direct adapters are catalogued in [providers](docs/providers.md). No automatic fallback to a retained direct commodity adapter occurs when a covered OmniRoute operation fails.

## MCP tools

| Tool | Purpose and important inputs |
|---|---|
| `web_search` | Web source pointers (`query`, optional `limit`, domain/date filters). |
| `news_search` | News pointers (`query`, optional `recency` or date bounds). |
| `web_read` | Read a URL or search handle (`target`, optional pagination `cursor`). |
| `site_map` | List URLs from a site (`url`, `limit`); requires a suitable provider. |
| `site_crawl` | Start a bounded site crawl (`url`, `limit`, `max_depth`); returns a job. |
| `paper_search` | Search scholarly sources (`query`, optional year bounds). |
| `paper_read` | Read available paper text or a labeled abstract (`target`, `cursor`). |
| `paper_metadata` | Resolve DOI/other IDs or conservatively match a citation (`ids` or `citation`). |
| `paper_related` | Similar, citing, or cited papers (`seeds`, `mode`, `limit`). |
| `citation_verify` | Bibliographic assertions and separately attributable claim evidence (`citation`, optional `claim`). |
| `citation_graph` | Citation nodes/edges (`seeds`, `direction`, `depth`); may be truncated. |
| `editorial_check` | Source-attributed editorial/retraction notices (`ids`); missing data is unknown. |
| `systematic_review` | Start an eligible upstream review (`question`, optional `criteria`); returns a job. |
| `deep_literature_search` | Start a hosted deep-search job (`goal`) only after an explicit route and eligible Elicit API account are configured. |
| `developer_search` | Technical/repository evidence (`query`, optional `repos` and `language`). |
| `repo_search` | Repositories, code, or issues (`query`, `mode`, optional `language`, `min_stars`). |
| `get_job` | Poll an owned job (`job_id`, optional `wait_s`); not a search fallback. |

Many tools accept `fresh` and `deadline_s`. `fresh=true` bypasses the engine cache; an upstream that cannot guarantee cache bypass can reject it. A `partial` result means at least one routed source failed, was skipped, or timed out; inspect `coverage` before relying on the result. Citation provenance is attribution, not proof of a claim.

## Retrieval semantics

All enabled, configured, semantically independent sources in a search route run concurrently within one deadline: the first success does not stop the others. A failing source reduces coverage without discarding successful peers. Direct and OmniRoute access to the same actual upstream cannot create two votes; same-provider account retries for auth/rate/quota availability also contribute only one list. The engine preserves provider/rank provenance, applies exact ID/URL-based deduplication with strong-identifier conflict guards, then plain reciprocal rank fusion (`Σ 1/(60 + rank)`). Optional reranking changes only the validated leading order; failure retains RRF order. Metadata, citation verification, graphs, and editorial assertions aggregate without RRF. See [routing and fusion](docs/routing-and-fusion.md).

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and a network connection for live upstream calls. From the repository root:

```bash
uv sync --frozen --extra dev
cp -n .env.example .env
uv run --frozen research-engine init --config config.yaml
uv run --frozen research-engine serve --config config.yaml
```

`init` writes ignored `config.yaml`, SQLite state, encryption/session keys, and a one-time admin password and MCP client token to `data/bootstrap.json` (mode 0600 for the new secret file). Do not publish this file or paste its contents into logs. In another terminal, check health and enumerate tools without calling a provider:

```bash
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json
```

For a safe example using configured public academic endpoints, run:

```bash
uv run --frozen python scripts/mcp_smoke.py --token-file data/bootstrap.json \
  --tool paper_search --arguments '{"query":"open access research reproducibility","limit":3}'
```

A research agent asked “Find recent evidence about reproducibility in open access research” can call `paper_search`, inspect coverage and citations, then call `paper_read` on a returned paper handle. For general web research it can call `web_search` then `web_read`. Provider calls may fail or incur upstream usage according to your configuration.

## Configuration and deployment

`.env.example` lists optional provider import variables with **empty values**. `OMNI_ROUTE_API_URL` (an API root ending in `/v1` or `/api/v1`) and `OMNI_ROUTE_API_KEY` connect the bridge. `GITHUB_TOKEN`, `FIRECRAWL_API_KEY`, and academic API keys are optional for their respective adapters. `init`/`import-env` import nonempty keys into the encrypted account store; commodity provider credentials belong in OmniRoute. YAML `config.yaml` controls bootstrap settings, listener, public base URL, and trusted origins; accounts and routes live in SQLite. Never commit runtime config or the `data/` directory. See [configuration and deployment](docs/deployment.md).

The implemented transport is **Streamable HTTP** at `/mcp`, not stdio. Locally it binds loopback by default; clients must send their own Bearer token. `/admin` retains username/password sessions and CSRF protection. Docker Compose uses the frozen lock, persistent local data, a read-only filesystem, and a configurable host bind that defaults to loopback. HTTPS exposure requires an operator-managed TLS proxy/tunnel and matching `public_base_url`/`trusted_origins`; the OAuth callback for upstream account Connect is `<public_base_url>/admin/oauth/callback`. There is no bundled tunnel or client-facing OAuth login. See [deployment](docs/deployment.md) for the exact path and security notes.

## Development

```bash
uv sync --frozen --extra dev
uv lock --check
uv run --frozen pytest -q
uv run --frozen ruff check src tests scripts
uv build
```

Fixture tests do not spend provider credits. There is no configured type checker; Ruff and pytest are the repository checks. For local server and HTTP smoke commands, see [development](docs/development.md). Third-party reuse is credited in [NOTICE](NOTICE) and the retained [web-layer license](src/research_engine/providers/web/LICENSE.research-mcp). This repository does not yet declare a project-wide license.

Research MCP is developed and maintained by [HTL 16666 Media](https://mr16666.com).
