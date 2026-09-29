# 07 — Evidence model, canonicalization, dedup, fusion, rerank, signals

Status: research + design proposal (no code). Date: 2026-09-29.
Scope: pipeline stages **Normalize → Canonicalize → Dedup → Rank Fusion → Optional Rerank → Enrich (signals) → compact response**,
per `../requirements-notes.md` (quality over latency, stable handles, signals-not-judgements).
Citations in `[n]` refer to §10. Items marked **(probed)** were verified against live APIs / package metadata on 2026-09-29.

---

## 0. TL;DR — recommended defaults

| Stage | Default | Why |
|---|---|---|
| Data model | `ProviderHit` (immutable, 1 per upstream result) → `Evidence` (canonical cluster) with `ids`, `versions`, `provenance[]`, `signals` | Keeps full per-hit provenance; merge is recomputable |
| Handles | Deterministic, typed, lowercase: `doi:` > `arxiv:` > `pmid:` > `pmcid:` > `openalex:` > `s2:` > `gh:` > `url:<b32(sha256(canon_url))[:20]>`; alias table, never delete | Stable across calls; upgrades (url→doi) keep old handle resolvable |
| URL canon | `url-normalize` (RFC 3986 [21]) → own tracker/redirector/AMP/mobile rules (courlan-style list) → drop fragment → sort query → key-only lowercasing | No single lib does all; tested behaviors in §2.1 |
| Scholarly IDs | Own regex extractors (DOI/arXiv/PMID/PMCID/OpenAlex/S2/bioRxiv/SSRN/Zenodo/ACL/OpenReview) + `idutils` for validation | idutils misses arXiv-from-URL, doesn't lowercase DOIs **(probed)** |
| Version linking | Union of: S2 `externalIds`, Crossref `relation` (`is-preprint-of`/`has-preprint`), bioRxiv `/pubs`, arXiv `arxiv:doi`, OpenAlex `locations`; fallback fuzzy title | Every single source is incomplete **(probed)** |
| Dedup | 4 tiers: strong-ID union-find with cannot-link → canonical URL → near-dup text (SimHash 64-bit, Hamming ≤3; MinHash-LSH J≥0.8 for short) → fuzzy title (RapidFuzz `token_sort_ratio≥95` + year±1 + first-author) | Precision first; ambiguous → `possible_duplicate_of` link, not merge |
| Fusion | **Hierarchical weighted RRF, k=60**: inner = per-provider over query variants/accounts (max-rank or RRF), outer = across providers with per-capability weights and **independence groups**; rank window W=20 per list | RRF robust without score calibration [1][5]; hierarchy makes "agreement" meaningful |
| Rerank | Off by default until benchmarked; when on: rerank top-N=40 of fused list, then **2nd-stage RRF(fused, reranked; weights 1:2)** | Keeps agreement effect; normalization-free |
| Local reranker | `gte-reranker-modernbert-base` (149M, Apache-2.0, ONNX, 8k ctx) EN; `bge-reranker-v2-m3` (568M, Apache-2.0) multilingual | Best license×size×quality point for CPU |
| API reranker | Voyage `rerank-2.5`/`-lite` (per-token, 200M free tokens), Cohere Rerank 3.5/4 (per search); Jina API (weights CC-BY-NC) | Pluggable chain with local fallback |
| Signals feeding rank | **Only agreement (implicitly via RRF)**. Everything else is attached, not used, unless consumer passes an explicit policy (e.g. `recency_weight`, `demote_retracted`) | Engine does not judge |

---

## 1. Canonical evidence data model

### 1.1 Layers

```
Call ──< ProviderRun ──< ProviderHit >── (assigned to) ── Evidence ──< Version link >── Evidence
                                               │
                                               ├── ids: IdentifierSet
                                               ├── fields (merged, with field-level source)
                                               ├── provenance: [HitRef]
                                               ├── signals: Signals
                                               └── passages: [Passage]
```

- **ProviderHit** — one raw result from one upstream call, stored verbatim (+ parsed). Immutable. The *only* thing the fusion stage consumes (via ranks).
- **Evidence** — canonical cluster = "one citable thing at one version" (a specific paper version, a web page, a repo, an issue, a news article). All fields derived deterministically from its hits + enrichment records → can be recomputed when merge rules change.
- **Version link** — typed edge between Evidence records that are *different versions of the same work* (preprint ↔ published, arXiv v1 ↔ v3 is **not** a separate Evidence — versions of one arXiv id stay one Evidence with `versions[]`). Preprint and published article are kept as separate Evidence records but **collapsed in responses** (published primary, preprint listed as alternate) unless the consumer asks otherwise. Rationale: content may differ (peer review changes results), so the engine must not silently equate them.

### 1.2 Schema (logical; Pydantic-style)

```python
class Kind(StrEnum):   # coarse, drives field set + dedup rules
    scholarly_work   # journal/conf article, preprint, thesis, book chapter, report
    web_page         # generic page, docs, blog, forum, Q&A, wiki
    news_article
    code_repo        # repository
    code_item        # file/snippet/commit (anchored to repo + sha + path + line range)
    issue            # issue / PR / discussion (subtype)
    dataset
    patent | video | other

class IdentifierSet:           # all normalized, lowercase where case-insensitive
    doi: str | None            # "10.1038/s41586-020-2649-2"
    doi_aliases: list[str]     # e.g. DataCite arXiv DOI 10.48550/arxiv.2005.11401
    arxiv: str | None          # "2005.11401" (versionless); versions in Evidence.versions
    pmid: str | None; pmcid: str | None          # "9500320", "PMC1234567"
    openalex: str | None       # "W2117847125"
    s2_corpus: str | None; s2_sha: str | None
    mag: str | None; dblp: str | None; acl: str | None; openreview: str | None
    ssrn: str | None; isbn13: list[str]; issn_l: str | None
    github_repo_id: int | None # numeric, survives renames
    canonical_url: str | None  # see §2.1
    url_aliases: list[str]     # every normalized URL seen (AMP, mobile, redirector-unwrapped...)

class Evidence:
    handle: str                    # primary stable handle (§3)
    handle_aliases: list[str]
    kind: Kind; subtype: str | None      # e.g. preprint, review, editorial, pr, discussion
    title: Field[str]; authors: Field[list[Author]]   # Author{name, family, orcid?, openalex_id?}
    abstract: Field[str] | None
    container: Field[Venue] | None # journal/conf/site/repo owner; Venue{name, issn_l, openalex_source_id, type}
    published: Field[PartialDate] | None; updated: Field[PartialDate] | None
    language: str | None
    urls: {landing: str, pdf: str | None, oa: str | None}
    versions: list[VersionRef]      # arXiv vN / bioRxiv vN with dates
    related: list[Relation]         # is-preprint-of, has-preprint, is-version-of, retracted-by, corrected-by,
                                    # possible_duplicate_of, syndicated-from, mirrors
    provenance: list[HitRef]        # §1.3
    passages: list[Passage]         # snippets/highlights per provider + fetched best passage
    signals: Signals                # §7
    merge_log: list[MergeDecision]  # which tier merged what, with scores (debuggable, replayable)

class Field[T]:                     # every merged scalar carries its source
    value: T; source: str           # "crossref" | "openalex" | "s2" | "provider:exa" | "html:citation_title" ...
    alternatives: list[tuple[str, T]] | None   # only kept when sources disagree materially
```

