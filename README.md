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

**Implemented:** 9 workflow MCP tools over 16 internal capabilities for web/news, academic, citation/editorial, site, developer/repository and job operations. Search results are pointers, not full text; use `read` on a returned URL or handle. Search-like routes execute enabled, configured independent providers concurrently. Ordinary results include `status`, `coverage`, and source attribution. Reads are paginated, and asynchronous operations use persisted jobs where a provider is configured. An optional reranker is wired into search and disabled by default.

**Conditional / experimental:** Hosted Scite and Elicit MCP search adapters validate known tool schemas but require eligible accounts and successful OAuth connection. `deep_research operation=literature` has an Elicit API adapter but no default seeded route; until one is configured it returns `NO_PROVIDER_AVAILABLE`. `code_search scope=docs` uses Firecrawl's developer index and needs a Firecrawl key. Some direct commodity adapters remain registered for compatibility but are not seeded as fallback routes for covered OmniRoute operations. Local/OmniRoute reranking needs deployment-specific configuration and evaluation before enabling.

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

The public interface is nine workflow tools. Agents choose a workflow and, where relevant, an
operation or scope; the engine selects the internal capability, saved route, providers and
accounts. Provider names, accounts and transports are never tool parameters.

| Tool | Use it for | Key inputs | Internal capability |
|---|---|---|---|
| `search` | Default for everyday and current information | `query`, `focus` (`web`/`news`), `domains`, `recency`, `limit` | `web_search`, `news_search` |
| `read` | Source text of a URL, returned handle, DOI or arXiv ID | `target`, `source_type` (`auto`/`web`/`paper`), `cursor` | `web_read`, `paper_read` |
| `paper_search` | Academic literature discovery | `query`, `year_from`, `year_to`, `limit` | `paper_search` |
| `paper_explore` | Known papers: identity, related work, citation network | `operation` (`metadata`/`related`/`citations`), `ids` or `citation`, `relation`, `direction`, `depth` | `paper_metadata`, `paper_related`, `citation_graph` |
| `verify` | Citation checks, claim evidence, retraction/editorial notices | `operation` (`citation`/`claim`/`editorial`), `citation`, `claim`, `ids` | `citation_verify`, `editorial_check` |
| `code_search` | Library docs, repositories, code, issues | `query`, `scope` (`docs`/`repositories`/`code`/`issues`), `repos`, `language`, `min_stars` | `developer_search`, `repo_search` |
| `site_research` | List a site's URLs or start a bounded crawl | `url`, `operation` (`map`/`crawl`), `limit`, `max_depth`, include/exclude paths | `site_map`, `site_crawl` |
| `deep_research` | Long literature search or systematic review job | `operation` (`literature`/`systematic_review`), `question`, `criteria` | `deep_literature_search`, `systematic_review` |
| `get_job` | Poll a job started by `site_research` or `deep_research` | `job_id`, `wait_s` | job store |

Search tools return ranked pointers (handle, URL, snippet, provenance), not full text; read the
important ones. Web and paper search are separate evidence pools. `status=partial` means some
intended sources failed, were skipped or timed out; check `coverage`. Provenance attributes a
source and is not a judgment of truth; citation tallies never verify a claim. Long reads are
paginated with `next_cursor`. `fresh=true` bypasses this server's cache only; upstream services
may still answer from their own caches.

**Filters.** `search` supports `domains` (Exa, Tavily, Nimble, Firecrawl) and `recency`
(Nimble, Firecrawl), the filters OmniRoute actually forwards. Sources that cannot apply a
requested filter are skipped and listed in `coverage` (never returned unfiltered); if no routed
source can apply it the call fails with `INVALID_INPUT`. Absolute date ranges are not offered.
Invalid operation/argument combinations also fail before any upstream request.

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

A research agent asked “Find recent evidence about reproducibility in open access research” can call `paper_search`, inspect coverage, then `read` a returned paper handle. For general questions it calls `search`, then `read` on the strongest sources. Provider calls may fail or incur upstream usage according to your configuration.

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
