# Architecture

Research MCP is one FastAPI process hosting FastMCP Streamable HTTP (`/mcp`) and SQLAdmin (`/admin`). An MCP client calls one of nine workflow tools with its Bearer token; a thin dispatch layer maps it to one internal capability, and the token authorizes the client and is never passed to an upstream provider. The admin dashboard uses a separate username/password session and CSRF protection. `/health` is the liveness endpoint.

```text
MCP client → auth → capability validation → saved route → provider calls
                                                 ├─ explicit OmniRoute operations
                                                 ├─ direct research adapters
                                                 └─ local readers
                     search hits → normalize → exact dedup → RRF → optional rerank
                     other records → capability-specific aggregation
                     output → coverage, attribution, handle / job reference
```

A YAML bootstrap file configures the listener, origin checks, admin hash, and secret file locations. SQLite stores provider catalog, operator routes, accounts, hashed MCP client tokens, requests, attempts, persistent handles, caches, and jobs. Account secrets are encrypted with the stable key in the private runtime directory. SQLite WAL and blob files require one process/writer; this is not a distributed service.

Search-like capabilities fan out to all enabled, configured, independent routed providers under one bounded request deadline. A source error yields partial coverage if another succeeds. `web_read` and `paper_read` try sources in order for the same target; metadata resolves only unresolved IDs with later providers; async operations start one recoverable job. Verification, citation graphs, editorial checks, and site maps aggregate source records without RRF.

Handles let clients read a search pointer after a restart. Source documents are paginated; jobs persist ownership and upstream references for polling and restart recovery where the upstream supports it. Complete query results can be cached; reduced-coverage searches are not cached as complete. `fresh=true` skips engine cache, but does not promise an upstream cache bypass where unsupported.

The engine selects research capabilities, not the meaning or truth of evidence. It does not contain an LLM judge, agent planner, or autonomous campaign/scheduler. For concrete inputs and tool names, see the [README](../README.md#mcp-tools), [providers](providers.md), and [routing and fusion](routing-and-fusion.md).
