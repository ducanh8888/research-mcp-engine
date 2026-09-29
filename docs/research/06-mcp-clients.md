# 06 — MCP client requirements (ChatGPT, Claude.ai, Claude Code, custom agent runtime)

Status: research, as of 2026-09-29. Scope: what our self-hosted, single-user, OAuth-protected Python MCP server
(~17 capability tools, behind Cloudflare Tunnel) must do to work well with each consumer.
Legend: **[V]** verified in an official doc fetched on 2026-09-29 · **[C]** community or issue-tracker evidence only ·
**[U]** could not verify / inferred.

---

## 0. TL;DR (decisions this implies for the design doc)

1. **Protocol:** serve **both eras**, the 2026-07-28 revision (stateless, `server/discover`, no sessions) and
   2025-11-25/2025-06-18 (initialize handshake). ChatGPT, claude.ai and Claude Code's v2 runtime all now probe with
   2026-07-28. MCP Python SDK **v2.x** (stable since 2026-07-28) supports both eras from one server. Never answer
   `server/discover` with only legacy versions. **[V]/[C]**
2. **Don't rely on MCP sessions.** Key all state by OAuth subject plus explicit server-minted handles
   (evidence handles, `job_id`). claude.ai opens a new session for each call, and 2026-07-28 removes sessions entirely. **[V]/[C]**
3. **ChatGPT `search`/`fetch`:** they are **no longer required** for ChatGPT developer mode or chat. They **are
   still required** (fixed shapes) for **deep research** (API deep-research models accept only search/fetch servers)
   and for **company knowledge**. ChatGPT deep research uses custom apps read-only. → Expose `search` + `fetch`
   **as two extra thin adapter tools on the same endpoint** (19 tools total), and optionally add a `/mcp/compat`
   profile that exposes only those two. **[V]**
4. **Sync budget:** keep **p95 ≤ ~25 s and a hard server deadline of ~45 s** for any synchronous tool call. ChatGPT
   has a hard limit of about 60 s. claude.ai documents 240 s but field reports show about 60 s. Claude Code has a 60 s
   first-byte timer. Cloudflare's proxy read timeout is 125 s. Longer work returns
   `{status:"running", job_id}` and the model polls with `get_job`. MCP Tasks (now an extension) has **no evidence
   of support** in ChatGPT, claude.ai or Claude Code, so use it only for our own agent runtime. **[V]/[C]**
5. **Output size:** default compact responses of **≤ ~8k tokens (~30k chars)**, and read/fetch paginated by cursor.
   The limits are: Claude Code **25k tokens** (warning at 10k), claude.ai **~150k chars**, and ChatGPT an undocumented
   "tool response budget" that truncates. **[V]/[C]**
6. **Auth:** OAuth 2.1 + PKCE S256, with **CIMD preferred and DCR as fallback**. Also needed: RFC 9728 PRM,
   RFC 8414 metadata, RFC 9207 `iss`, `offline_access` with refresh-token rotation, a form-urlencoded token endpoint
   that responds in under 10 s, and an exact `resource` match. Allowlist the ChatGPT, claude.ai and Claude Code
   loopback redirects. **[V]**
7. **Every tool:** `title`, `readOnlyHint:true`, `destructiveHint:false`, `openWorldHint:true` (web-facing),
   `outputSchema` + `structuredContent` + a text block, a name ≤64 chars, a description ≤2048 chars with the key facts
   first, and each tool definition well under 5k tokens. **[V]/[C]**

---

## 1. ChatGPT (connectors → "apps" → now "plugins"; developer mode; deep research; company knowledge)

