# 04 — Academic Providers (Scite, Elicit, Undermind, Consensus, OpenAlex, Crossref, Semantic Scholar, arXiv)

> Historical investigation, recorded before the specialist/OmniRoute bridge pivot.
> Current authority: [design v4](../design.md), [roadmap](../roadmap.md) and
> [cleanup](../cleanup.md). Recommendations, pricing, protocol and account observations
> below are dated evidence, not current requirements or live validation.

Status: research, 2026-09-29. Scope is the academic capabilities (PAPER_*, CITATION_*, EDITORIAL_CHECK,
SYSTEMATIC_REVIEW, DEEP_LITERATURE_SEARCH), plus short notes on Lune, alphaXiv, Valency and SciSpace.

Evidence levels used below:
- **[obs]**: observed live in this session, either from an MCP tool schema, a tool call, or a `curl` probe.
- **[doc]**: taken from official docs, with the URL given.
- **[3p]**: from a third-party source and not verified; confirm before depending on it.

Account-state vocabulary: READY / COOLING / RATE_LIMITED / EXHAUSTED / AUTH_REQUIRED / PLAN_BLOCKED /
DEGRADED / DISABLED.

---

## 0. TL;DR

| Provider | Access path | Auth | Current account (obs) | Best at | Cost to us |
|---|---|---|---|---|---|
| **Scite** | MCP `https://api.scite.ai/mcp`, with an unauthenticated public REST `api.scite.ai/{papers,tallies}/{doi}` | OAuth 2.1 + DCR + PKCE. API key (Bearer, `mcp` scope) needs Pro | **EXHAUSTED**: free "Connect" plan, 25/25 monthly calls used, resets 2026-10-01 | Smart Citations (supporting/contrasting), editorial notices, intent citation graph | Free 25 calls/mo. Basic $14/mo gets 250. Pro $35/mo gets 2,500 plus API |
| **Elicit** | MCP `https://elicit.com/api/mcp` or REST `https://elicit.com/api/v2` | OAuth (DCR, PKCE) or API key `elk_live_…` | **PLAN_BLOCKED** (`api_access_denied`): Basic plan has no API | Systematic review, reports, trials search, PubMed corpus | Pro $49/mo minimum |
| **Undermind** | MCP only: `https://mcp.undermind.ai/mcp` | OAuth with **CIMD only (no DCR)**, PKCE, RFC 8707 | **READY** (Free) | Deep literature search (async, 2–5 min), citation/reference semantic search, PDF QA | Free with "standard" limits. Pro $16/mo (annual) gives 10× |
| **Consensus** | MCP `https://mcp.consensus.app/mcp` or REST `api.consensus.app/v1/search` (self-serve key on **every** plan) | OAuth (DCR, PKCE), no-account mode, `x-api-key` for REST | **READY** (Free: 10 papers/search, no DOI) | Evidence-grade filters (study type, sample size, SJR, medical_mode) | Free 30 calls/mo. Pro 500 calls/mo, up to 1,000 with $0.05 overage |
| **OpenAlex** | REST `https://api.openalex.org` | `api_key` (free account); without one, $0.10/day | READY (keyless probe) | Resolver backbone, search, citations, retraction flag, OA URLs | $1/day free credit. Singletons free, lists ≈$0.0001 |
| **Crossref** | REST `https://api.crossref.org` | None (`mailto` puts you in the polite pool), Plus token optional | READY | Authoritative DOI metadata, `updated-by` retractions (Retraction Watch) | Free |
| **Semantic Scholar** | REST `https://api.semanticscholar.org` | `x-api-key` (free on request) | **RATE_LIMITED** keyless (429 on the 2nd request) | Recommendations, citation contexts/intents, snippet search, batch 500 | Free. Key gives 1 RPS |
| **arXiv** | REST (Atom) `https://export.arxiv.org/api/query` | None | READY | Preprint search/metadata, PDFs | Free, **1 request / 3 s, single connection** |

Main conclusion: route the high-volume, cheap work to the open REST sources: resolving, metadata,
editorial checks, citation graphs and most search. Treat the four hosted MCPs as **budgeted premium
providers** with hard monthly quotas. Every capability that only they cover is plan-gated, so the account
router must model monthly quotas and reset dates as well as short-term rate limits.

---

## 1. Scite