Kind-specific extensions (flat, optional):
- `code_repo`: `owner, name, default_branch, license_spdx, stars, forks, pushed_at, archived, fork_of, topics`.
- `code_item`: `repo_handle, commit_sha, path, line_start, line_end, language`.
- `issue`: `repo_handle, number, type(issue|pr|discussion), state, merged, created_at, closed_at, comments, labels`.
- `news_article`: `publisher, byline, section, syndicated_from`.
- `dataset`: `repository (zenodo/figshare/dryad/hf), version, license`.

### 1.3 Provenance per provider hit

```python
class ProviderHit:          # stored; HitRef in Evidence points here
    hit_id: str             # ulid
    call_id; provider: str  # "exa", "openalex", "scite-mcp", ...
    account_ref: str        # opaque account id (never the key)
    capability: str         # "web_search", "scholarly_search", "code_search", "news_search", ...
    upstream_op: str        # "exa.search(type=neural)", "mcp:scite.search_literature"
    query_variant: str      # exact query string sent
    rank: int               # 1-based, within that upstream list
    list_len: int           # returned length (needed for fusion diagnostics)
    raw_score: float | None; score_type: str | None   # "bm25"|"cosine"|"rerank_calibrated"|None
    raw_url: str; raw_ids: dict; title_raw: str; snippet: str | None
    published_raw: str | None
    retrieved_at: datetime; cache: "miss" | "hit" ; latency_ms; cost_units
    payload_ref: str        # pointer to verbatim JSON blob (TTL'd)
```

Compact response exposes a **provenance summary** per Evidence: `[{provider, rank, capability}]` sorted by rank, plus `n_providers`, `n_hits`. Full hits via `get(handle, expand=["provenance"])`.

### 1.4 Compact response item (target ≤ ~120 tokens)

```json
{"handle":"doi:10.1038/s41586-020-2286-9","kind":"scholarly_work","title":"A SARS-CoV-2 protein interaction map reveals targets for drug repurposing",
 "year":2020,"venue":"Nature","snippet":"…","url":"https://doi.org/10.1038/s41586-020-2286-9",
 "alt":[{"handle":"doi:10.1101/2020.03.22.002386","rel":"has-preprint"}],
 "signals":{"agreement":{"providers":4,"eligible":6},"stage":"published","editorial":"none_found",
            "oa":"green","cited_by":{"openalex":4100},"age_days":2300},
 "prov":["openalex#1","s2#2","scite#1","exa#7"]}
```

---

## 2. Canonicalization

Principle: **two strings** per URL — `display_url` (as returned, minus trackers) and `canon_key` (aggressive, used only for identity). Aggressive transforms (https-forcing, `www.` stripping, trailing-slash removal) are *key-only* because they are not semantically guaranteed [21][25].

### 2.1 URL normalization

Library behaviors **(probed 2026-09-29 on `HTTP://WWW.Example.com:80/a/../b/?utm_source=x&b=2&a=1&fbclid=zz&gclid=1#section`)**:

| Lib (license, last release) | Result | Does | Misses |
|---|---|---|---|
| `url-normalize` 3.0.1 (MIT, 2026-09) | `http://www.example.com/b/?utm_source=x&b=2&a=1&fbclid=zz&gclid=1#section` | RFC 3986 §6: case, IDNA2008/UTS46, dot-segments, default port, percent-encoding; allowlist param filtering (per-domain) | trackers by default, sorting, fragments |
| `courlan` 1.4.0 (Apache-2.0, 2026-06) | `http://www.example.com/a/../b/?a=1&b=2#section` | tracker removal, query sort, default port, spam/filter heuristics, lang filters | dot-segments; keeps fragment; `check_url` rejected the URL (strict filters) |
| `w3lib` 2.4.1 (BSD-3, 2026-03) `canonicalize_url` | `http://www.example.com:80/a/../b/?a=1&b=2&fbclid=zz&gclid=1&utm_source=x` | sort query, drop fragment, encoding; `url_query_cleaner` for param allow/deny | trackers, port, dot-segments |
| `yarl` (Apache-2.0) | parsing/building | fast, correct parsing | policy |
| `tldextract` 5.3 (BSD-3) | eTLD+1 via Public Suffix List | registrable domain | — |

**Pipeline (`canon_key(url)`):**
1. Trim, reject non-http(s), parse (yarl). Unwrap **redirectors** statically: `google.com/url?q=`, `bing.com/ck/a?…&u=a1<b64>`, `duckduckgo.com/l/?uddg=`, `l.facebook.com/l.php?u=`, `lnkd.in`, `t.co` (needs HEAD), `news.google.com/rss/articles/…` (needs resolve), `out.reddit.com`, Outlook safelinks (`safelinks.protection.outlook.com/?url=`). Network resolution only through the existing research-mcp URL guard, budgeted, cached.
2. `url_normalize()` (RFC 3986 syntax-based + scheme-based normalization [21]).
3. Remove tracking params: `utm_*`, `gclid, dclid, gbraid, wbraid, fbclid, msclkid, yclid, mc_cid, mc_eid, _hsenc, _hsmi, hsCtaTracking, mkt_tok, igshid, si (youtube share), ref, ref_src, ref_url, spm, scid, cmpid, s_cid, ncid, ocid, sr_share, share, trk, trkCampaign, __twitter_impression, oly_*, vero_*, rb_clickid, _ga, _gl`. Maintain as data file; seed from courlan's list (Apache-2.0). (ClearURLs rules are LGPL-3.0 data — reference only.)
4. Drop fragment, except `#!` (hash-bang SPA) and known fragment-routed hosts.
5. Sort remaining query params (stable), drop empty values.
6. **Key-only rules**: scheme → `https`; host lowercase, strip leading `www.`/`www2.`; mobile hosts `m.`, `mobile.`, `amp.`, `*.m.wikipedia.org → *.wikipedia.org`; strip trailing `/` (not for root); strip `index.html|index.php|default.aspx`.
7. **AMP**: `/amp/` or `/amp` path segment, `?amp=1|?outputType=amp`, `.amp.html`, `amp.` host, Google AMP cache `*.cdn.ampproject.org/c/s/<host>/<path>` and `google.com/amp/s/<host>/<path>` → origin URL. Confirm with `<link rel="canonical">` when page is fetched.
8. Host-specific canonicalizers (small plugin registry): YouTube (`youtu.be/ID`, `/shorts/ID`, `/embed/ID` → `youtube.com/watch?v=ID`, drop `t`, `list`), GitHub (`/tree/<default>`/`.git`/`/blob/<sha>` handling; `github.com/o/r/issues/N` vs `/pull/N`), Reddit (`old.`/`np.`, strip slug after post id), Medium (strip `?source=`), X/Twitter (`x.com`, `twitter.com`, `mobile.twitter.com` → `x.com/i/status/ID`), StackOverflow (`/q/ID`, `/questions/ID/slug` → id), Wikipedia (title normalization `_`/space, `?curid=`), arXiv/DOI/PubMed hosts → handled by §2.2 before URL keys.
9. **Canonical link (`rel=canonical`, `og:url`)** — only when a page is fetched (read/enrich stage). Trust it if same eTLD+1, or target is a known syndication origin; otherwise record as `related: syndicated-from` without merging (canonical tags are author-controlled and are abused/misconfigured; the DUST problem [22] shows URL rules alone are insufficient, so content near-dup (§4.3) is the backstop).