OpenAI renamed the surface again. MCP servers are now attached as **plugins** at `chatgpt.com/plugins`, shared by
ChatGPT and Codex. "Developer-mode apps" are the unpublished/private form. **[V]**
(https://developers.openai.com/plugins/quickstart.md, https://developers.openai.com/api/docs/guides/developer-mode.md)

### 1.1 Are `search`/`fetch` still required?

| Surface | Requirement | Source |
|---|---|---|
| ChatGPT chat, developer mode | **Not required.** "Developer mode does not require `search`/`fetch` tools. Any tools your app exposes (including write actions) are available, subject to confirmation settings." Help Center FAQ: "Are search and fetch tools required…? No. They are no longer required." | [V] developer-mode.md; help.openai.com/…/12584461 |
| ChatGPT **deep research** | Can use custom apps, **read/fetch actions only, not write**. The OpenAI MCP guide: "To work with ChatGPT deep research and company knowledge, your MCP server should implement two read-only tools: `search` and `fetch`." Whether ChatGPT DR can use arbitrary read-only tools in practice: **[U]** (the Help Center says "custom apps… read/fetch only", which is ambiguous). | [V] developers.openai.com/api/docs/mcp; Help Center 12584461 |
| **Company knowledge** (Business/Enterprise/Edu) | "Only apps with search/fetch functionality are included." To be eligible: "implement the standard `search` and `fetch` tool input schemas and mark other read-only tools with `readOnlyHint: true`." Not applicable to a personal account. | [V] plugins/build/mcp-server.md; Help Center |
| **API deep research** (`o3-deep-research`, `o4-mini-deep-research`) | **Hard requirement.** "deep research models require a specialized type of MCP server — one that implements a search and fetch interface… doesn't support tool calls or MCP servers that don't implement this interface." `require_approval` must be `"never"`. | [V] developers.openai.com/api/docs/guides/deep-research.md |
| Agent mode | Does **not** use custom apps. | [V] Help Center 12584461 |

**Exact schemas (verbatim shape, developers.openai.com/api/docs/mcp) [V]:**

`search`: argument is a single query string (`{"query": "..."}`). Returns an object with a single key `results`:

```json
{ "structuredContent": { "results": [ { "id": "doc-1", "title": "...", "url": "..." } ] },
  "content": [ { "type": "text", "text": "{\"results\":[{\"id\":\"doc-1\",\"title\":\"...\",\"url\":\"...\"}]}" } ] }
```

`fetch`: argument is a single string `id`. Returns:

```json
{ "structuredContent": { "id": "doc-1", "title": "...", "text": "full text...", "url": "https://example.com/doc",
                         "metadata": { "source": "vector_store" } },
  "content": [ { "type": "text", "text": "<same object JSON-encoded>" } ] }
```

- Declare an `outputSchema` for both tools. The reference implementation uses pydantic `SearchResult{id,title,url}`,
  `SearchOutput{results}` and `FetchOutput{id,title,text,url,metadata: dict|None}`. **[V]**
- **Citations are created only when `url` is a non-empty string.** Return absolute, user-openable canonical URLs
  and keep internal identifiers in `id`. **[V]**
- Both tools must be read-only (`readOnlyHint: true`). **[V]**
- The legacy example still runs FastMCP with `transport="sse"` and a `/sse/` URL. The current plugin docs require
  **Streamable HTTP at `/mcp`**. The developer-mode page says "Supported MCP protocols: SSE and streaming HTTP." **[V]**

### 1.2 Auth **[V]** (https://developers.openai.com/plugins/build/auth.md)

- OAuth 2.1 per the MCP authorization spec (2025-11-25). The spec also links to 2026-07-28 for `iss` validation. Modes: OAuth, No auth, and
  **Mixed** (initialize and `tools/list` without auth; each tool's auth comes from its `securitySchemes`
  `noauth`/`oauth2`).
- Client identity options: **CIMD (preferred; "ChatGPT prioritizes CIMD when available")**, DCR ("still
  supported"; run once per connection, then reused), or a predefined client (static credentials entered in the UI).
  - CIMD `client_id` = `https://chatgpt.com/oauth/client.json` (when the AS supports RFC 9207 issuer identification),
    otherwise `https://chatgpt.com/oauth/{callback_id}/client.json`. Token auth is `none` or `private_key_jwt`
    (JWKS at `/oauth/jwks.json` on the metadata origin). The document publishes both plural
    `token_endpoint_auth_methods_supported` and legacy singular `token_endpoint_auth_method` (SEP-3149 transition).
  - AS metadata must include `code_challenge_methods_supported: ["S256"]` and
    `client_id_metadata_document_supported: true` (for CIMD) and `token_endpoint_auth_methods_supported`.
    Include `registration_endpoint` if DCR is offered.
- **Redirect URIs:** with RFC 9207 (`authorization_response_iss_parameter_supported: true`, and `iss` returned on every
  response, matching exactly), ChatGPT uses the stable `https://chatgpt.com/connector_platform_oauth_redirect`.
  Otherwise it uses `https://chatgpt.com/connector/oauth/{callback_id}`. Copy the exact value shown on the server's management page.
- PRM at `/.well-known/oauth-protected-resource` (or pointed to by `WWW-Authenticate` on 401). ChatGPT sends `resource`
  on both the authorize and token requests, and the token audience must match it.
- ChatGPT requests advertised OIDC scopes by default. The Help Center says to advertise **`offline_access`** or
  ChatGPT may lose access when the token expires. **[V]** Re-auth may send `id_token_hint`.
- To trigger re-link on a tool error, return `_meta["mcp/www_authenticate"]` with `error` and `error_description`,
  and also declare `securitySchemes`.
- **Not supported:** client_credentials / M2M grants, custom API keys, customer mTLS certs. Optional: verify ChatGPT
  via **OpenAI-managed mTLS** (leaf SAN `mtls.prod.connectors.openai.com`) or the published egress IP ranges.
- Multi-account support is optional: a read-only profile tool with `_meta["openai/profile"]: true`. Not needed for a
  single user.

### 1.3 Transport, limits, behaviour

| Item | Value | Evidence |
|---|---|---|
| Transport | Streamable HTTP at a stable `/mcp` (SSE still accepted in developer mode) | [V] plugins/build/mcp-server.md, developer-mode.md |
| Local/private servers | Not directly. **Secure MCP Tunnel** (`openai/tunnel-client`, outbound long-poll to `api.openai.com`) works for developer mode, not for public submission. A plain public HTTPS URL (Cloudflare Tunnel) also works. | [V] api/docs/guides/secure-mcp-tunnels.md |
| **Tool-call timeout** | **Hard ~60 s** ("Currently the hard limit on any tool call is 1 minute", forum moderator, 2026-04-27). Reported as a "~60 s, 500 error" in 2025-12. ChatGPT **may re-call the tool** if it believes the call failed. Not in official docs. | [C] community.openai.com/t/1379834, /t/1369341 |
| Response size | **Undocumented.** Reports since 2026-06 describe the error "Response output was truncated at a line boundary to fit the tool response budget". ChatGPT then pages through the result via an internal "Read resource" call. Reported workaround: omitting the duplicate JSON text block avoided truncation (OpenAI support didn't confirm it). | [C] community.openai.com/t/1383071 |
| Tool definition size | Error "All tools (including name, description, and input schema) must be less than 5000 tokens" (reported per tool on Free/Pro). One report of a ~19.7k-char description cap. | [C] /t/1371022, /t/1366904 |
| Tool count | **No documented cap.** API guidance: "Aim for fewer than 20 functions available at the start of a turn" (soft). | [V] api/docs/guides/function-calling.md |
| Server instructions | Used. "Keep the most important details in the first 512 characters." | [V] |
| Annotations | `readOnlyHint` respected. "Tools without this hint are treated as write actions" (write actions need confirmation). `destructiveHint`, `openWorldHint` (true for web search, even read-only). | [V] developer-mode.md, plugins/plan/tools.md |
| Structured output | `structuredContent` (model-visible, used for chaining), `content`, and `_meta` (hidden from the model). Declare `outputSchema`, which must match the output. | [V] |
| Progress / tasks | No documentation of rendering `notifications/progress`. MCP Tasks are **not supported** ("not yet supported", 2025-12). ChatGPT **doesn't auto-poll**: the model re-checks only if instructed or asked. | [C] /t/1369341 |
| Metadata refresh | Developer mode has a manual **Refresh**. Published workspace apps are a **frozen snapshot**, and incompatible schema changes break calls. Keep schemas backward-compatible. | [V] Help Center |
| Protocol era | ChatGPT probes `server/discover`. A server that answers it with legacy-only `supportedVersions` stalls setup (ChatGPT doesn't fall back to `initialize`). | [C] github.com/kagura-ai/memory-cloud/issues/1544 (2026-09-18) |

### 1.4 Plan restrictions **[V], partly contradictory**

- developer-mode guide: "Available to **Pro, Plus, Business, Enterprise, and Education** accounts on the web."
- Help Center (updated ~Aug 2026): "Full MCP… **Business, Enterprise/Edu**". "**Pro users can connect MCPs with
  read/fetch permissions in developer mode**." Web only, not mobile. Plus is not mentioned in the FAQ.
  → For a personal plan, assume **read-only tools only**. Our server is entirely read-only, so it fits. Whether Plus
  users get the same access as Pro is **[U]**.
- Business: only admins/owners can use developer mode, and published apps can't be updated (recreate them instead).

---

## 2. Claude.ai custom connectors and Claude Code

### 2.1 Claude.ai (web, Desktop, mobile, Cowork share one connector infrastructure) **[V]**
(https://claude.com/docs/connectors/building/index.md, …/authentication.md)

- **Plans:** custom connectors by URL on Free, Pro, Max, Team and Enterprise. **Free: one custom connector.** On
  Team/Enterprise, an Owner adds it. (claude.com/docs/connectors/custom/add-unlisted.md)
- **Transport:** Streamable HTTP. Legacy HTTP+SSE still supported but being deprecated.
- **Not supported:** resource subscriptions, sampling, "advanced or draft capabilities".
- **Auth types:** `oauth_dcr` and `oauth_cimd` (default). Also `none`, `static_headers` (beta, limited orgs), and
  Anthropic-held or connection-time credentials (by arrangement). No `client_credentials`.
  - CIMD is used **only if** AS metadata has `client_id_metadata_document_supported: true` **and** `"none"` in
    `token_endpoint_auth_methods_supported`. Otherwise Claude falls back to DCR. With DCR, Claude registers **a new
    client on each fresh connection**.
  - **Callback:** `https://claude.ai/api/mcp/auth_callback`. Support docs say it may move to
    `https://claude.com/api/mcp/auth_callback`, so allowlist both (the second is **[C]**).
  - PKCE S256 on every request. Scopes come from the `scope` in the `WWW-Authenticate` of the 401, otherwise from PRM
    `scopes_supported`. `offline_access` is appended if the AS advertises it.
  - Stricter than the spec: a **401 is required** (Claude ignores `WWW-Authenticate` on a 200). **Only the first
    `authorization_servers` entry** is used. **`resource` must equal the URL exactly as the user enters it**, including the path.
  - **Endpoint latency:** discovery, registration and token endpoints get **10 s**, and refresh gets **30 s**.
  - Refresh happens reactively on 401 and proactively up to 5 min before expiry. Return `invalid_grant` for a dead
    refresh token and rotate refresh tokens (public client). The token endpoint must accept
    `application/x-www-form-urlencoded`.
  - Egress: **160.79.104.0/21**. Discovery requests to the AS come from the same range, so a WAF or bot filter in
    front of the AS breaks the flow.
  - **Cross-host 3xx on the MCP URL drops the `Authorization` header**, which fails as "Authorization with the MCP
    server failed". Register the final URL with no redirects. (troubleshooting.md)
- **Limits:** max tool result **~150,000 chars**, above which Claude writes the result to the sandbox file system if
  code execution is active. **Tool call timeout: 240 s per tool call** (doc). **[V]**
  - Field evidence **conflicts**: a 2026-07 probe measured a **~60 s ceiling** from a claude.ai custom connector.
    Progress notifications (claude.ai sends a `progressToken`) **don't reset** the timer. Issue #115 is open. **[C]**
    → Design for 60 s.
- **Progress:** claude.ai web **does not render** `notifications/progress` messages (issue #804, open). **[C]**
- **Sessions:** claude.ai **starts a new MCP session for each tool call** (#583, open). Session-scoped state is lost. **[C]**
- **2026-07-28:** the claude.ai client (`Anthropic/ClaudeAI/1.0.0`) already sends `server/discover` and 2026-07-28
  requests. It **fails on a state-only MRTR `InputRequiredResult`** (shows "Error occurred during tool execution",
  #1027), so don't use MRTR as a long-running mechanism. **[C]**
- **Annotations and review:** every tool should declare `title` + `readOnlyHint`/`destructiveHint`. Read-only tools can
  run without per-call confirmation, and destructive tools always prompt. Names ≤64 chars. No read/write catch-all
  tools. Descriptions must not steer Claude (prompt-injection rules). These rules are directory-review criteria, but
  the permission behaviour applies to custom connectors too. **[V]** (review-criteria.md, mcp.md)
- **structuredContent:** handled for cloud connectors (#563: "cloud connectors unaffected"). Whether the model sees
  `structuredContent` or the text block is **[U]**, so always send both.

### 2.2 Claude Code **[V]** (https://code.claude.com/docs/en/mcp.md, …/env-vars.md)

- **Transports:** `http` (alias `streamable-http`, recommended), `sse` (deprecated, auto-fallback ≥ v2.1.265), `ws`, `stdio`.
  claude.ai connectors also appear in Claude Code when the user is logged in with a claude.ai subscription.
- **Protocol:** the v2 client runtime (TS SDK 2.0) adds **2026-07-28** and probes HTTP servers for it
  (`MCP_PROTOCOL_NEGOTIATION`). It is the default in most sessions. The v2 runtime **fails sign-in on an issuer
  mismatch** (RFC 9207).
- **Auth:** OAuth via `/mcp` or `claude mcp login`. DCR, **CIMD auto-discovered**, or pre-configured `--client-id`
  (+ `--client-secret`). Claude Code's own CIMD is `https://claude.ai/oauth/claude-code-client-metadata`. The
  **redirect is a loopback on an ephemeral port**: `http://localhost:<port>/callback` (also `127.0.0.1`). The AS must
  match both ignoring the port, or the user fixes it with `--callback-port`. Static `headers`/`headersHelper` is an
  alternative for our own use (e.g. a long-lived personal token). `authServerMetadataUrl` overrides discovery.
- **Output limits:** warning at **10,000 tokens**, default max **25,000 tokens** (`MAX_MCP_OUTPUT_TOKENS`). Above the
  max, the result is **saved to a file** and replaced with a path. A server can raise the limit for one tool with
  `_meta["anthropic/maxResultSizeChars"]` (text only, hard ceiling **500,000 chars**).
- **Timeouts:** `MCP_TIMEOUT` startup (30 s). `MCP_TOOL_TIMEOUT` wall clock (default ≈28 h). **For HTTP/SSE/connector
  servers there is a per-request timer to the first response byte = max(60 s, tool timeout, MCP_TIMEOUT)**, so the
  default is effectively **60 s** unless the user sets a per-server `"timeout"` in `.mcp.json`. The **idle timeout is
  5 min** without a response or progress notification (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`). Progress **does not
  extend** the wall-clock limit. After **2 min** a main-conversation call **auto-moves to a background task**
  (≥ v2.1.212) and the result arrives later as a notification. This doesn't apply to subagents or `-p`.
- **Progress:** progress text is rendered (since v2.1.153, per #804's reference). **[C]**
- **Tool search (deferred loading):** on by default. Only tool **names + server instructions** load at start, and
  definitions are fetched on demand. There is no per-server tool cap. `_meta["anthropic/alwaysLoad"]: true` pins a tool, and
  `alwaysLoad` pins a server. **Tool descriptions and server instructions are truncated at 2,048 chars**
  (`CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`). The docs say server instructions "become more useful with tool search"
  and should explain the category of tasks and when to search for these tools.
- **Schema hygiene:** top-level property names 1–64 chars `[A-Za-z0-9_.-]`, and valid JSON Schema 2020-12. Otherwise
  the tool is excluded. Root-level `anyOf/oneOf/allOf` gets flattened, so avoid it.
- `list_changed` is supported (on 2026-07-28 via `subscriptions/listen`). Elicitation is supported. Tasks: **no
  mention**, so assume unsupported. **[U]**

---

## 3. MCP specification state

| Revision | Status | Key points for us |
|---|---|---|
| **2026-07-28** | **Current** (versioning page) | **Stateless**: no `initialize`, and version + client capabilities travel in each request's `_meta`. **`server/discover` is mandatory**. **Sessions and `Mcp-Session-Id` removed** ("servers that need cross-call state use explicit, server-minted handles passed as ordinary tool arguments"). `subscriptions/listen` replaces GET streams. Progress still flows on the request's own response stream. **SSE resumability removed** (a broken stream loses the request, which the client re-issues). **Tasks moved to extension `io.modelcontextprotocol/tasks`** (`tasks/get` polling, `tasks/update`, `tasks/cancel`, server may return a task unsolicited). **MRTR** (`InputRequiredResult`) replaces server-initiated requests. `resultType` is required on results. `Mcp-Method`/`Mcp-Name` headers are required. `ttlMs`/`cacheScope` on list results. Deterministic `tools/list` order (SHOULD). RFC 9207 `iss` (clients MUST validate if present). **DCR deprecated in favour of CIMD**. Roots, Sampling, Logging and HTTP+SSE deprecated. `structuredContent` may be any JSON value. |
| **2025-11-25** | Final | CIMD added (recommended), experimental **tasks** (core), URL-mode elicitation, tool-name guidance (SEP-986), OIDC discovery, incremental scope via `WWW-Authenticate`, JSON Schema 2020-12 default, input validation errors returned as tool errors (not protocol errors). |
| 2025-06-18 | Final | `structuredContent`/`outputSchema`, resource links, elicitation, RFC 9728 PRM. Still referenced by OpenAI for server `instructions`. |

Sources: https://modelcontextprotocol.io/specification/2026-07-28/changelog.md,
…/2025-11-25/changelog.md, …/specification/versioning.md, https://modelcontextprotocol.io/extensions/tasks/overview.md.

**Client support snapshot (2026-09):**

| Feature | ChatGPT | claude.ai | Claude Code | Our agent runtime |
|---|---|---|---|---|
| 2026-07-28 | Yes, probes first and appears modern-first **[C]** | Yes **[C]** | Yes (v2 runtime) **[V]** | Our choice (Python SDK v2) |
| Legacy (initialize) | Yes, via SSE/older setups **[V]** | Yes **[V]** (2025-03-26…2025-11-25 auth specs) | Yes **[V]** | Yes |
| CIMD / DCR | Both, CIMD preferred **[V]** | Both, CIMD conditional **[V]** | Both **[V]** | N/A (own token) |
| Tasks extension | No **[C]** | No ("draft capabilities" unsupported) **[V]** | Not documented **[U]** | Implement if wanted |
| Progress notifications | Not documented **[U]** | Token sent, not rendered, doesn't extend timeout **[C]** | Rendered. Resets idle timer, not wall clock **[V]** | Yes |
| MRTR / elicitation | Elicitation mentioned in plugin docs **[V]**. MRTR **[U]** | MRTR state-only broken **[C]** | Elicitation yes **[V]** | — |
| MCP Apps (UI) | Yes | Yes | n/a | n/a (extension matrix **[V]**) |

The extension support matrix (https://modelcontextprotocol.io/extensions/client-matrix.md) lists **no tasks column
and no client with tasks support**. **[V]**

**SDK:** `mcp` Python SDK **v2.0.0 (2026-07-28)** "supports the 2026-07-28 revision… and serves every earlier
revision from the same server". v2.2.0 was released 2026-09-07. The 1.x line (1.30.0) is maintenance only. v2 moved
`mcp.server.fastmcp` (see the migration guide, and the note in the requirements that mcp-gateway depends on FastMCP 4). **[V]**
(github.com/modelcontextprotocol/python-sdk/releases)

---

## 4. Tool design guidance for LLM consumers

Sources: Anthropic "Writing effective tools for agents" (2025-09-11,
https://www.anthropic.com/engineering/writing-tools-for-agents). Anthropic tool-use best practices
(https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use.md). Anthropic tool search
(…/tool-search-tool.md). OpenAI function calling (https://developers.openai.com/api/docs/guides/function-calling.md).
OpenAI plugin "Define tools" (https://developers.openai.com/plugins/plan/tools.md). **[V]**

- **Count:** Anthropic says selection accuracy "degrades once you exceed 30–50 available tools". OpenAI says "fewer than
  20 functions at the start of a turn" (soft) and fewer than 10 per namespace for tool search. Our 17 + 2 = 19 is inside
  both. Claude Code defers loading anyway.
- **Consolidate vs split (the vendors differ):** Anthropic says consolidate related ops ("action" parameter, e.g.
  `schedule_event`). OpenAI says prefer "focused operations… over one tool with many unrelated modes". Both say to
  **split by permission/safety** (Anthropic directory review **rejects** mixed read/write tools). For us, **one tool per
  capability** is fine. Avoid mode switches that change the output shape radically.
- **Naming:** action-oriented, stable, namespaced prefixes when many tools coexist (`research_search_web`…) ≤64 chars
  (OpenAI/Claude). The prefix helps tool search, since the client prefixes `mcp__server__` anyway in Claude Code.
- **Descriptions:** state what the tool does, **when to use it and when not**, how it differs from sibling tools, and
  limits and prerequisites. Anthropic recommends ≥3–4 sentences. OpenAI uses "Use this when…" phrasing and the "intern
  test". Put the critical content in the **first ~2,000 chars** (Claude Code truncates at 2,048). Server instructions
  carry the cross-tool workflow (search → read by handle → poll job), with the important parts in the **first 512 chars** (ChatGPT).
  Avoid imperative behaviour-steering text (Anthropic prompt-injection review rule).
- **Inputs:** strict schemas, enums over free strings, sensible defaults. Use `input_examples` for complex tools
  (Anthropic API only). Validation errors should be returned as **tool errors with actionable text** (2025-11-25 spec,
  Anthropic review: no bare "Bad Request").
- **Outputs:** "return only high-signal information". Use semantic stable IDs, and include human-readable names
  alongside IDs. Offer a `response_format: "concise"|"detailed"` (Anthropic). Use pagination, range selection,
  filtering and truncation "with sensible default parameter values". When truncating, **say so and say how to get
  more** (cursor, narrower query).
- **Handles:** stable server-minted identifiers are now the spec-endorsed way to carry state (2026-07-28 SEP-2567).
  This matches our evidence handles and job IDs.

---

## 5. Recommendations

### 5.1 `search`/`fetch` alongside one-tool-per-capability

Options considered:

| Option | Pros | Cons |
|---|---|---|
| A. Only capability tools | Cleanest | Loses API deep research and company knowledge, and possibly ChatGPT deep research (**[U]** whether ChatGPT DR uses non-search/fetch tools) |
| **B. Capability tools + `search`/`fetch` adapters on the same endpoint (recommended default)** | Works everywhere with one connector and one OAuth link. 19 tools stays under the soft caps | Two overlapping entry points, which the descriptions must disambiguate |
| C. Profiles by path: `/mcp` (full = B), `/mcp/compat` (search+fetch only), optional `/mcp/core` (capability only) | Gives OpenAI API deep research / Responses `allowed_tools` a minimal surface. Lets a client avoid the overlap | Each path is its own OAuth `resource` (exact URL match, RFC 9728 path-suffixed PRM `/.well-known/oauth-protected-resource/mcp/compat`), which means more connectors to link |
| D. Client sniffing (`clientInfo`/User-Agent) to vary `tools/list` | Invisible to the user | Fragile. 2026-07-28 wants deterministic, cacheable lists (`ttlMs`, `cacheScope`). Against "don't branch on host name" guidance. **Rejected** |

**Recommendation: B, with C as a cheap add-on** (a profile is just a tool filter plus its own PRM document, served by the
same app and AS).

Adapter semantics:
- `search(query: str)` runs the default "balanced research" route (web + scholarly fan-out, fusion, rerank) and returns
  top ~10 `{id: <evidence handle>, title, url: <canonical DOI/arXiv/landing URL>}`. The fixed schema has no room for
  snippets or signals. Optionally include extra fields per result (e.g. `snippet`), which is **[U]** whether extra
  fields are tolerated. Put them in `metadata` on `fetch` instead.
- `fetch(id: str)` resolves the handle and returns `{id, title, text, url, metadata}`. `text` is the best available
  full text/abstract, **capped (~20–30k chars)** with a trailing "truncated; use read_document(id, cursor)…" note.
  `metadata` holds signals, provenance, DOI, year, venue, retraction flag and `next_cursor`.
- Descriptions: `search`/`fetch` say "Generic compatibility search… prefer `search_scholarly`/`search_web`… when you need
  filters or signals". Capability tools say they return the same handles and that `fetch` accepts them.
- `readOnlyHint: true`, `openWorldHint: true`, `idempotentHint: true`. Always a non-empty `url` so ChatGPT cites.

### 5.2 Async / long-running work across clients

Effective sync ceilings: ChatGPT ~60 s (hard) **[C]**. claude.ai 240 s documented but ~60 s observed **[V]/[C]**. Claude Code
60 s to first byte by default, 5 min idle, auto-background after 2 min **[V]**. Cloudflare proxied origin: 125 s read timeout
(non-Enterprise, not configurable) **[V]**. Whether streamed SSE bytes reset Cloudflare's read timer is **[U]**. Tasks
extension: unsupported by the hosted clients.

Pattern ("deadline-bounded call with job handoff"):
1. Every potentially slow tool takes an optional `max_wait_s` (default **40**, clamp 5–50). The engine runs the fan-out
   with that deadline.
2. **Finished in time** → normal result (`status:"complete"`).
3. **Not finished** → return **partial results so far** plus `{status:"running", job_id, progress:{done,total},
   poll_after_s}` (anytime-results: providers that already answered are fused and returned). The model gets something
   useful even if it never polls.
4. `get_job(job_id, max_wait_s=40)` is a read-only tool that **long-polls** up to the deadline and then returns the
   complete or updated partial result. Add `cancel_job` only if needed (it would be a write → confirmation in ChatGPT). Prefer TTL expiry.
5. **Idempotent job IDs** = hash(user, tool, normalized args, cache epoch). A duplicate call (ChatGPT retries after a
   timeout) attaches to the same job and the cache. This justifies `readOnlyHint:true` on the starter tools: no external
   side effects, safe to retry. (OpenAI: "only mark a tool as read-only if it is side-effect-free and safe to retry".)
   Our job row is internal cache state. Note this as a judgment call.
6. Persist jobs in SQLite (survive restart). TTL of hours to days.
7. **Tell the model** in the tool description and the server instructions (first 512 chars): "If `status` is `running`,
   call `get_job` with the `job_id` (wait `poll_after_s`) before answering." ChatGPT doesn't auto-poll. This is the only lever.
8. Emit `notifications/progress` when the client sent a `progressToken`. Claude Code renders it and it resets Claude
   Code's idle timer. It is harmless elsewhere. **Never rely on it to extend timeouts.** Send the first SSE bytes early
   on long streams (Claude Code's first-byte timer).
9. For **our agent runtime** (and later for clients that adopt it): optionally advertise
   `io.modelcontextprotocol/tasks` and return `CreateTaskResult` when the client declares the extension. Back it with
   the same job table. **Do not** use MRTR state-only results for long work (breaks claude.ai, #1027).
10. For Claude Code, the user may set `"timeout": 300000` on our server entry so a single call can run longer. The
    design must not depend on it.

### 5.3 Output-size policy

- Default search/list responses: top-k 8–10 compact items. Target **≤ 8k tokens**, well under Claude Code's 10k warning.
- `read`/`fetch`: page by `cursor`, default page **~20k chars**, and include `next_cursor` + `total_chars`. Set
  `_meta["anthropic/maxResultSizeChars"]` (e.g. 100000) on read tools so larger explicit requests aren't dumped
  to a file in Claude Code.
- Every tool returns `structuredContent` (matching `outputSchema`) **and** one text block. The text should be a compact
  rendering or the JSON when small. For ChatGPT truncation, test whether the duplicate JSON text triggers the "tool
  response budget" (**[C]** workaround: structured-only). Consider a compact textual summary instead of the full JSON
  duplicate. Verify with ChatGPT during implementation.

### 5.4 Auth / deployment checklist (Cloudflare Tunnel, single user)

- AS metadata (`/.well-known/oauth-authorization-server` + OIDC config): `issuer` (exact),
  `authorization_response_iss_parameter_supported: true` (return `iss` always), `code_challenge_methods_supported:
  ["S256"]`, `client_id_metadata_document_supported: true`, `token_endpoint_auth_methods_supported: ["none",
  "private_key_jwt"]` (both needed: Claude requires `none` for CIMD, and ChatGPT may prefer `private_key_jwt`),
  `registration_endpoint` (DCR fallback; set `application_type` handling per SEP-837), `scopes_supported` incl.
  `offline_access`.
- PRM per profile path, with `resource` = exact public URL (incl. `/mcp`), and **only one** `authorization_servers` entry.
- 401 + `WWW-Authenticate: Bearer resource_metadata=…, scope=…` on unauthenticated requests. Never a 200 with a challenge.
- Redirect allowlist: `https://chatgpt.com/connector_platform_oauth_redirect`,
  `https://chatgpt.com/connector/oauth/*` (callback-ID form), `https://claude.ai/api/mcp/auth_callback`,
  `https://claude.com/api/mcp/auth_callback` **[C]**, `http://localhost:*/callback`, `http://127.0.0.1:*/callback`.
  For a single-user deployment, **allowlist CIMD `client_id` URLs** (ChatGPT `https://chatgpt.com/oauth/client.json` and
  `…/oauth/{callback_id}/client.json`, Claude Code `https://claude.ai/oauth/claude-code-client-metadata`, and the
  claude.ai hosted CIMD URL **[U]**, which we couldn't find, so log it on first connect) and gate DCR behind the owner login.
- Token endpoint: form-urlencoded, < 1 s typical (10 s hard for Claude). Refresh < 30 s. Rotate refresh tokens and
  return `invalid_grant` on reuse or expiry. Verify `aud`/`resource` on every MCP request.
- Cloudflare: **no Bot Fight Mode, JS challenge or WAF managed challenge** on `/mcp`, `/.well-known/*`, `/oauth/*`,
  `/register`, `/token` (non-browser clients). **No redirects** on the MCP URL. Disable response buffering for SSE.
  Optionally, WAF-allow Anthropic `160.79.104.0/21` and OpenAI's published connector ranges plus the owner IPs, but
  keep OAuth as the real control. Cloudflare Access in front of `/mcp` is **incompatible** with these clients unless
  bypassed for those paths.
- Alternatives if the public URL becomes a problem: OpenAI Secure MCP Tunnel (developer mode) and Claude MCP tunnels
  (claude.com/docs/connectors/mcp-tunnels; plan availability **[U]**, appears org-oriented).

### 5.5 Per-client quick notes

- **ChatGPT:** enable developer mode, then add a plugin with URL `https://<host>/mcp` and OAuth (CIMD). Mark all tools read-only
  (write tools are unavailable on Pro/Plus, and non-annotated tools count as write and need confirmation). After schema
  changes, click Refresh.
- **Claude.ai:** add a custom connector by URL. Free = 1 connector. The connector syncs into Claude Code automatically
  when the user is logged in with a claude.ai account.
- **Claude Code:** either use the synced claude.ai connector or add it directly: `claude mcp add --transport http research https://<host>/mcp`
  (OAuth loopback). Optional `"timeout"` and `MAX_MCP_OUTPUT_TOKENS`. Write good server instructions for tool search.
- **Custom agent runtime:** Python SDK v2 client over Streamable HTTP with a long-lived personal token (static
  bearer, or OAuth with refresh). It can use the tasks extension and `get_job` directly, with no timeout constraints
  beyond Cloudflare's.

---

## 6. Unverified / open items

- Whether **ChatGPT deep research (in-app)** still needs `search`/`fetch`, or can use any read-only tool. The OpenAI
  doc says it "should" implement them, and the Help Center says custom apps are "read/fetch only". Test once in the UI.
- ChatGPT's exact **tool-call timeout** (60 s from a forum moderator) and **response budget** (undocumented).
  Also whether the duplicate text block matters.
- ChatGPT's 5k-token cap: per tool or aggregate. Reports say per tool.
- **Plus** plan developer-mode access and read-only restriction (the two OpenAI pages conflict).
- claude.ai: **240 s documented vs ~60 s observed** timeout. Whether the model sees `structuredContent` or the text.
  The claude.ai CIMD `client_id` URL. Whether the `claude.com` callback is live.
- Whether Cloudflare's 125 s Proxy Read Timeout is reset by streamed SSE bytes (probably between reads, not total).
- ChatGPT support for `notifications/progress` display, MRTR and the tasks extension.
- Whether extra fields in `search` results (beyond id/title/url) are tolerated by deep research.

## 7. Sources

OpenAI
- https://developers.openai.com/api/docs/mcp — search/fetch schemas, citation rule, CIMD note
- https://developers.openai.com/api/docs/guides/developer-mode.md — eligibility, transports, auth modes, readOnlyHint, "does not require search/fetch"
- https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt — plans, DR read-only, company knowledge, frozen snapshots, offline_access
- https://developers.openai.com/plugins/build/mcp-server.md — tools, annotations, company-knowledge compatibility, Streamable HTTP `/mcp`, instructions 512 chars
- https://developers.openai.com/plugins/build/auth.md — CIMD/DCR, redirect URIs, RFC 9207, mTLS, securitySchemes
- https://developers.openai.com/plugins/plan/tools.md, https://developers.openai.com/plugins/deploy/connect-chatgpt.md, https://developers.openai.com/plugins/deploy/troubleshooting.md
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels.md
- https://developers.openai.com/api/docs/guides/deep-research.md — DR requires search/fetch, require_approval never
- https://developers.openai.com/api/docs/guides/tools-connectors-mcp.md, https://developers.openai.com/api/docs/guides/function-calling.md, https://developers.openai.com/api/docs/guides/tools-tool-search.md
- Community: https://community.openai.com/t/1379834 (60 s), https://community.openai.com/t/1369341 (timeouts, no auto-poll, tasks unsupported), https://community.openai.com/t/1383071 (truncation budget), https://community.openai.com/t/1371022 (5000-token tools), https://community.openai.com/t/1366904 (tool count)
- https://github.com/kagura-ai/memory-cloud/issues/1544 (ChatGPT `server/discover` behaviour)

Anthropic
- https://claude.com/docs/connectors/building/index.md — limits table (150k chars / 240 s; 25k tokens), unsupported features
- https://claude.com/docs/connectors/building/authentication.md — auth types, CIMD conditions, callbacks, latency, refresh, egress IPs
- https://claude.com/docs/connectors/building/troubleshooting.md, …/review-criteria.md, …/mcp.md, …/mcp-apps/troubleshooting.md
- https://claude.com/docs/connectors/custom/add-unlisted.md — plans, Free = 1 connector
- https://code.claude.com/docs/en/mcp.md, https://code.claude.com/docs/en/env-vars.md — Claude Code timeouts, output limits, tool search, OAuth, runtimes
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use.md, https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool.md
- https://www.anthropic.com/engineering/writing-tools-for-agents
- Issues: https://github.com/anthropics/claude-ai-mcp/issues/115, /583, /804, /1027, /563
- https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers (claude.com callback note, via search snippet)

MCP / infra
- https://modelcontextprotocol.io/specification/versioning.md, …/specification/2026-07-28/changelog.md, …/specification/2025-11-25/changelog.md
- https://modelcontextprotocol.io/extensions/tasks/overview.md, https://modelcontextprotocol.io/extensions/client-matrix.md
- https://github.com/modelcontextprotocol/python-sdk/releases (v2.0.0 2026-07-28, v2.2.0 2026-09-07)
- https://developers.cloudflare.com/fundamentals/reference/connection-limits/, https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/