### Endpoint / auth
- MCP: `https://api.scite.ai/mcp` (Streamable HTTP, JSON-RPC). `GET /mcp/info` and `GET /mcp/health` are
  public [obs]. `/mcp/info` reports `protocolVersion: 2025-06-18` and lists 17 tools [obs]. The docs say
  "25 tools", and the extra ones sit behind `get_more_tools` (patent, clinical trial and grant datasets,
  Pro) [doc: https://docs.scite.ai/mcp/overview].
- OAuth AS metadata at `https://api.scite.ai/.well-known/oauth-authorization-server` [obs]:
  `authorize=/mcp/oauth/authorize`, `token=/mcp/oauth/token`, `registration=/mcp/oauth/register` (DCR),
  scopes `mcp offline_access`, `token_endpoint_auth_methods=none` (public client), PKCE S256, refresh tokens
  supported.
- API key alternative: create one in the API Console (**Pro only**), grant the `mcp` scope explicitly, and
  send it as a Bearer token. It "shares your account's MCP quota" [doc]. Enterprise accounts can use
  client_id/secret.
- **Public REST without a token** [obs]: `GET https://api.scite.ai/tallies/{doi}` returns
  `{total, supporting, contradicting, mentioning, unclassified, citingPublications}`, and
  `GET https://api.scite.ai/papers/{doi}` returns metadata plus **`editorialNotices`**
  (Retracted / Has correction / Has erratum / Has expression of concern / Comment, each with `noticeDoi`
  and date), along with `preprintLinks` and `publicationLinks`. Both answered in about 1.3 s, and neither
  counted against the MCP quota. The docs say "endpoints require tokens (aside from tallies and papers)"
  [doc: https://docs.scite.ai/guides/search]. The rate limits for these endpoints are not published.

### Tools (MCP)
| Tool | Purpose | Key params | Output |
|---|---|---|---|
| `search_literature` | Search, plus metadata fetch by DOI/title, plus full-text excerpts | `term` (Boolean, phrase, proximity), `dois[]`, `titles[]`, `author`, `journal`, `date_from/to`, `year`, `paper_type`, `topic`, `has_retraction/correction/concern/erratum`, `supporting_from/to`, `contrasting_from/to`, `mentioning_from/to`, `citing_publications_from/to`, `collection_slug`, `limit` (max 1000, advise 10–50), `offset` | title, authors (first 3), abstract, **DOI**, journal, year, vol/issue/page; `fulltextExcerpts` (≤5 × ~500 chars, OA only); `access` (link, type, pricing); `citations[]` (Smart Citation snippets with type supporting/contrasting/mentioning/unclassified); `tally`; `editorialNotices[]`; `isOa`, `oaStatus`, `license` |
| `read_fulltext` | Read the paper text linearly | `doi`, `offset`, `length` ≤ 8000 | `source` ∈ {`fulltext`, `abstract`, null}, `contentDenied`, `totalChars`, `hasMore`, `link`, `agAccess` |
| `citation_graph` | Citation topology from seed DOIs | `seeds[]` ≤10, `direction` in/out/both, `depth` 1–2, `max_edges` ≤2000, `include_intent`, `include_snippets` | `edges[{s,t,d,type?,section?,snippets?}]`, `papers{doi:{title,year}}`, `seed_coverage`, `low_coverage_seeds`, `truncated` |
| `bibliography` | Format DOIs as BibTeX/RIS/CSV | `dois[]` ≤500, `format` | `found`, `notFound`, `content` |
| `report_citations`, `citation_report` | Log and report include/exclude decisions (PRISMA-style audit) | `citations[{source_ref, decision, source, reason_code, stage}]` | These **write** to Scite server state. The engine should not call them. |
| Collections (`create/get/search/update/delete_collection`, `add/remove_dois_…`, `*_collection_note`) | Server-side paper sets | slug, dois | Mutating. Out of scope for the engine. |
| `get_more_tools` | Tool discovery | `context` | Exposes the Pro dataset tools |

### Capability mapping
- PAPER_SEARCH: `search_literature(term)`.
- PAPER_METADATA: `search_literature(dois=[…])` without `term`.
- PAPER_READ: `read_fulltext`. This only returns full text for OA/permissive-license papers or content the
  org is entitled to; otherwise it falls back to the abstract.
- CITATION_VERIFY: Smart Citation snippets and tallies. **This is Scite's distinctive value.**
- CITATION_GRAPH: `citation_graph`, with intent.
- EDITORIAL_CHECK: `editorialNotices`, available from both MCP and the free REST endpoint.
- PAPER_RELATED: `citation_graph` depth 1.

### Identifiers
DOI only; every record is keyed by DOI. arXiv papers appear under their DataCite DOI (`10.48550/arxiv.*`).
Seeds automatically expand across preprint and published versions. The docs warn that arXiv-heavy (ML)
papers "often under-resolve", so the engine should read `low_coverage_seeds`.

### Sync/async, latency
Everything is synchronous. The public REST endpoints took about 1.3 s [obs]. I could not time the MCP calls
because the quota was exhausted.

### Plans / limits
Per https://scite.ai/pricing [doc, fetched 2026-09-29]:
- Connect (free): **25 MCP credits/month**, no Assistant or Search.
- Basic: $14/mo (annual), **250 credits/mo**.
- Pro: $35/mo (annual), **2,500 credits/mo**, API access, patent/trial/grant datasets.
- Org licenses are available per seat, as a shared pool, or unlimited.

Monthly reset. Plan changes take up to 15 minutes to propagate, and reconnecting forces the new
entitlement to apply.

Plan-gated items (report PLAN_BLOCKED):
- API-key auth: Pro.
- Dataset tools: Pro.
- Org-shared collections: org license.
- Full text beyond OA: publisher amendment or institutional holdings.

### Error / quota signals → state
| Signal | State |
|---|---|
| Tool error text `"You have reached your monthly MCP usage limit (25 calls). Your usage resets on 2026-10-01 (UTC)…"` [obs] / JSON-RPC error **`-32001`** "Usage limit reached" [doc] | **EXHAUSTED** with `reset_at` parsed from the text (fallback: the 1st of next month, UTC). The limit number (`25`) is the plan fingerprint. |
| HTTP 401 / `WWW-Authenticate` on `/mcp` | AUTH_REQUIRED, after one refresh-token attempt |
| `get_more_tools` or dataset tool refused, org-share refused | PLAN_BLOCKED for that capability |
| `read_fulltext.contentDenied=true` | Not an account state. Record a per-item `fulltext=false` signal. |
| `low_coverage_seeds` non-empty | Per-result DEGRADED flag for the graph |
| Public REST 429/5xx | RATE_LIMITED or COOLING for the public-REST pseudo-account only |

Scite does not publish a usage endpoint over MCP. Count calls locally and reconcile against
`https://scite.ai/users/me/subscription`, which is manual.

### ToS notes
- Automated access is permitted **only via the Scite API or an official AI-assistant connector**, and plan
  limits must be respected [doc: https://scite.ai/terms].
- The MCP Terms forbid circumventing usage limits, rate limits or technical restrictions
  [doc: https://www.researchsolutions.com/scite-mcp-terms]. Using several free accounts to multiply the
  25-call quota would violate this. Separate paid accounts are only legitimate if they belong to distinct
  subscriptions.
- Paywalled text is never served. The same content restrictions apply over MCP as on the website.

### Normalization
- Lowercase DOIs.
- The REST tally uses `contradicting` where MCP uses `contrasting`; normalize both to `contrasting`.
- Map `editorialNotices.status` to the enum {retracted, correction, erratum, expression_of_concern,
  withdrawn, comment}. `Comment` is noise for editorial signals.
- `preprintLinks` / `publicationLinks` feed the version-merge step of dedup.

---

## 2. Elicit

### Endpoint / auth
- MCP: `https://elicit.com/api/mcp`. Protected-resource metadata is at
  `https://elicit.com/.well-known/oauth-protected-resource/api/mcp` [obs] and gives `resource`, AS
  `https://elicit.com/api/auth`, and scope `elicit.mcp`.
- The AS supports DCR (`/api/auth/register`), PKCE S256, auth method `none`, refresh, introspection and
  JWKS [obs].
- REST: `https://elicit.com/api/v2` with `Authorization: Bearer elk_live_…`. Keys are created at
  `elicit.com/developer`.
- MCP and REST share the same plan, quota and session-access rules
  [doc: https://docs.elicit.com/, https://elicit.com/blog/the-elicit-api-and-mcp-powering-autonomous-research-engines].
- **Implication:** once the account is Pro, the engine can call REST directly with an API key, which is
  simpler than MCP-over-OAuth and avoids the tool-text parsing.

### Tools (MCP; the REST endpoints mirror them)
| Tool (REST) | Purpose | Key params | Output / behavior |
|---|---|---|---|
| `search_papers` (`POST /search/papers`) | Semantic or keyword paper search | `query` ≤2000, `corpus` elicit\|pubmed, `searchMode` semantic\|keyword (Lucene / PubMed syntax; keyword mode excludes `filters`), `filters{minYear,maxYear,minEpochS,maxEpochS,typeTags[Review,Meta-Analysis,Systematic Review,RCT,Longitudinal],maxQuartile,includeKeywords,excludeKeywords,hasPdf,pubmedOnly,retracted}`, `maxResults` (plan cap: Pro 300 / Scale 500 / Ent 10,000; above the cap returns 400) | Sync. No pagination. |
| `search_trials` (`POST /search/trials`) | ClinicalTrials.gov search | `query`, `trialFilters{phase[],recruitmentStatus[],hasResults}`, `searchMode` | Sync |
| `create_report` (`POST /sessions/reports`) | Search → screen → extract → report | `researchQuestion`, `maxSearchPapers` ≤1000, `maxExtractPapers` ≤80 | **Async.** Returns 202 with `sessionId`. Poll with `get_report(sessionId, includeReportBody)`. Exports: PDF/DOCX/bib/ris/txt. |
| `create_systematic_review` (`POST /sessions/systematic-reviews`) | Multi-stage SR | `researchQuestion`, `searches[]` ≤20 (corpus elicit/pubmed/clinical_trials), `abstractScreening{criteria[],generate,depth fast\|thorough}`, `fulltextScreening`, `extraction{questions[],generate,useFigures}`, `generateReport` | **Async**, can run for minutes to hours. `get_systematic_review` returns CSV/XLSX per stage. Running reviews cannot be amended. `depth` and `useFigures` cost more, and the tool contract says a human must choose them. |
| `create_agent_session`, `send_agent_session_message`, `get_agent_session(_events/_artifacts)`, `stop_agent_session`, `resume_session`, `list_sessions` | Research-agent chat sessions | `query`, attachments | Async and stateful. **Do not retry blindly on timeout**: call `list_sessions` first. |
| Library (`search_library`, `get_library_source(_full_text)`, `import_library_files`, `save_library_sources`, collections, `stage_*_upload`) | User library, PDF import, parsed full text | `sourceId` | `get_library_source_full_text` returns markdown, or 404 with `full_text_pending` / `full_text_unavailable` / `not_found` |
| `get_usage` (`GET /usage`) | Quota introspection | none | `hasUsageRemaining`, `percentUsed`, `periodStart`/`periodEnd`, `extraUsage` (cents) |
| Shares (`create/list/delete_session_share`) | Sharing | | Mutating. Out of scope. |

### Capability mapping
- PAPER_SEARCH: `search_papers`. This also covers PubMed and trials.
- SYSTEMATIC_REVIEW: `create_systematic_review`. **Elicit is the only provider for this.**
- DEEP_LITERATURE_SEARCH: `create_report`.
- PAPER_READ: only through the library (import, then `get_library_source_full_text`). That writes to the
  user library, so it should not be a default path.
- EDITORIAL_CHECK: partial, via the `filters.retracted` flag.

### Identifiers
Not observed because of PLAN_BLOCKED. The docs mention Elicit ids and DOI-shaped filters, and PubMed ids are
likely for the PubMed corpus. **Verify once Pro is active.**

### Sync / async, latency
- Search is sync.
- Reports and SRs are async. Poll `links.self` every 30–60 s; states go `processing` → `completed` |
  `failed` | `pausedForInsufficientQuota`. Downloads expire after 7 days.
- Export manifests can be stale, so repoll at least 60 s apart with bounded backoff [doc].

### Plans
Per https://elicit.com/pricing [doc]:

| Plan | Price | API | What it adds |
|---|---|---|---|
| Basic | free | **no** | Limited reports/SR |
| Pro | $49/mo ($588/yr) | yes | SR up to 5,000 papers, extraction up to 135 sources × 20 columns, maxResults 300 |
| Scale | $169/mo | yes | 5× usage, figure extraction, maxResults 500 |
| Enterprise | custom | yes | 40k screening, maxResults 10,000 |

Usage draws on a single credit pool, and optional "extra usage" is billed in USD.

**PLAN_BLOCKED on Basic for every capability.**

### Error / quota signals → state
| Signal | State |
|---|---|
| `{"code":"api_access_denied"}` [obs] | **PLAN_BLOCKED** at the account level. Re-probe daily or after an admin says "upgraded". |
| HTTP 403 (plan ineligible for a feature, e.g. `useFigures`) | PLAN_BLOCKED for that capability only |
| HTTP 402 `insufficient_quota`, or a session in `pausedForInsufficientQuota` | **EXHAUSTED** until `get_usage.periodEnd`. The paused job waits and resumes via `resume_session` / `links.resume`. |
| HTTP 429 (burst limit of 100 req/min per IP; lockout 5 min) | RATE_LIMITED, `retry_at = now + 5 min` |
| HTTP 401 | AUTH_REQUIRED |
| 400 `invalid_request` on `maxResults` | Config error. Clamp to the learned plan cap; not an account state. |
| `get_usage.percentUsed ≥ ~90` | COOLING. Reserve the remainder for SR/DEEP only. |

### ToS notes
Per https://elicit.com/operations/api-terms [doc]. **This is the riskiest provider in scope.**
- "Cannot use the API or its outputs to build or operate a search index, research engine, or similar
  product that competes with Elicit."
- No "aggregat[ing] search results to build … a standalone search index, database, or corpus".
- No reselling, sublicensing or proxying of access.
- Keys must stay confidential.
- Safeguards against runaway loops are required.
- Use of a third-party service must be disclosed.

Mitigations:
- Single-user, self-hosted use only.
- Elicit results live only in a **TTL cache**, never in a persistent evidence corpus. Store handles and
  provenance only.
- Never expose Elicit to other users.
- Budget guards on job creation.
- Flag this for the user's own judgment in the design doc.

### Normalization
- Map `typeTags` to a `study_type` signal.
- Clinical-trial records are a different entity type (NCT id), and dedup must not merge them with papers.

---

## 3. Undermind

### Endpoint / auth
- MCP only: `https://mcp.undermind.ai/mcp` [doc: https://www.undermind.ai/mcp]. **No REST API.**
- Protected-resource metadata: `https://mcp.undermind.ai/.well-known/oauth-protected-resource/mcp` gives
  AS `https://api.undermind.ai/` and scope `mcp` [obs].
- AS metadata [obs]:
  - `authorize=/o/authorize/`, `token=/o/token/`, `revoke=/o/revoke_token/`.
  - PKCE S256.
  - `resource_indicators_supported: true` (RFC 8707).
  - **`client_id_metadata_document_supported: true` and no `registration_endpoint`**: no DCR.
- The engine's upstream OAuth client therefore needs **CIMD**: serve a client-metadata JSON at an HTTPS URL
  (via the Cloudflare Tunnel hostname) and use that URL as the `client_id`. Check whether the mcp-gateway
  upstream client supports CIMD as a *client*. It supports CIMD on the AS side.
- On an unauthenticated POST, the server returns `401` with
  `WWW-Authenticate: Bearer error="invalid_token", … resource_metadata="…"` [obs].

### Model
- Everything is **workspace-scoped**, and `workspace_id` is required on almost every tool.
- Papers are addressed by **cite keys** (`Li24`, `Wan20c`). These are unique within a workspace, not across
  workspaces.
- `get_orientation` returns usage guidance and the connected account email [obs].
- **Side effect:** any paper "referenced in recent MCP tool calls" joins the workspace library, and deep
  searches persist in the workspace. The engine should create one dedicated workspace
  (e.g. `research-engine`) once and use it for everything.

### Tools
| Tool | Purpose | Key params | Output |
|---|---|---|---|
| `search_papers` | Semantic search (title + abstract) | `workspace_id`, `sample_abstract` (2–3 sentences phrased like the target abstract, **not** keywords), `search_type` semantic\|library\|citations\|references, `seed_papers[]` ≤10 cite keys, `year_min/max`, `author_contains`, `journal_contains`, `has_pdf`, `sort_override`, `limit` ≤50, `offset` (offset+limit ≤100 for semantic, ≤200 otherwise), `detail_level` | Text: `[CiteKey] Title (year) / journal / authors – citations – cit./yr / PDF ✓\|X`. **No DOI in the search output** [obs]. |
| `get_paper_info` | Metadata for ≤50 cite keys | `show_doi`, `show_locations`, `detail_level` | Adds `DOI:` lines, or a link when there is no DOI [obs] |
| `lookup_papers_by_metadata` | Resolve titles/DOIs/BibTeX to cite keys | `paper_metadata[]` ≤300 | Cite keys |
| `find_papers_by_author` | Author disambiguation | `request` (natural language) | Cite keys grouped by identity with confidence |
| `launch_deep_search` | Agentic comprehensive review | `goal` (self-contained natural language, ≤30k chars), `name`, `folder_path` | **Async.** Returns immediately. Typically 2–5 min (average about 2.9 min [3p]). "Only a few can run in parallel." Output is a ranked paper list plus a summary stored in the workspace. |
| `inspect_deep_searches` | Poll and read results | `names[]`, `status_only`, `papers_only`, `limit` ≤50, `offset`, `sort_override` | Status, summary, relevant papers with relevance |
| `read_pdfs` | Per-paper sub-agent QA over the full PDF, including figures and tables | `papers[{cite_key, question}]` ≤20, run in parallel | Answers, not raw text. Covers OA PDFs plus user uploads. |
| `get_pdf_download_links` | Temporary PDF URLs | cite keys | URLs; keep them private |
| `display_*`, `suggest_pdf_uploads` | UI cards for the chat client | | Not useful for the engine |
| Workspace/folder/file/star tools (`create_workspace`, `write_file`, `delete_*`, `move_*`, `star_papers`, …) | Organization | | Mutating. Only `create_workspace` is used, once. |

### Capability mapping
- DEEP_LITERATURE_SEARCH: `launch_deep_search`. **This is the primary provider.**
- PAPER_SEARCH: `search_papers` semantic.
- PAPER_RELATED and CITATION_GRAPH (semantic-filtered): `search_papers` with citations/references +
  `seed_papers`.
- PAPER_METADATA: `get_paper_info` / `lookup_papers_by_metadata`.
- PAPER_READ as a QA mode: `read_pdfs`.
- Author search: `find_papers_by_author`.

### Identifiers
- Cite key, workspace-local; DOI via `get_paper_info(show_doi=true)` [obs:
  `Li24 → 10.1093/sleep/zsae006`, `Mar15 → 10.1007/978-3-319-13117-7_108`].
- A search therefore costs **N+1 calls**: the search plus one `get_paper_info` batch of up to 50 keys.
- Cache cite-key→DOI per workspace.

### Latency
Sync tools returned in seconds (not precisely timed). Deep search takes 2–5 minutes, so poll with
`status_only=true` every 15–30 s.

### Plans
- Free: "standard rate limits". Paid Pro gets "10× higher usage limits". Pro is $16/mo annual, Team $15/seat,
  Enterprise custom [doc: https://www.undermind.ai/pricing].
- Numeric caps are **not published**. Free was reported as 3 deep searches/month and Pro as 30 at $20/mo
  [3p: https://tooliverse.ai/tools/undermind, https://theaidude.net/tools/undermind]; treat these as
  unverified.
- MCP is available on Free [doc].

### Error / quota signals → state
| Signal | State |
|---|---|
| Tool result containing `rate_limited`. The server's own guidance says "wait and try again later". | RATE_LIMITED. No Retry-After is known, so back off exponentially from 30 s up to 15 min. |
| Deep-search launch refused for concurrency | COOLING for DEEP_LITERATURE_SEARCH only |
| Deep-search or usage-limit message, not yet observed (expect wording like "limit reached / upgrade") | EXHAUSTED for DEEP_LITERATURE_SEARCH with `reset_at` = the 1st of next month (heuristic). Capture the exact text when first seen. |
| HTTP 401 `invalid_token` (WWW-Authenticate) | AUTH_REQUIRED after a refresh attempt. The server tells clients to re-register, which here means CIMD. |
| `get_orientation` account email ≠ configured account | DISABLED (misbound token) |

### ToS notes
Per https://www.undermind.ai/terms [doc].
- §3.4 bans automated queries "except where you access your own Account, using your own valid credentials,
  through official interfaces or integrations we authorize for programmatic or agent-based access". The MCP
  qualifies.
- §2.1: you may not "resell, repackage, or incorporate the Site or its functionality into any product,
  service, or offering".
- §2.2(c): no building a competitive service.
- §1.2: "Only one person may use an Account".
- A **single-user personal gateway is defensible**. Exposing it to anyone else is not.

### Normalization
- `sample_abstract` needs a query rewrite: the engine should template it from the consumer query, e.g.
  "We investigate {query}. Results show …". No LLM is involved.
- Parse the text output with regex.
- `cit./yr` is a useful normalized citation signal.

---

## 4. Consensus

### Endpoint / auth
- MCP: `https://mcp.consensus.app/mcp`.
  - PRM [obs]: resource `https://mcp.consensus.app`, AS `https://consensus.app`, scopes `search profile`.
  - AS [obs]: `/oauth/authorize/`, `/oauth/token/`, `/oauth/revoke/`, **DCR** `/oauth/register/`, PKCE
    S256, token auth `none|client_secret_basic|client_secret_post`.
  - A **no-account mode** also exists: 3 papers per search, with a 5 rps limit shared across all
    anonymous users.
- REST: `POST https://api.consensus.app/v1/search` with header `x-api-key`. It is **self-serve on every plan,
  including Free**, and shares the monthly pool with MCP. Enterprise uses `Authorization: Bearer`
  [doc: https://docs.consensus.app/api-plans-and-access].
- **Recommendation:** use REST with an API key. It gives structured JSON, `retry-after`, and explicit
  status codes instead of parsing Markdown.

### Tools (MCP)
There is a single tool, `search`:
- Params: `query` (≤500), `year_min/max`, `month_min/max`, `study_types[]` (rct, meta-analysis,
  systematic review, cohort, case-control, … 23 values), `human`, `controlled`, `sample_size_min`,
  `duration_min/max` (days), `sjr_min/max` (quartile), `citation_min`, `open_access`, `medical_mode`
  (about 8M top medical and guideline documents), `exclude_preprints`, `domain` (field codes), `country`,
  `journal_name` (boost), `publisher_name`, `include_full_text_chunks` (**paid**), `page` (0–49, **paid for
  page > 0**), `page_size`.
- Output [obs, Free]: Markdown list of `[n] [Title](https://consensus.app/papers/details/<32-hex>/?utm_source=…)`
  (authors, year, citations, journal) followed by the abstract. It ends with an upsell / usage footer that
  the MCP instructions tell the LLM to repeat verbatim; the engine should strip it and record it as plan
  telemetry.
- Consensus Deep Search / Pro Search (the web product) are **not exposed** over MCP.

### Capability mapping
- PAPER_SEARCH with evidence filters. It is the best provider for "clinical/RCT/meta-analysis" filtering.
- `study_type` / `takeaway` signals, paid plans only.
- Full-text excerpts, paid plans only.
- No read, graph, related or deep capabilities.

### Identifiers
- **Free and no-account plans return no DOI** [obs; doc table: "Study type, takeaway, and DOI in results:
  Pro+"]. The only id is the Consensus paper hash in the URL.
- Resolving Free results means **title + year + first author** → OpenAlex `search` / Crossref
  `query.bibliographic` → DOI.
- Pro and above return a DOI.

### Latency
Sync; a few seconds [obs, not precisely timed].

### Plans
Per https://docs.consensus.app/mcp-plans-and-access [doc]:

| Plan | Papers/search | Calls/mo (max with overage) | Rate limit |
|---|---|---|---|
| No account | 3 | no monthly limit | 5 rps shared |
| Free | 10 | 30 (30) | 1 rps |
| Pro / Teams | ≤300 | 500 (1,000) | 3 rps (MCP) / 1 rps (API) |
| Deep | ≤750 | 2,000 (10,000) | 3 rps (MCP) / 1 rps (API) |
| Enterprise | ≤1,000 | custom | custom |

- Billing counts one call per 100 papers returned, rounded up.
- Overage is $0.05 per call and is off by default.
- Usage resets on the 1st of each month.
- The REST `page_size` defaults to 20 and is capped at the plan limit (Free 20). At most 1,000 results per
  query.

Plan-gated (report PLAN_BLOCKED):
- Pagination.
- `include_full_text_chunks`.
- DOI, study type and takeaway fields. On Free the call still works; the engine should report DEGRADED
  fields.

### Error / quota signals → state
Per the REST docs [doc] and MCP observations [obs]:

| Signal | State |
|---|---|
| `429` body "used all included searches" | **EXHAUSTED** until the 1st of next month, 00:00 UTC |
| `429` "Too many requests" + `retry-after` | RATE_LIMITED until `retry-after`. The MCP instructions say to wait 30 s and batch at most 3 in parallel. |
| `403 feature_not_allowed` | PLAN_BLOCKED for pagination or full text |
| `402` billing past due | DISABLED, needs manual action |
| `401` | AUTH_REQUIRED |
| MCP footer "Upgrade to Consensus Pro to return 20 results…" | Plan = Free fingerprint. Reports DEGRADED capability (no DOI). |

### ToS notes
- Consensus prohibits using outputs to "develop, train, or improve any competing product"
  [doc: https://consensus.app/home/terms-of-service/]. Personal single-user use via a gateway is fine.
  Consensus even documents "behind an AI gateway" setups for Enterprise.
- The verbatim-upsell requirement is an instruction for LLM chat clients, not a contractual term. The
  engine keeps the Consensus URL for provenance and attribution.

### Normalization
- Strip `utm_source` from URLs.
- The Consensus hash serves as `provider_item_id`.
- Map the `study_types` enum to our `study_type` signal.
- `sjr` quartile is a venue-quality signal.

---

## 5. OpenAlex (REST)

- **Endpoint:** `https://api.openalex.org`. Entities: `/works`, `/authors`, `/sources`, `/institutions`,
  `/topics`, … Singletons by W-id or `doi:`, `pmid:`, `pmcid:` [obs].
- **Auth:** an `api_key` param has been **required since 2026-02-13** [doc:
  https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ, https://help.openalex.org/api/authentication/].
  - The free account key gets **$1/day**; without a key, the allowance is $0.10/day [obs:
    `x-ratelimit-limit-usd: 0.1`].
  - Singletons cost $0 [obs].
  - A list or search call cost $0.001, i.e. 10 credits [obs, keyless]. Docs say list calls cost about
    $0.0001. The observed search cost is 10× that, so **full-text search is priced higher than filter
    lists**. Budget roughly 1,000 searches/day on a free key.
  - Hard limit of 100 rps.
  - Paid tiers: Member $5k/yr ($20/day), Member+ $10k/yr, Partner $20k+; prepaid top-ups in $1 increments
    [doc: https://help.openalex.org/access/pricing/].
- **Quota headers [obs]:** `x-ratelimit-limit`, `x-ratelimit-remaining` (credits),
  `x-ratelimit-limit-usd`, `x-ratelimit-remaining-usd`, `x-ratelimit-cost-usd`,
  `x-ratelimit-credits-used`, `x-ratelimit-prepaid-remaining-usd`, `x-ratelimit-reset` (seconds until
  midnight UTC).
  - These make OpenAlex the **best-observable provider**, and the engine can track quota proactively.
  - A 429 with `remaining-usd ≈ 0` → EXHAUSTED until the reset.
  - Otherwise a 429 → RATE_LIMITED (1–2 s).
  - 401/403 → AUTH_REQUIRED.
- **Capabilities:**
  - PAPER_SEARCH: `search=` over title, abstract and full text; `x_query.oql` shows the interpretation.
    Semantic search exists in newer releases.
  - PAPER_METADATA: the primary resolver.
  - CITATION_GRAPH: `filter=cites:W…` and `cited_by` / `referenced_works`.
  - PAPER_RELATED: `related_works`.
  - EDITORIAL_CHECK: `is_retracted` [obs: true for the Wakefield 1998 paper].
  - PAPER_READ: via `open_access.oa_url` / `best_oa_location` to a PDF, fetched through the engine's PDF
    pipeline.
- **Identifiers:** OpenAlex W-id, DOI, PMID, PMCID, MAG in `ids` [obs].
- **ToS:** data is CC0.
- **Latency:** 0.75–0.8 s [obs].

## 6. Crossref (REST)

- **Endpoint:** `https://api.crossref.org/works/{doi}`, `/works?query.bibliographic=…`. Auth: none. Add
  `mailto=` or a User-Agent with an email to enter the polite pool. Metadata Plus uses a Bearer token (paid
  members).
- **Limits** (pools revised 2025-12-01 and 2026-07-21)
  [doc: https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/,
  https://community.crossref.org/t/refining-rest-api-limits-for-improved-stability-and-reliability/16137].
  Observed headers:
  - `public-single`: **5 rps, concurrency 1**.
  - `polite-array` (list/query with mailto): **3 rps, concurrency 3**.
  - Docs: public list 1 rps / conc 1; polite single 10 rps / conc 3.
- **Signals:**
  - `x-rate-limit-limit`, `x-rate-limit-interval`, `x-concurrency-limit`, `x-api-pool` [obs]. Configure
    the client-side limiter from these headers dynamically.
  - 429 → RATE_LIMITED.
  - There are no daily quotas; persistent abuse leads to a block (DISABLED).
- **Capabilities:**
  - PAPER_METADATA: authoritative for DOIs.
  - CITATION_VERIFY: existence plus a bibliographic match through `query.bibliographic`.
  - EDITORIAL_CHECK: **`updated-by[]`** with `type: retraction|correction|expression_of_concern|…` and
    `source: retraction-watch` [obs: Wakefield returns both a retraction and a correction]. Also
    `relation.is-preprint-of` / `has-preprint`.
  - PAPER_SEARCH: weak ranking; use it as a fallback.
  - Reference lists are available when publishers deposit them.
- **Identifiers:** DOI.
- **Latency:** about 1.1 s [obs].
- **ToS:** metadata is essentially open (facts/CC0). Follow the etiquette: cache, use `select=`, use
  cursors, and back off on 429.

## 7. Semantic Scholar (REST)

- **Endpoint:** `https://api.semanticscholar.org/graph/v1`:
  - `/paper/search`, `/paper/search/bulk`, `/paper/search/match`.
  - `POST /paper/batch` (≤500 ids).
  - `/paper/{id}/citations|references`, with `contexts`, `intents` and `isInfluential`.
  - `/snippet/search`: full-text snippets.
  - `/author/*`.

  Plus `recommendations/v1/papers/forpaper/{id}` and `POST /papers` for positive/negative seeds.
  Accepted ids: S2 sha, `CorpusId:`, `DOI:`, `ARXIV:`, `PMID:`, `PMCID:`, `URL:`.
- **Auth:** `x-api-key`, free on request by email. The **introductory limit is 1 RPS** across all
  endpoints and can be raised on request. Without a key, requests share a global pool
  [doc: https://www.semanticscholar.org/product/api]. **In practice the keyless pool is unusable**: the
  2nd keyless request returned `429 TooManyRequestsException` ("apply for a key") [obs].
- **Signals:** 429 (AWS API Gateway, no Retry-After) → RATE_LIMITED with a 1–5 s backoff. With a key,
  enforce a 1 rps token bucket client-side. 403 → AUTH_REQUIRED (bad key).
- **Capabilities:**
  - PAPER_RELATED: the recommendations endpoint is the best free option.
  - CITATION_GRAPH: with citation intents (background, method, result) and influential flags.
  - PAPER_SEARCH.
  - PAPER_METADATA (batch).
  - PAPER_READ (partial): snippet search plus `openAccessPdf`.
  - CITATION_VERIFY (partial): `contexts`.
- **Identifiers:** paperId (40-hex), CorpusId, DOI, PubMed, ArXiv, ACL, DBLP, MAG in `externalIds` [obs].
- **Latency:** about 2.3 s for a single lookup [obs].
- **ToS:** S2 API License Agreement. Attribution required; keys must not be shared
  [doc: https://www.semanticscholar.org/product/api/license].

## 8. arXiv (REST)

- **Endpoint:** `https://export.arxiv.org/api/query?search_query=…&id_list=…&start&max_results` (Atom XML).
  Plain `http://` redirects with a 301 [obs]. PDFs are at `https://arxiv.org/pdf/{id}`, and HTML (where
  available) at `https://arxiv.org/html/{id}`. OAI-PMH can be used for bulk harvesting.
- **Auth:** none.
- **Limits:** **1 request every 3 s, single connection, aggregated across all of our machines**. Circumvention
  is banned [doc: https://info.arxiv.org/help/api/tou.html]. Enforce this with a global lock and token
  bucket; on 503 / `Retry-After` → RATE_LIMITED.
- **Capabilities:**
  - PAPER_SEARCH: fielded Lucene-like queries (`ti:`, `au:`, `abs:`, `cat:`). Relevance ranking is poor.
  - PAPER_METADATA via `id_list`.
  - PAPER_READ: PDF or HTML.
- **Identifiers:** arXiv id + version (`2609.06042v1`) [obs], optional `arxiv:doi` (journal DOI), and the
  DataCite DOI `10.48550/arXiv.{id}`.
- **Latency:** about 0.5 s [obs].
- **ToS:**
  - Metadata is CC0.
  - Do not redistribute PDFs unless the license permits.
  - Do not imply arXiv endorsement.
  - Attribution: "Thank you to arXiv for use of its open access interoperability."

---

## 9. Other academic MCPs visible in this environment (worth adding later?)

| Provider | Endpoint / auth | Tools (observed names) | Notes | Verdict |
|---|---|---|---|---|
| **Lune** | `https://mcp.luneresearch.com`, OAuth; stdio package with a PAT (`@retrograde-labs/lune-mcp-server`) [doc: https://github.com/RetrogradeLabs/lune-mcp-server] | `search_papers` (hybrid BM25 + embeddings + Cohere rerank; `rerank_score` calibrated 0..1, `low_confidence`), `search_papers_many` (1–25 variants fused with RRF; **billed per variant**), `search_related_papers`, `get_paper_fulltext`, `get_paper_citations`, `verify_claims` (returns verbatim quotes), `extract_from_papers`, `gather_evidence`, conferences, research guidance | Full-text corpus of top CS venues (ML/NLP/CV/systems/security). "50 free searches then $0.01/result" [3p] | **Add in phase 2 for CS topics.** Strong CITATION_VERIFY (`verify_claims`), PAPER_READ, and calibrated abstain signals. PAT means no OAuth hassle. |
| **alphaXiv** | `https://api.alphaxiv.org/mcp/v1`, OAuth 2.1 **or API key Bearer** [doc: https://www.alphaxiv.org/docs/mcp] | `discover_papers` (**2 searches per message**), `get_paper_content`, `answer_pdf_queries`, `find_researchers` / `get_researcher_papers`, `read_files_from_github_repository`, library/folder tools | arXiv only (2.5M papers; no biomed). CORS is locked to first-party origins, so it is native clients only, which is fine for the engine. | **Add later**: a good arXiv PAPER_SEARCH/READ layer above raw arXiv. The GitHub reader can back REPO_SEARCH/READ for paper code. Its API key suits headless use. |
| **Valency** | Hosted MCP; endpoint and pricing not found on the web | `search_by_title/abstract/author/venue/category`, `semantic_search_papers`, `find_similar_papers`, `get_citing_papers`, `get_paper_versions`, `filter_papers_with_doi`, `get_author_identity`, `resolve_orcid`, `find_coauthors`, publication/keyword trends, exports (BibTeX/CSV/JSON) | Appears to be a preprint-centric index (arXiv categories, versions, licenses) with author analytics | **Low priority.** Overlaps OpenAlex/S2. Possibly useful for author/ORCID disambiguation and trends. |
| **SciSpace** | Hosted MCP | `search-papers` (natural-language question; 280M index; title/abstract/authors/year/journal/citations), `add-column` | Two tools only. Paid plans are roughly $12–20/mo [3p] | **Skip / low priority.** One more generic PAPER_SEARCH provider with no ids advertised. |
| Also seen | bioRxiv MCP (official-API wrapper), Firecrawl `firecrawl_research_*` (PubMed, bioRxiv, medRxiv, arXiv search, read, related), Wiley Scholar Gateway (`search_wiley_fulltext`, **`getUsageLimit`**) | | | Firecrawl research tools are already in scope via Firecrawl (doc 0x), and so is a biomed full-text path. Wiley is a candidate publisher full-text provider with an explicit usage endpoint. |

---

## 10. Capability × provider matrix

Legend:
- ● primary / strong
- ◐ partial / weak
- $ plan-gated (PLAN_BLOCKED on the current plan)
- — none

| Capability | Scite | Elicit | Undermind | Consensus | OpenAlex | Crossref | S2 | arXiv | Lune | alphaXiv |
|---|---|---|---|---|---|---|---|---|---|---|
| PAPER_SEARCH | ● (Boolean, full text) | ● $ (semantic, PubMed, trials) | ● (semantic, needs `sample_abstract`) | ● (evidence filters; 10/call free) | ● | ◐ | ● | ◐ (arXiv only) | ● (CS) | ● (arXiv) |
| PAPER_METADATA | ● (by DOI) | ◐ $ | ◐ (N+1 calls) | — (no DOI on Free) | ● (resolver) | ● (DOI authority) | ● (batch 500) | ● (arXiv ids) | ◐ | ◐ |
| PAPER_READ | ◐ (OA full text or abstract; 8k pages) | ◐ $ (library import) | ◐ (QA over PDF, not raw text) | ◐ $ (full-text chunks) | ◐ (OA URL → PDF pipeline) | — | ◐ (snippets, OA PDF) | ● (PDF/HTML) | ● | ● |
| PAPER_RELATED | ◐ (graph) | — | ● (citations/references + semantic) | — | ◐ (`related_works`) | — | ● (recommendations) | — | ● | ◐ |
| CITATION_VERIFY | ● (Smart Citations, tallies) | — | ◐ (`read_pdfs` QA) | — | ◐ (existence/metadata) | ● (existence/bibliographic match) | ◐ (contexts) | ◐ | ● (`verify_claims`) | ◐ (`answer_pdf_queries`) |
| CITATION_GRAPH | ● (intent graph, depth 2) | — | ◐ (semantic-filtered) | — | ● | ◐ (deposited references) | ● (intents, influential) | — | ◐ | — |
| EDITORIAL_CHECK | ● (`editorialNotices`, free REST) | ◐ $ (`retracted` filter) | — | — | ● (`is_retracted`) | ● (`updated-by`, Retraction Watch) | — | — | — | — |
| SYSTEMATIC_REVIEW | ◐ (PRISMA logging only) | ● $ (async) | — | — | — | — | — | — | ◐ (`extract_from_papers`) | — |
| DEEP_LITERATURE_SEARCH | — | ● $ (`create_report`, async) | ● (`launch_deep_search`, async 2–5 min) | — (not exposed via MCP) | — | — | — | — | ◐ (`gather_evidence`) | ◐ (`difficulty` 10) |
| REPO_SEARCH (paper code) | — | — | — | — | — | — | — | — | — | ◐ (`read_files_from_github_repository`) |

WEB_SEARCH, WEB_READ, NEWS_SEARCH, SITE_* and DEVELOPER_SEARCH are not covered by any academic provider.

---

## 11. Recommended default routing (per academic capability)

General rules:
- Open REST sources are the default fan-out.
- Hosted MCPs are *budgeted* enrichers. The router needs a per-account monthly budget and reserve (e.g.
  keep 20% for explicit consumer requests).
- Skip any provider whose state is not READY or COOLING.

| Capability | Default chain / fan-out | Notes |
|---|---|---|
| **PAPER_METADATA** (resolver) | OpenAlex singleton (free) → Crossref DOI → S2 `paper/batch` → arXiv `id_list`. For title-only input: OpenAlex `search` + Crossref `query.bibliographic`, then S2 `search/match`. | Canonical id priority: DOI (lowercase) > arXiv id (without version) > PMID > OpenAlex W > S2 CorpusId. Keep an id-crosswalk table. |
| **PAPER_SEARCH** | Fan-out: OpenAlex `search` + S2 `paper/search` + Undermind `search_papers` (semantic; template a `sample_abstract`). Add arXiv or alphaXiv when there is a CS/physics hint. Add Consensus only for evidence-filter queries or when budget allows, and prefer REST with `x-api-key`. Add Scite `search_literature` when budget allows. Add Elicit when Pro. Fuse with RRF, then resolve everything to DOI via PAPER_METADATA. | Consensus Free results must be resolved by title. Undermind requires an N+1 `get_paper_info` call for DOIs. |
| **PAPER_READ** | arXiv HTML/PDF (for arXiv ids) → OpenAlex `best_oa_location` PDF through the engine PDF pipeline → Scite `read_fulltext` (OA; paged 8k) → S2 snippet search (passage mode). Question-directed reads: Undermind `read_pdfs`, or Lune/alphaXiv later. | Always report `fulltext` vs `abstract_only`. Never serve paywalled text. |
| **PAPER_RELATED** | S2 recommendations + OpenAlex `related_works` + Undermind `search_papers(search_type=citations\|references, seed)`, fused. | |
| **CITATION_GRAPH** | OpenAlex (`cites:`, `referenced_works`) + S2 citations/references (intents, `isInfluential`). Use Scite `citation_graph(include_intent)` only when supporting/contrasting intent is requested and quota allows. | Merge edges on canonical DOI. Mark Scite `low_coverage_seeds`. |
| **CITATION_VERIFY** | (1) Existence and metadata match: Crossref + OpenAlex (free). (2) Support signal: Scite public `/tallies/{doi}` (free). (3) Statement-level evidence: Scite `search_literature(dois, term)` (budgeted). Later, Lune `verify_claims` for CS. | Output signals only: exists, metadata_match_score, supporting/contrasting counts, snippets. |
| **EDITORIAL_CHECK** | Run all three in parallel, all free: Crossref `updated-by` + OpenAlex `is_retracted` + Scite public `/papers/{doi}.editorialNotices`. Union them, and track which sources agree. | No MCP quota is used. This should be run automatically as enrichment on top-k results. |
| **DEEP_LITERATURE_SEARCH** (async job) | Undermind `launch_deep_search` in a dedicated workspace (primary; Free available) → Elicit `create_report` (when Pro) → fallback: an engine-native "wide" job (multi-query fan-out across OpenAlex, S2 and Undermind search, plus a citation-expansion round). | Persist the job id and workspace/search name. Poll Undermind every 15–30 s, Elicit every 30–60 s. Enforce a concurrency cap of 1–2 Undermind deep searches. |
| **SYSTEMATIC_REVIEW** (async job) | Elicit `create_systematic_review` only. On the current plan → **PLAN_BLOCKED** with an explicit error. Screening `depth` and `useFigures` must come from the consumer's explicit params and never be defaulted to the costly options. | The Scite `report_citations` PRISMA log is not an SR engine. Do not route to it. |

### Cross-cutting implementation notes
1. **Upstream OAuth client requirements.** Scite, Elicit and Consensus use DCR + PKCE. Undermind uses
   **CIMD + RFC 8707 `resource`**, and the engine must host a client-metadata document. All four use public
   clients (`token_endpoint_auth_method=none`) with refresh tokens. Store the tokens per account, encrypted
   with Fernet.
2. **Tool-result parsing.**
   - Scite and Elicit return JSON-ish text.
   - Consensus and Undermind return Markdown/plain text; write a regex parser per provider.
   - Some quota errors surface as `isError` tool results rather than JSON-RPC errors (Scite, Elicit
     [obs]), so the classifier must inspect both.
3. **Quota classifier** (port research-mcp's 402/429/credit-body detection):
   - Match on per-provider regexes: `monthly MCP usage limit`, `api_access_denied`, `insufficient_quota`,
     `used all included searches`, `rate_limited`, `Too many requests`.
   - Parse reset dates from the text where present (Scite).
   - Otherwise use a calendar rule: the 1st of the month UTC for Scite and Consensus, `periodEnd` for
     Elicit, midnight UTC for OpenAlex.
4. **Probing.** A cheap health probe per provider that does not consume quota:
   - Scite: `GET /mcp/health`.
   - Elicit: `get_usage`.
   - Undermind: `get_orientation`.
   - Consensus: none are free. Rely on passive detection.
   - OpenAlex: headers on any singleton.
5. **Side-effect hygiene.** Never call mutating tools: collections, notes, shares, stars, `write_file`,
   `report_citations`. Undermind reads still add papers to the workspace library, which is why the engine
   uses a dedicated workspace.
6. **Current account inventory (2026-09-29).**
   - Scite: EXHAUSTED until 2026-10-01.
   - Elicit: PLAN_BLOCKED (Basic).
   - Undermind: READY (Free).
   - Consensus: READY (Free; 30 calls/mo; 1 call was used by this probe).
   - OpenAlex: needs a free key.
   - S2: needs a free key.
   - Crossref and arXiv: READY.

## Sources
- Scite:
  - https://docs.scite.ai/mcp/overview
  - https://scite.ai/mcp
  - https://scite.ai/pricing
  - https://scite.ai/terms
  - https://www.researchsolutions.com/scite-mcp-terms
  - https://docs.scite.ai/guides/search
  - https://api.scite.ai/mcp/info
  - https://api.scite.ai/.well-known/oauth-authorization-server
- Elicit:
  - https://docs.elicit.com/
  - https://elicit.com/pricing
  - https://elicit.com/operations/api-terms
  - https://elicit.com/blog/the-elicit-api-and-mcp-powering-autonomous-research-engines
- Undermind:
  - https://www.undermind.ai/mcp
  - https://www.undermind.ai/pricing
  - https://www.undermind.ai/terms
  - https://api.undermind.ai/.well-known/oauth-authorization-server
  - https://casrai.org/guides/undermind-ai
  - https://tooliverse.ai/tools/undermind
- Consensus:
  - https://docs.consensus.app/mcp-plans-and-access
  - https://docs.consensus.app/api-plans-and-access
  - https://github.com/Consensus-NLP/consensus-mcp
  - https://consensus.app/home/terms-of-service/
- OpenAlex:
  - https://help.openalex.org/access/pricing/
  - https://help.openalex.org/api/authentication/
  - https://blog.openalex.org/openalex-api-new-features-and-usage-based-pricing/
  - https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ
- Crossref:
  - https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/
  - https://community.crossref.org/t/refining-rest-api-limits-for-improved-stability-and-reliability/16137
  - https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/
- Semantic Scholar:
  - https://www.semanticscholar.org/product/api
  - https://api.semanticscholar.org/api-docs/
  - https://www.semanticscholar.org/product/api/license
- arXiv:
  - https://info.arxiv.org/help/api/tou.html
- Others:
  - https://github.com/RetrogradeLabs/lune-mcp-server
  - https://www.alphaxiv.org/docs/mcp
  - https://scispace.com/pricing