### 2.2 Scholarly identifier extraction & normalization

Run on every hit: provider-supplied ids, URL, PDF URL, and (when fetched) HTML `<meta>` (`citation_doi`, `dc.identifier`, `prism.doi`, `citation_arxiv_id`, `citation_pmid`) via `extruct` (BSD) / lxml.

| ID | Normalized form (key) | Extraction patterns (non-exhaustive) |
|---|---|---|
| DOI | `10.<reg>/<suffix>` **lowercased** (DOIs are case-insensitive [17]); percent-decoded; trailing `.,;:)]}>'"` stripped unless parenthesis balanced | `doi:`, `https?://(dx.)?doi.org/`, `hdl`-style; regex `\b10\.\d{4,9}/[-._;()/:a-z0-9<>\[\]]+` (Crossref's recommended pattern matches ~99.3% of DOIs [18]); publisher paths: `/doi/(abs|full|pdf|epdf|epub)/10.…` (Wiley, T&F, SAGE, ACM, ACS, APS), `link.springer.com/(article|chapter)/10.…`, `nature.com/articles/<s…>` → `10.1038/<id>`, `journals.plos.org/…?id=10.1371/…`, `biorxiv|medrxiv.org/content/10.1101/<d>(v\d+)?(.full|.pdf)?` → DOI + version, `papers.ssrn.com/…abstract_id=N` → `10.2139/ssrn.N`, `zenodo.org/records/N` → `10.5281/zenodo.N`, `aclanthology.org/<id>` → `10.18653/v1/<id>` (verify), `sciencedirect.com/…/pii/<PII>` → needs lookup (Crossref `alternative-id` filter or page meta), IEEE `document/<n>` → page meta. Strip trailing `/full`, `/abstract`, `.pdf`, `/epdf`, `?…` from extracted suffix. |
| DataCite arXiv DOI | `10.48550/arxiv.<id>` → **alias**, primary is `arxiv:<id>` | RA check via `https://doi.org/ra/<doi>` → `DataCite` **(probed)** |
| arXiv | versionless id; `versions=[vN…]`. New `^\d{4}\.\d{4,5}$` (4-digit before 2015), old `^[a-z-]+(\.[a-z]{2})?/\d{7}$` (archive lowercased) | `arxiv.org/(abs|pdf|html|format)/<id>(vN)?(.pdf)?`, `export.arxiv.org`, `ar5iv(.labs).arxiv.org/html/`, `alphaxiv.org/abs/`, `huggingface.co/papers/`, `arXiv:<id>` in text, `10.48550/arXiv.<id>` |
| PMID | digits, no leading zeros | `pubmed.ncbi.nlm.nih.gov/<n>`, `ncbi.nlm.nih.gov/pubmed/<n>`, `europepmc.org/(article|abstract)/MED/<n>` |
| PMCID | `PMC\d+` uppercase | `(pmc.)?ncbi.nlm.nih.gov/(pmc/)?articles/PMC\d+`, `europepmc.org/article/PMC/…` ; PMID↔PMCID↔DOI via NCBI ID Converter API |
| OpenAlex | `W\d+` | `openalex.org/W…`, `api.openalex.org/works/W…` |
| S2 | CorpusId (int) + 40-hex sha | `semanticscholar.org/paper/(<slug>/)?<sha>`, API `CorpusId:` |
| OpenReview / ACL / DBLP / MAG | as issued | `openreview.net/(forum|pdf)?id=`, `aclanthology.org/<id>`, dblp keys, OpenAlex `ids.mag` |

`idutils` 1.7.0 (BSD-3, active; **note: pulls `isbnlib2`, LGPL-3.0**) is useful for scheme validation (ISBN/ISSN/ORCID/Handle/ARK/RRID/etc.), but **(probed)**: `detect_identifier_schemes("https://arxiv.org/abs/2005.11401v4") → ['url']` (no arXiv), `normalize_pid(<arxiv URL>,'arxiv') → 'arXiv:https://…'`, `normalize_doi` keeps case. ⇒ Own extractor module (≈300 LOC + table-driven tests) is the core; idutils optional for validation of long-tail schemes. `python-doi` is GPL-3 → avoid.

Resolution/enrichment clients (all MIT unless noted): `habanero` 2.9 (Crossref; active), `pyalex` 0.21 (OpenAlex; active), `semanticscholar` 0.12 (S2), `arxiv` 4.0 (arXiv API), `metapub` 0.7.5 (Apache-2.0; PubMed/NCBI). Given async fan-out, prefer **thin own `httpx` clients** for OpenAlex/Crossref/S2 (simple REST; need custom quota/429 handling from the account router) and use these libs as reference/tests only.

**API-cost note (probed):** OpenAlex now requires API keys and uses usage-based pricing with $1/day free allowance; a search call reported `cost_usd: 0.001` in `meta` [12]. Crossref REST remains free (polite pool with `mailto`). S2 unauthenticated calls hit 429 quickly → use a key.

### 2.3 Preprint ↔ published linking

No single source is complete. Probed example: bioRxiv `10.1101/2020.03.22.002386` → Crossref `relation = {}`, but bioRxiv `/pubs` API returns `published_doi = 10.1038/s41586-020-2286-9`. OpenAlex returned BERT (NAACL DOI) as one work with one location (the arXiv version not merged). Crossref currently has ~842k records with `is-preprint-of` **(probed)**.

Sources, in precedence order (all recorded in `related[]` with `asserted_by`):
1. **Crossref `relation`**: `is-preprint-of`, `has-preprint`, `is-version-of`, `has-version`, `is-identical-to` (asserted by publisher/preprint server) [13].
2. **Preprint-server APIs**: bioRxiv/medRxiv `api.biorxiv.org/pubs/…` (`published_doi`), Research Square/SSRN via Crossref; Europe PMC preprint links for life sciences.
3. **Semantic Scholar**: a single paper record often carries `externalIds.ArXiv` **and** `externalIds.DOI` (venue DOI) → strong link for CS/ML.
4. **arXiv API** `<arxiv:doi>` / `<arxiv:journal_ref>` (author-supplied; incomplete, sometimes wrong journal).
5. **OpenAlex** `locations[]` with `version ∈ {submittedVersion, acceptedVersion, publishedVersion}` on one work; `type = preprint` vs `article`.
6. **Fallback fuzzy**: preprint vs candidate published work: normalized title `token_sort_ratio ≥ 92`, first-author family name equal (ASCII-folded), ≥50% author overlap, `published_year ∈ [preprint_year, preprint_year+3]` → `related: probable-published-version (asserted_by: engine:fuzzy, score)`. Never auto-merge on fuzzy; response collapse is allowed only for sources 1–5.

---

## 3. Stable handles and merge rules

### 3.1 Handle grammar

```
handle   := scheme ":" body
doi:10.1038/s41586-020-2649-2         (lowercase; '/' kept; URL-safe via percent-encoding at transport only)
arxiv:2005.11401                      (versionless; "arxiv:2005.11401v2" accepted as alias → same Evidence, version pinned in expand)
pmid:9500320  pmcid:PMC1234567  openalex:W2117847125  s2:215416146
isbn:9780262033848
gh:owner/repo                         (+ strong id github_repo_id; renames add alias)
gh:owner/repo/issues/123  gh:owner/repo/pull/45
gh:owner/repo@<sha>:<path>#L10-L40    (code_item; sha makes it immutable)
url:<base32(sha256(canon_key))[:20]>  (lowercase base32, 100 bits; readable form shown alongside)
```

Rules:
- **Deterministic**: computed from identifiers only; never a DB sequence. Same item from any provider/any day → same handle.
- **Primary selection** = highest-priority ID present: `doi` > `arxiv` > `pmid` > `pmcid` > `openalex` > `s2` > `isbn` > `gh` > `url`. Exception: DataCite arXiv DOIs (10.48550) → `arxiv:`.
- **Upgrades**: when enrichment discovers a higher-priority ID (e.g., `url:` page turns out to have `citation_doi`), the primary changes and the old handle becomes an **alias** (stored forever in `handle_alias(alias → primary)`). `get(old_handle)` resolves transparently and returns `handle` = new primary + `resolved_from`.
- **Splits** (a bad merge corrected): old handle keeps pointing to the cluster that contains the hit that originally generated it; log in `merge_log`.
- Version-specific published vs preprint: separate handles (`doi:10.1038/…` and `doi:10.1101/…`), linked.

### 3.2 Field precedence when merging

Per-field ordered source lists (first non-empty wins, others kept in `alternatives` only if they materially differ, e.g. title similarity < 90):

| Field | Precedence | Notes |
|---|---|---|
| title | Crossref > PubMed > OpenAlex > S2 > arXiv > HTML `citation_title` > JSON-LD `headline` > `og:title` > provider title | strip HTML/JATS, LaTeX→unicode (`pylatexenc`, MIT), NFC |
| authors | Crossref (w/ ORCID) > PubMed > OpenAlex (authorships, ORCID, institutions) > S2 > arXiv > HTML `citation_author` | keep ORCID/OpenAlex author id if any source has it |
| abstract | PubMed > S2 > arXiv > Crossref (JATS stripped) > OpenAlex (`abstract_inverted_index` reconstruct) > provider snippet | respect licenses: abstracts stored, not republished in bulk |
| venue | OpenAlex `primary_location.source` > Crossref `container-title` > S2 `publicationVenue` > HTML `citation_journal_title` | |
| published date | Crossref `published-print`/`published-online`/`issued` (earliest of print/online as `published`) > PubMed > OpenAlex `publication_date` > arXiv v1 `published` > JSON-LD/meta > htmldate > provider date | always store source + precision (Y, YM, YMD) |
| updated date | arXiv latest version; JSON-LD `dateModified`; htmldate (`original_date=False`) | |
| urls.oa / pdf | OpenAlex `best_oa_location` / Unpaywall > arXiv PDF > PMC > provider PDF | |
| counts (citations, stars) | **not merged** — kept per source with timestamp | |
| retraction/editorial | **OR** across sources, keep each source's assertion | never let a later "no" overwrite a "yes" |
| snippet | per-provider passages kept; displayed snippet = passage with highest reranker score, else best-ranked provider's snippet | |

---

## 4. Deduplication

Operates on hits → clusters. Precision over recall: a false merge silently hides evidence and corrupts agreement; a missed merge only shows a near-duplicate row. Every merge records `(tier, key/score)`.

### 4.1 Tier 1 — strong identifiers (union-find with cannot-link)
- Build a graph where hits sharing any strong key (`doi`, `arxiv`, `pmid`, `pmcid`, `openalex`, `s2_corpus`, `mag`, `github_repo_id`, `isbn13`) are unioned.
- **Cannot-link**: a cluster may not contain two different DOIs (except alias pairs like DataCite-arXiv ↔ arXiv), two different arXiv ids, two different PMIDs. When S2 reports `{ArXiv: X, DOI: D}`, that is a **version link** (preprint ↔ published), not a union → two clusters + `related`. Conflict in provider data (e.g. wrong DOI in a search index) → keep separate, add `possible_duplicate_of`, log.
- ID→ID mapping fetched lazily (NCBI ID converter for PMID/PMCID/DOI; OpenAlex `ids`) and cached (TTL 30 d).

### 4.2 Tier 2 — canonical URL key
- Same `canon_key` → merge (after Tier 1, so an arXiv abs URL and the arXiv PDF URL already merged via arXiv id).
- URLs that *are* IDs (doi.org, arXiv, PubMed) never reach Tier 2 as plain URLs.

### 4.3 Tier 3 — near-duplicate content (only when text is available)
Use cases: syndicated news (AP/Reuters copies), mirrors/scrapers of docs, GitHub forks' READMEs, blog cross-posts, DUST [22].
- **Long text (≥ ~150 words, fetched/extracted by trafilatura)**: 64-bit **SimHash** [7] over word 3-shingles (weights = tf), near-dup if Hamming ≤ 3 — the Google web-crawl setting of Manku et al. [8]; Henzinger's large-scale comparison found Charikar SimHash more precise than Broder shingling on web pages [9].
- **Short text (titles+snippets, 20–150 words)**: **MinHash** [6] (128 perms) + LSH (`datasketch`, MIT, active; `MinHashLSH(threshold=0.8)`) over char 5-shingles; verify candidates with exact Jaccard ≥ 0.8. Provider snippets of the *same* page are often different windows → do **not** use snippet MinHash to merge different URLs unless titles also match (RapidFuzz ratio ≥ 95). 
- Result of Tier 3 across different domains: **merge** only if same eTLD+1 or canonical link agrees; otherwise `related: syndicated-from` (original = earliest date, or canonical target) and the response collapses copies under the original. This preserves the fact that N outlets published it (signal) without counting it as N independent agreements.
- Implementation: SimHash is ~40 LOC with `xxhash`/`hashlib`; `simhash` PyPI (MIT) unmaintained since 2022 → write own. `datasketch` 2.0 (MIT, 2026-07) for MinHash/LSH.

### 4.4 Tier 4 — fuzzy scholarly metadata (records without shared IDs)
Typical: provider returns title+authors+year without DOI (Consensus/Elicit/web PDFs).
- Normalize title: NFKD + drop combining marks (avoid GPL `unidecode`; use own fold or `anyascii` ISC), casefold, LaTeX→text, remove punctuation/stopword-insensitive whitespace, drop trailing subtitles only for a second-pass comparison.
- Blocking: first 3 content tokens of title + year bucket (±1).
- Match: `rapidfuzz.fuzz.token_sort_ratio ≥ 95` **and** `|year Δ| ≤ 1` **and** first-author family name equal (or Jaro-Winkler ≥ 0.92) → merge; `90–95` or author missing → `possible_duplicate_of` (no merge). Short titles (< 5 tokens: "Attention", "Editorial") require exact title + venue + year.
- Then resolve the merged record's IDs (Crossref `query.bibliographic` / OpenAlex title search) to promote it to Tier 1 when confident (Crossref's own reference-matching evaluation favours a score threshold plus validation rather than raw search top-1 [19]).
- `rapidfuzz` 3.14 (MIT, very active) — fastest option; `jellyfish` (MIT) for Jaro-Winkler if needed (rapidfuzz also has it).

