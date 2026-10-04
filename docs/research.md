# Research Engine — Research Summary

Updated 2026-10-05. Historical investigations below were recorded before the specialist/bridge
pivot. [design.md](design.md), [requirements-notes.md](requirements-notes.md) and
[roadmap.md](roadmap.md) are the active contracts; research recommendations are not a work queue.

## Current conclusions

- Provider abstraction and capability routing remain valid; model providers are not equivalent
  to complementary research providers.
- OmniRoute is now an upstream service for covered search/fetch/API rerank operations, rather
  than a source of control-plane abstractions to port.
- Research Engine owns specialist accounts, results/provenance, exact dedup, RRF and async jobs.
  OmniRoute owns its commodity credentials and account pool.
- Open/REST specialist source exists; hosted-MCP clients/OAuth exist but normalized
  Scite/Elicit/Undermind adapters are missing at the frozen checkpoint.
- The recorded CPU rerank result exists in
  [benchmarks/results/local_cpu.json](../benchmarks/results/local_cpu.json).
  Eight authored cases establish limited local execution evidence, not production quality.
- Frozen web adapters have fixture coverage. Commodity cutover and installed OmniRoute
  account behavior require live checks; source inspection is not endpoint acceptance.

## OmniRoute evidence

Source inspection used
[`fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b`](https://github.com/diegosouzapw/OmniRoute/tree/fc5e2bccd4f70fecf5aab94dfb8136c74ab5a21b):
search registry, fetch handler and search/fetch/rerank API routes. That source's
`package.json` says 3.8.51. No installed 3.8.52 version was verified in this pass.
The operation map and exact provider IDs are in [design §5](design.md#5-provider-scope-and-bridge).

The source supports explicit provider selection and different search/fetch IDs.
Generic fetch does not establish site-map/crawl or specialized paper semantics.
The installed instance's account failover, provider pinning, cache freshness, quota errors
and filter behavior remain P1 validation work.

## Historical evidence index

| Note | Subject | Current use |
|---|---|---|
| [01](research/01-research-mcp.md) | research-mcp source | Compatible HTTP/SSRF/PDF/adapter reuse and attribution |
| [02](research/02-mcp-gateway-fastmcp.md) | gateway/FastMCP | Upstream OAuth/storage patterns; no client OAuth AS or tunnel |
| [03](research/03-omniroute-9router.md) | router/control plane | Historical comparison; current bridge source inspection supersedes porting recommendations |
| [04](research/04-academic-providers.md) | academic providers | Provider investigation; schemas/quotas/plans require current live checks |
| [05](research/05-web-dev-providers.md) | web/developer providers | Operation semantics; covered direct adapter development deferred |
| [06](research/06-mcp-clients.md) | hosted/native clients | Compatibility background; private Claude Code/agentRT is the current target |
| [07](research/07-canonicalize-fusion-signals.md) | evidence/merge/rank | Exact-ID/URL and provenance background; historical advanced algorithms are not requirements |

Prices, account readiness, exhausted-until dates, model IDs, protocol claims and provider
terms in those notes are dated observations. They do not establish current account state.
Verify the locked runtime and actual upstream rather than treating a research snapshot as live configuration.

## Implementation risks to resolve

| Risk | Current action |
|---|---|
| Missing hosted adapters masked by registry | Explicit shipped registration and pending inventory; cleanup C01 |
| Private SDK OAuth overrides | Pin contract, reproduce need and test refresh/concurrency/cancellation; C02–C03 |
| Quota/reset guesses and stale shared eligibility | Known data only, labelled retry estimates and rechecks; C04 |
| Bridge provenance/filter/account mismatch | Explicit-provider mapping plus installed-instance live tests |
| Typed evidence collapsed into one verdict/ranked list | Preserve source assertions; no RRF for verification/graph |
| Fixture PASS confused with live delivery | Record each evidence class and pending blocker in implementing changes |

No historical spending strategy, independence map, weighted fusion, implicit enrichment,
health scheduler, React extraction or public ingress is restored by this summary.