---

## 5. Rank fusion

### 5.1 Evidence from literature
- **RRF** `score(d) = Σ_i 1/(k + r_i(d))` [1]: beat Condorcet fusion, CombMNZ and learned rankers on TREC; *"k = 60 was near-optimal, but the choice was not critical"* [1]. Needs no score calibration — decisive for us because web search APIs return no or incomparable scores.
- **CombSUM/CombMNZ** [2][3]: sum (× number of lists containing d) of **normalized** scores. Lee [3] explained their gains by the observation that relevant docs overlap across systems more than non-relevant ones (the "chorus effect" [4]) — the theoretical basis for treating cross-provider agreement as a relevance signal. Weakness: normalization choice matters (min-max, sum, z-score, rank-based) [5a][5].
- **Montague & Aslam** [5a]: score normalization choice materially changes metasearch results; min-max is sensitive to outliers and to list truncation.
- **Bruch et al., TOIS 2023** [5]: for 2-way lexical+semantic hybrid, a tuned **convex combination (CC)** of normalized scores outperforms RRF in- and out-of-domain, is sample-efficient (few labelled queries), normalization choice is minor if linear; RRF *is* sensitive to k and generalizes poorly when tuned. Caveat: requires meaningful scores from each system — we lack them for most web providers.
- **Chen et al., ECIR 2022** [5b]: RRF more robust than CC zero-shot (the result Bruch contests) → with **no labelled data**, RRF is the safe default; with labels, CC or weighted RRF can be tuned.
- **Weighted RRF** is now mainstream (Elasticsearch `rrf` retriever `weight`, `rank_constant=60`, `rank_window_size` [15]; many 2024–26 RAG papers use per-list weights, e.g. Exp4Fuse's overlap-aware weights [16]).
- **Risk**: fusion can hurt when one component is much weaker (Benham & Culpepper [14]); weights/independence groups mitigate.
- Libraries: **`ranx`** (MIT; `ranx.fuse` implements ~25 fusion methods incl. RRF, CombSUM/MNZ, Borda, ISR, LogISR, PosFuse, ProbFuse, Weighted-RRF, and 5 normalizations, plus `optimize_fusion` grid search [10]). Last release 0.3.21 (2025-08) — slow but alive; use for **offline evaluation/tuning**, implement online RRF in-house (≈50 LOC, avoids numba dependency on hot path).

### 5.2 Design: hierarchical weighted RRF with independence groups

```
for each provider p (for capability c):
    lists_p = all ranked lists from p (query variants, accounts, sub-ops), each truncated to window W
    inner_p(d) = min rank of d across lists_p          # "best rank" — default
                 (alt: RRF over lists_p, k_inner=60, then re-rank)
for each independence group g (providers sharing an index/corpus):
    group_score_g(d) = max_{p∈g} w_p / (k + inner_p(d))  # correlated providers don't add up
score(d) = Σ_g w_g · group_score_g(d)
final ties: best single rank asc → #groups desc → provider priority → handle lexicographic   (deterministic → cacheable)
```

- **Why inner first**: a provider queried with 3 variants (or 2 accounts) must not count 3× in agreement; RAG-fusion-style query expansion is still captured.
- **Independence groups** (config): e.g. `{openalex, crossref}`(bibliographic, not ranking—usually resolvers only), `{s2, elicit?, consensus?}` if they are known to rank over the same S2 corpus, `{brave, *-using-brave}`, `{exa}`, `{tavily}`, `{firecrawl-search}`; unknown → own group. Rationale: agreement among correlated rankers is weaker evidence than among independent ones (chorus effect assumes independent systems [4]). Group membership is an admin-editable policy.
- **Weights**: per `(capability, provider)` default 1.0; suggested starting table: scholarly capability — semantic academic engines (Undermind, Elicit, Consensus, Scite, S2 relevance search) 1.0, OpenAlex search 0.7 (lexical, broad), general web search 0.5; web capability — web providers 1.0, academic 0.5. Tunable in admin policy editor; later fit with `ranx.optimize_fusion` on logged, consumer-labelled queries (Bruch shows few labels suffice [5]).
- **k**: default **60** [1]. Providers return short lists (≈5–25) so rank 1 vs 10 changes `1/(k+r)` by only 13% at k=60 — agreement dominates position. If offline eval shows top-position matters more, use k≈20 (rank1/rank10 ratio 1.45). Expose `k` per capability; evaluate k ∈ {10, 20, 60}.
- **List-length heterogeneity**: truncate every list to **W = 20** (configurable) so a provider returning 100 items does not flood the pool; items beyond W still enter the candidate pool with contribution 0 only if some other list has them. Record `list_len` for diagnostics.
- **Eligibility-aware agreement**: the `agreement` *signal* (§7.1) uses a denominator of providers that *could* have returned the item (e.g., arXiv provider cannot return a Lancet article; GitHub search cannot return papers). RRF itself is unaffected (missing = 0), which slightly favours broad-coverage items; acceptable and documented.
- **Scores from providers**: ignored for fusion by default (heterogeneous: BM25 unbounded, cosine, calibrated rerank, none). Kept in provenance. Optional `combmnz_rank` mode (ranx: rank-normalized CombMNZ) for experiments.

### 5.3 Reranker integration
- Rerank candidate set = top-**N = 40** of fused list (latency budget §6).
- Final order default: **2nd-stage weighted RRF** over two lists: fused list (w=1) and reranked list (w=2), k=60. Keeps agreement's influence, no normalization needed. Alternative once labels exist: CC over `[TM2C2-normalized rerank score, rank-normalized RRF]` with α≈0.7 per Bruch [5].
- Pure rerank order available via policy `final="rerank_only"`.

---

## 6. Reranking

### 6.1 Candidate models (licenses from HF tags, **probed** 2026-09-29; BEIR nDCG@10 as **self-reported** by vendors — not comparable across cards; e.g. mxbai-base-v2 = 55.57 on its own card vs 58.40 on Jina's table)

| Model | Params | License | Ctx | Langs | ONNX on HF | BEIR (source) | CPU fit (top-40 × ~300 tok) — rough, to benchmark |
|---|---|---|---|---|---|---|---|
| `cross-encoder/ms-marco-MiniLM-L6-v2` | 22M | Apache-2.0 | 512 | EN | yes | older baseline | ≪1 s |
| `mixedbread-ai/mxbai-rerank-xsmall-v1` | 70M | Apache-2.0 | 512 | EN | yes | — | ~0.5–1 s |
| **`Alibaba-NLP/gte-reranker-modernbert-base`** | 149M | Apache-2.0 | 8192 | EN | yes | 56.19 (own) | ~1–3 s |
| `BAAI/bge-reranker-base` | 278M | MIT | 512 | EN/ZH | yes | — | ~2–4 s |
| **`BAAI/bge-reranker-v2-m3`** | 568M | Apache-2.0 | 8192 (use 512) | 100+ | community | 56.51 (Jina table) | ~5–12 s fp32; ~2–5 s int8 ONNX |
| `mixedbread-ai/mxbai-rerank-base-v2` | 0.5B (Qwen2.5) | Apache-2.0 | long | multi | no | 55.57 own / 58.40 Jina | ~6–15 s |
| `Qwen/Qwen3-Reranker-0.6B` / `4B` | 0.6B / 4B | Apache-2.0 | 32k | multi | no (seq-cls port exists) | 56.28 / 61.16 (Jina table) | 0.6B ~6–15 s; 4B GPU only |
| `mixedbread-ai/mxbai-rerank-large-v2` | 1.5B | Apache-2.0 | long | multi | no | 57.49 own / 61.44 Jina | GPU |
| `zeroentropy/zerank-2`, `zerank-1-small` | 4B / 1.7B | Apache-2.0 (HF tag) | — | multi | no | — | GPU |
| `jinaai/jina-reranker-v2-base-multilingual` | 278M | **CC-BY-NC-4.0** | 1024 | multi | yes | 57.06 | ~2–4 s |
| `jinaai/jina-reranker-v3` (listwise, 64 docs/pass) | 0.6B | **CC-BY-NC-4.0** | 131k | multi | no | 61.94 (own) [24] | ~5–10 s (single pass) |

CPU numbers are order-of-magnitude estimates for an 8-core desktop and must be measured in a spike (see §9). Latency budget 10–30 s makes even 0.5B models acceptable on CPU at N=40 with 256–384-token passages; a GPU (if present on the home machine) makes 1.5–4B viable.

Evidence: Abdallah et al. (EMNLP Findings 2025, 22 methods/40 variants) — LLM rerankers win on familiar queries, but on **novel (post-training-cutoff) queries** generalization varies and small pointwise cross-encoders give competitive quality at far lower cost [23]. LLM listwise rerankers are much slower [23]. Reasoning (CoT) rerankers do not help and hurt calibration (ICLR 2026) [26] — irrelevant for us (no LLM in engine) but supports cross-encoders.
Research-oriented queries are often "novel" (recent topics) → favour robust cross-encoders + fusion over one expensive reranker.

### 6.2 APIs (pricing snapshot; verify at implementation time)
| API | Model | Pricing | Notes |
|---|---|---|---|
| Voyage | `rerank-2.5`, `rerank-2.5-lite` | $0.05 / $0.02 per 1M tokens; 200M free tokens/account [27] | cheapest per token; instruction-following |
| Cohere | Rerank 3.5 / Rerank 4 (Fast/Pro) | ~$2 per 1k searches (≤100 docs/search) (3.5); Rerank 4 via plans [28] | strong multilingual; per-search pricing favours N≈100 |
| Jina | `jina-reranker-v3`, `m0` | per-token API | API use avoids the NC weight license issue |
| Self-host | any above | $0 | CC-BY-NC weights OK for personal non-commercial use only — flag in admin UI |

### 6.3 Python libraries
| Lib | License / activity | Pros | Cons |
|---|---|---|---|
| `sentence-transformers` 6.1 (`CrossEncoder`) | Apache-2.0, very active (2026-09) | de-facto standard; `backend="onnx"/"openvino"` for cross-encoders; handles most HF rerankers incl. ModernBERT/gte, bge | torch dependency |
| `fastembed` 0.8.1 (`TextCrossEncoder`) | Apache-2.0, very active | ONNX-only, no torch, small; good for CPU | limited model list (MiniLM, bge-reranker-base, jina v1/v2 …) |
| `rerankers` 0.10 (AnswerDotAI) [20] | Apache-2.0; last release 2025-05, commits to 2025-12 | one API over cross-encoders, FlashRank, ColBERT, T5, LLM listwise, Cohere/Jina/Voyage APIs | slower maintenance; extra abstraction; pin version |
| `FlagEmbedding` 1.4 | MIT, active | reference impl for bge-reranker incl. layerwise/LLM variants | heavy |

### 6.4 Pluggable design
```python
class Reranker(Protocol):
    id: str                     # "local:gte-reranker-modernbert-base@onnx-int8" / "api:voyage:rerank-2.5"
    meta: RerankerMeta          # max_tokens, multilingual, calibrated:bool, license, cost_model, device
    async def rerank(self, query: str, docs: list[RerankDoc], top_n: int) -> list[RerankScore]
class RerankDoc: handle: str; text: str       # built by DocTextBuilder
```
- **DocTextBuilder** per kind: papers `title + venue/year + abstract[:N]`; web `title + best passage` (if fetched: split into ~200-word passages, score each, doc score = **MaxP** [29]); code `repo + path + snippet`; issue `title + first comment`.
- Implementations: `NoopReranker`, `STCrossEncoderReranker` (sentence-transformers, torch or ONNX), `FastEmbedReranker`, `RerankersAdapter` (optional), `VoyageReranker`, `CohereReranker`, `JinaReranker`.
- **Chain with failover** reusing the account router (API → local fallback on 429/402/timeout); rerank step has its own time budget (default 8 s); on budget exhaustion, return fused order and set `coverage.rerank = "skipped:timeout"`.
- **Cache** `(reranker_id, sha1(query), sha1(doc_text)) → score` in SQLite (TTL 30 d) — "many small calls" sessions repeat candidates heavily.
- Expose `relevance: {rerank_score, reranker_id, calibrated}` on each item (a relevance estimate, not a quality judgement).

---

## 7. Signals

Rules: every signal = `{value, source(s), as_of, method}`; **unknown ≠ negative** (e.g. `editorial: "none_found"` with `checked: ["crossref","openalex"]` vs `"unknown"` when not checked). Signals never filter results. Ranking effect default **none**, except agreement which is intrinsic to RRF.

| # | Signal | Definition | Source(s) | Cost | Feeds ranking? |
|---|---|---|---|---|---|
| 7.1 | `agreement` | `providers` = # distinct **independence groups** whose (inner) list contains the Evidence within window W; `eligible` = # groups successfully queried whose coverage could include this kind/domain; `ratio = providers/eligible`; `best_rank`, `ranks{provider: r}`; `syndicated_copies` counted separately | fusion stage | free | **Yes (implicitly via RRF)**; ratio not added again |
| 7.2 | `editorial` | enum `retracted | withdrawn | expression_of_concern | corrected | none_found | unknown`, plus `notices[{type, doi, date, source}]` | Crossref `updated-by[]` (types `retraction`, `withdrawal`, `expression_of_concern`, `correction`; `source: "retraction-watch"` or publisher) **(probed: Wakefield 1998 shows RW-sourced retraction + correction)**; OpenAlex `is_retracted`; Scite `editorialNotices`; PubMed pub-types `Retracted Publication` / `Retraction of Publication`. Retraction Watch DB is open via Crossref since 2023 [11] | Crossref free; OpenAlex ≈ credits; Scite via hosted MCP (plan) | No (policy `demote_retracted` optional; never hide) |
| 7.3 | `citations` | per source: `openalex.cited_by_count`, `openalex.fwci`, `openalex.citation_normalized_percentile`, `s2.citationCount`, `s2.influentialCitationCount`, `crossref.is-referenced-by-count`; `as_of` each | OpenAlex / S2 / Crossref | cheap (batched) | No (age & field confounded; popularity ≠ validity) |
| 7.4 | `scite` | tallies `{supporting, contrasting, mentioning, unclassified, citing_publications}` [30] | Scite MCP (`search_literature` / tallies) | plan-gated, rate-limited → top-k only | No |
| 7.5 | `venue` | `{name, type(journal|conference|repository|book-series|other), issn_l, openalex_source_id, publisher, is_in_doaj, is_core, listed_in[]}` — descriptive only, no quality score | OpenAlex `primary_location.source` (probed: `listed_in`, `is_core`, `is_in_doaj` present) | cheap | No |
| 7.6 | `stage` (publication stage) | `published | accepted_manuscript | preprint | preprint_has_published_version | unknown`. Derivation: Crossref `type=posted-content/subtype=preprint`; OpenAlex `type=preprint`, `primary_location.version`, `is_published`, `is_accepted`; S2 `publicationTypes`; arXiv/bioRxiv/SSRN host without venue DOI → preprint. **Named "stage", not "peer_reviewed"**: publication in a venue does not guarantee peer review (editorials, some workshops) — consumer decides | Crossref / OpenAlex / S2 | cheap | No |
| 7.7 | `oa` | `{status: diamond|gold|hybrid|bronze|green|closed, is_oa, oa_url, pdf_url, license, version}` | OpenAlex `open_access` + `best_oa_location` (Unpaywall data) [12][31] | cheap | No |
| 7.8 | `dates` (web) | `{published, modified, source: jsonld|meta(article:published_time)|htmldate|url_path|provider:<name>|sitemap, confidence}`; confidence high if ≥2 independent sources agree ±2 d | provider fields (Exa `publishedDate`, Tavily news `published_date`, Brave `page_age`), JSON-LD via `extruct`, `htmldate` [32] (`find_date(original_date=True)` and `False` for modified), `trafilatura` metadata [33] | needs page fetch → top-k only / on `get` | No |
| 7.9 | `source_type` + `domain` | `domain = eTLD+1` (tldextract/PSL); `source_type ∈ {scholarly_publisher, preprint_server, scholarly_index, repository_institutional, gov, intl_org, edu, news, reference_wiki, vendor_docs, code_host, forum_qa, blog, social, video, dataset_repo, patent, unknown}`; `method ∈ {domain_list, tld_rule, markup(schema.org @type NewsArticle/ScholarlyArticle/BlogPosting; citation_* meta), path_rule}` | curated YAML lists (seed: OpenAlex source homepages, DOAJ, ROR, preprint server list), `.gov/.mil/.edu/.ac.*` TLD rules | free | No |
| 7.10 | `freshness` | `age_days` from best `published` (or `modified` if more recent and flagged); code: `pushed_at`, latest release; issue: `updated_at`, `state`; `retrieved_at` for all | derived | free | No (optional `recency_weight` only when consumer passes it) |
| 7.11 | `code` | `{stars, forks, archived, license_spdx, pushed_at, open_issues}`, issue `{state, merged, comments}` | GitHub API | cheap | No |
| 7.12 | `versions` | `{arxiv_versions, latest_version_date, has_published_version, published_handle}` | §2.3 | cheap | No |
| 7.13 | `coverage` (call-level report) | see below | engine | free | — |

**Coverage / gaps report** (per call, compact by default, expandable):
```json
{"providers":{"attempted":9,"ok":7,"failed":[{"p":"elicit","err":"RATE_LIMITED"},{"p":"undermind","err":"TIMEOUT"}]},
 "per_provider":{"exa":{"hits":20,"unique":6},"openalex":{"hits":25,"unique":9}},
 "overlap":{"pairs_jaccard_top20":{"exa~tavily":0.41,"s2~openalex":0.22}},
 "items":{"total_clusters":83,"single_source":51,"no_strong_id":17,"collapsed_versions":4,"syndicated":3},
 "enrichment":{"editorial_checked":10,"editorial_unknown":0,"dates_extracted":6},
 "facets":{"kind":{"scholarly_work":7,"web_page":3},"stage":{"preprint":3,"published":4},"year":{"2024":2,"2025":5}},
 "gaps":["no biomedical provider queried for capability scholarly_search","2/9 providers failed; agreement denominators reduced"],
 "rerank":"local:gte-reranker-modernbert-base, 40 docs, 2.1s"}
```
`gaps` are rule-generated facts (which eligible providers/capabilities were missing or failed), never advice.

**Enrichment budget**: signals needing extra calls (7.2–7.8, 7.11) run **only for the returned top-k (~10) + expand requests**, batched (OpenAlex `filter=doi:a|b|c`, S2 `/paper/batch`, Crossref per-DOI with polite pool), cached by handle (TTL: editorial 7 d, citations 7 d, OA 30 d, venue 90 d, web dates 30 d).

---

## 8. Stage ordering & where things run

1. **Normalize** each ProviderHit (parse kind, ids from payload, dates, text cleanup).
2. **Canonicalize**: ID extraction (§2.2) → URL canon (§2.1, no network) → handle candidate.
3. **Dedup** tiers 1–2 (+4 for scholarly without IDs) — pure CPU, all hits.
4. **Fusion** (§5) over clusters → fused list.
5. **Rerank** top-N (optional) (§6).
6. **Dedup tier 3** + canonical-link checks for top-N items where text was fetched (reuses fetch done for rerank passages / dates).
7. **Enrich** top-k signals (§7), version collapse (§2.3), handle upgrade (§3.1).
8. Compact response + coverage report; persist hits/evidence/aliases; cache.

---

## 9. Open questions / spikes before implementation

1. **Reranker CPU benchmark** on the home machine: gte-modernbert (ONNX int8), bge-v2-m3 (int8), mxbai-base-v2, Qwen3-0.6B at N ∈ {20, 40, 60}, 256/384 tokens → pick default & N.
2. **Fusion eval set**: log 50–100 real research queries, label top-20 (quick binary), run `ranx` comparisons: RRF k∈{10,20,60} vs weighted vs CombMNZ(rank-norm) vs with/without independence groups; with/without rerank.
3. **Provider independence map**: verify which providers share corpora (S2-based engines; Brave-backed APIs).
4. **OpenAlex cost model** under new pricing (singleton vs list vs search costs) → decide OpenAlex vs Crossref/S2 as primary enrichment.
5. **Scite access** via hosted MCP only (tallies & notices): rate limits under single-user plan.
6. Tracker-param list and host canonicalizers: build fixture corpus (~500 URLs) from real provider outputs.

---

## 10. References

[1] G. V. Cormack, C. L. A. Clarke, S. Büttcher. *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods.* SIGIR 2009. https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf
[2] E. A. Fox, J. A. Shaw. *Combination of Multiple Searches.* TREC-2, 1994 (CombSUM/CombMNZ).
[3] J. H. Lee. *Analyses of Multiple Evidence Combination.* SIGIR 1997.
[4] C. C. Vogt, G. W. Cottrell. *Fusion via a linear combination of scores.* Information Retrieval 1(3), 1999 (chorus / skimming / dark-horse effects).
[5] S. Bruch, S. Gai, A. Ingber. *An Analysis of Fusion Functions for Hybrid Retrieval.* ACM TOIS 42(1), 2023. https://doi.org/10.1145/3596512 (arXiv:2210.11934)
[5a] M. Montague, J. A. Aslam. *Relevance Score Normalization for Metasearch.* CIKM 2001.
[5b] T. Chen, M. Zhang, J. Lu, M. Bendersky, M. Najork. *Out-of-Domain Semantics to the Rescue! Zero-Shot Hybrid Retrieval Models.* ECIR 2022.
[6] A. Z. Broder. *On the resemblance and containment of documents.* SEQUENCES 1997 (MinHash / shingling).
[7] M. S. Charikar. *Similarity estimation techniques from rounding algorithms.* STOC 2002 (SimHash).
[8] G. S. Manku, A. Jain, A. Das Sarma. *Detecting Near-Duplicates for Web Crawling.* WWW 2007 (64-bit SimHash, k=3).
[9] M. Henzinger. *Finding near-duplicate web pages: a large-scale evaluation of algorithms.* SIGIR 2006.
[10] E. Bassani, L. Romelli. *ranx.fuse: A Python Library for Metasearch.* CIKM 2022. https://doi.org/10.1145/3511808.3557207 ; https://github.com/AmenRa/ranx
[11] Crossref. *Crossref acquires Retraction Watch database and opens it* (Sept 2023); REST API `updated-by` with `source: retraction-watch` — probed via `api.crossref.org/works/10.1016/S0140-6736(97)11096-0`. Docs: https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/
[12] J. Priem, H. Piwowar, R. Orr. *OpenAlex: A fully-open index of scholarly works, authors, venues, institutions, and concepts.* arXiv:2205.01833 (2022). Pricing/auth: https://help.openalex.org/access/pricing/ , https://blog.openalex.org/openalex-api-new-features-and-usage-based-pricing/
[13] Crossref. *Version control, corrections, and retractions* / relations (`is-preprint-of`, `has-preprint`). https://www.crossref.org/documentation/principles-practices/best-practices/versioning/
[14] R. Benham, J. S. Culpepper. *Risk-Reward Trade-offs in Rank Fusion.* ADCS 2017.
[15] Elastic. *RRF retriever* (weights, `rank_constant`, `rank_window_size`). https://www.elastic.co/docs/reference/elasticsearch/rest-apis/retrievers/rrf-retriever
[16] L. Liu, M. Zhang. *Exp4Fuse: A Rank Fusion Framework for Enhanced Sparse Retrieval using LLM-based Query Expansion.* ACL 2025 (weighted, overlap-aware RRF, k=60).
[17] DOI Foundation. *DOI Handbook* §2.4 (DOI names are case-insensitive). https://www.doi.org/the-identifier/resources/handbook
[18] A. Gilmartin (Crossref). *DOIs and matching regular expressions* (2015). https://www.crossref.org/blog/dois-and-matching-regular-expressions/
[19] D. Tkaczyk (Crossref). *Reference matching: for real this time* (2018/2019) — evaluation of search-based matching with thresholds. https://www.crossref.org/blog/reference-matching-for-real-this-time/
[20] B. Clavié. *rerankers: A Lightweight Python Library to Unify Ranking Methods.* arXiv:2408.17344 (2024). https://github.com/AnswerDotAI/rerankers
[21] T. Berners-Lee, R. Fielding, L. Masinter. *RFC 3986 URI Generic Syntax*, §6 Normalization and Comparison. https://www.rfc-editor.org/rfc/rfc3986#section-6
[22] Z. Bar-Yossef, I. Keidar, U. Schonfeld. *Do Not Crawl in the DUST: Different URLs with Similar Text.* WWW 2007 / ACM TWEB 2009.
[23] A. Abdallah, B. Piryani, J. Mozafari, M. Ali, A. Jatowt. *How Good are LLM-based Rerankers? An Empirical Analysis of State-of-the-Art Reranking Models.* Findings of EMNLP 2025. arXiv:2508.16757
[24] F. Wang et al. (Jina AI). *jina-reranker-v3: Last but Not Late Interaction for Listwise Document Reranking.* arXiv:2509.25085 (2025). Model card BEIR table: https://huggingface.co/jinaai/jina-reranker-v3
[25] courlan (A. Barbaresi), url-normalize, w3lib documentation; behaviors probed locally (§2.1). https://github.com/adbar/courlan · https://github.com/niksite/url-normalize · https://github.com/scrapy/w3lib
[26] X. Lu et al. *Rethinking Reasoning in Document Ranking: Why Chain-of-Thought Falls Short.* ICLR 2026.
[27] Voyage AI pricing. https://docs.voyageai.com/docs/pricing
[28] Cohere pricing. https://cohere.com/pricing
[29] Z. Dai, J. Callan. *Deeper Text Understanding for IR with Contextual Neural Language Modeling.* SIGIR 2019 (MaxP passage aggregation).
[30] J. M. Nicholson et al. *scite: A smart citation index that displays the context of citations and classifies their intent using deep learning.* Quantitative Science Studies 2(3), 2021. https://doi.org/10.1162/qss_a_00146
[31] H. Piwowar et al. *The state of OA: a large-scale analysis of the prevalence and impact of Open Access articles.* PeerJ 6:e4375, 2018 (Unpaywall OA taxonomy).
[32] A. Barbaresi. *htmldate: A Python package to extract publication dates from web pages.* JOSS 5(51):2439, 2020. https://github.com/adbar/htmldate (Apache-2.0, v1.10, 2026-06)
[33] A. Barbaresi. *Trafilatura: A Web Scraping Library and Command-Line Tool for Text Discovery and Extraction.* ACL-IJCNLP 2021 System Demos. (Apache-2.0 since v1.8; v2.2, 2026-07)
[34] J. Kalra et al. *MoR: Better Handling Diverse Queries with a Mixture of Sparse, Dense, and Human Retrievers.* EMNLP 2025 (multi-retriever combination; links routing to metasearch/fusion literature).

### Library license/maintenance snapshot (PyPI/GitHub, probed 2026-09-29)

| Package | Version (date) | License | Use |
|---|---|---|---|
| url-normalize | 3.0.1 (2026-09-22) | MIT | RFC URL normalization |
| courlan | 1.4.0 (2026-06-01) | Apache-2.0 (deps: `tld` MPL/GPL/LGPL tri-license, babel) | tracker list, filters |
| w3lib | 2.4.1 (2026-03-20) | BSD-3 | query cleaner, canonicalize |
| tldextract | 5.3.2 (2026-08-08) | BSD-3 | eTLD+1 |
| yarl | 1.25.1 (2026-09-15) | Apache-2.0 | URL parsing |
| idutils | 1.7.0 (2026-07-16) | BSD-3 (dep isbnlib2 LGPL-3.0) | PID validation (optional) |
| habanero | 2.9.2 (2026-06-17) | MIT | Crossref client (reference) |
| pyalex | 0.21 (2026-02-23) | MIT | OpenAlex client (reference) |
| semanticscholar | 0.12.0 (2026-03-29) | MIT | S2 client (reference) |
| arxiv | 4.0.1 (2026-07-31) | MIT | arXiv API |
| metapub | 0.7.5 (2026-09-24) | Apache-2.0 | PubMed/NCBI |
| extruct | 0.18.0 (2024-11-08) | BSD | JSON-LD/microdata/meta |
| pylatexenc | 2.11 (2026-07-25) | MIT | LaTeX→unicode in titles |
| rapidfuzz | 3.14.6 (2026-08-30) | MIT | fuzzy title/author |
| datasketch | 2.0.0 (2026-07-05) | MIT | MinHash/LSH |
| simhash | 2.1.2 (2022-03-03) | MIT | **unmaintained → own impl** |
| htmldate | 1.10.0 (2026-06-01) | Apache-2.0 | web dates |
| trafilatura | 2.2.0 (2026-07-31) | Apache-2.0 | main-text + metadata |
| ranx | 0.3.21 (2025-08-07) | MIT | offline fusion eval/tuning |
| sentence-transformers | 6.1.0 (2026-09-18) | Apache-2.0 | CrossEncoder (torch/ONNX/OpenVINO) |
| fastembed | 0.8.1 (2026-09-22) | Apache-2.0 | ONNX cross-encoders, no torch |
| rerankers | 0.10.0 (2025-05-22) | Apache-2.0 | unified reranker API (optional) |
| FlagEmbedding | 1.4.2 (2026-08-24) | MIT | bge reference |
| unidecode / python-doi | — | GPL | **avoid** |
