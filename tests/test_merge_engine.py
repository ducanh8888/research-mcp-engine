from dataclasses import dataclass

import pytest

from research_engine.merge import canonical_handle, merge_hits, normalize_ids, normalize_url, url_key
from research_engine.merge.canonical import url_handle


def hit(provider="exa", rank=1, url="https://example.org/paper", ids=None, **fields):
    return {"provider": provider, "rank": rank, "url": url, "ids": ids or {}, **fields}


def test_url_normalization_removes_only_known_trackers():
    value = normalize_url("http://EXAMPLE.org:80/a/../b?utm_source=x&b=2&a=1#fragment")
    assert value == "http://example.org/b?a=1&b=2"
    assert url_key("http://www.example.org/paper/") == url_key("https://example.org/paper")
    assert url_key("https://example.org/paper?id=1") != url_key("https://example.org/paper?id=2")
    assert normalize_url("https://example.org/?ref=meaningful") == "https://example.org/?ref=meaningful"
    assert normalize_url("https://user:password@example.org/paper") == ""
    assert normalize_url("file:///etc/passwd") == ""


@pytest.mark.parametrize(("ids", "url", "expected"), [
    ({"DOI": "https://doi.org/10.1000/Foo"}, None, {"doi": "10.1000/foo"}),
    ({"arxiv": "arXiv:2301.12345v3"}, None, {"arxiv": "2301.12345"}),
    ({}, "https://arxiv.org/pdf/hep-th/9901001v2.pdf", {"arxiv": "hep-th/9901001"}),
    ({}, "https://pubmed.ncbi.nlm.nih.gov/000123/", {"pmid": "123"}),
    ({}, "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/", {"pmcid": "PMC123"}),
    ({}, "https://openalex.org/W123", {"openalex": "W123"}),
    ({"pmcid": "pmc000123"}, None, {"pmcid": "PMC123"}),
    ({}, "https://www.github.com/OWNER/Repo.git", {"gh": "owner/repo"}),
    ({"doi": "10.1000/A"}, "https://doi.org/10.1000/B", {"doi": "10.1000/a"}),
])
def test_normalized_ids(ids, url, expected):
    assert normalize_ids(ids, url) == expected


def test_exact_doi_merge_alias_upgrade_and_provider_fields():
    old_url = "https://example.org/paper?utm_source=test"
    hits = [
        hit("arxiv", 3, old_url, {"doi": "10.1000/x"}, title="Old title", snippet="arXiv abstract"),
        hit("crossref", 2, "https://doi.org/10.1000/X", {"doi": "10.1000/X"}, title="Publisher title", authors=["A"]),
        hit("openalex", 1, old_url, {"doi": "10.1000/x", "openalex": "W123"}, title="Index title", year=2024),
    ]
    result = merge_hits(hits)
    assert len(result.items) == 1
    item = result.items[0]
    assert item["handle"] == "doi:10.1000/x"
    assert item["title"] == "Publisher title"
    assert item["snippet"] == "arXiv abstract"
    assert item["authors"] == ["A"] and item["year"] == 2024
    assert item["fields"]["title"]["p"] == "crossref"
    assert len(item["providers"]) == 3
    assert url_handle(old_url) in result.identities[0]["aliases"]
    assert "openalex:W123" in result.identities[0]["aliases"]


@pytest.mark.parametrize(("namespace", "left", "right"), [
    ("doi", "10.1000/a", "10.1000/b"),
    ("arxiv", "2301.12345", "2301.12346"),
    ("pmid", "123", "124"),
    ("pmcid", "PMC123", "PMC124"),
])
def test_strong_id_conflicts_veto_url_and_secondary_id_matches(namespace, left, right):
    result = merge_hits([
        hit("crossref", ids={namespace: left, "openalex": "W12"}),
        hit("openalex", ids={namespace: right, "openalex": "W12"}),
    ])
    assert len(result.items) == 2
    assert len({item["handle"] for item in result.items}) == 2
    assert url_handle("https://example.org/paper") in result.ambiguous_aliases
    assert "openalex:W12" in result.ambiguous_aliases
    assert all(not set(identity["aliases"]) & set(result.ambiguous_aliases) for identity in result.identities)


def test_same_doi_with_conflicting_arxiv_ids_has_unambiguous_handles():
    result = merge_hits([
        hit("arxiv", ids={"doi": "10.1000/a", "arxiv": "2301.12345"}),
        hit("openalex", ids={"doi": "10.1000/a", "arxiv": "2301.12346"}),
    ])
    assert len(result.items) == 2
    assert {item["handle"] for item in result.items} == {"arxiv:2301.12345", "arxiv:2301.12346"}
    assert "doi:10.1000/a" in result.ambiguous_aliases


def test_idless_hit_does_not_arbitrarily_resolve_conflicting_url():
    result = merge_hits([
        hit("crossref", ids={"doi": "10.1000/a"}),
        hit("exa"),
        hit("openalex", ids={"doi": "10.1000/b"}),
    ])
    assert len(result.items) == 3
    assert len({item["handle"] for item in result.items}) == 3
    assert next(item for item in result.items if not item["ids"])["handle"].startswith("provider:exa:")


def test_transitive_secondary_id_bridge_cannot_join_conflicting_dois():
    s2_id = "a" * 40
    result = merge_hits([
        hit("crossref", url="https://a.org", ids={"doi": "10.1000/a", "openalex": "W1"}),
        hit("exa", url="https://b.org", ids={"openalex": "W1", "s2": s2_id}),
        hit("openalex", url="https://c.org", ids={"doi": "10.1000/b", "s2": s2_id}),
    ])
    assert len(result.items) == 2
    assert {item["ids"].get("doi") for item in result.items} == {"10.1000/a", "10.1000/b"}
    assert f"s2:{s2_id}" in result.ambiguous_aliases


def test_provider_specific_ids_are_scoped():
    result = merge_hits([
        hit("exa", url="https://a.org", ids={"id": "same"}),
        hit("brave", url="https://b.org", ids={"id": "same"}),
    ])
    assert len(result.items) == 2
    assert {item["handle"] for item in result.items} == {"provider:exa:id:same", "provider:brave:id:same"}


def test_rrf_counts_best_rank_once_per_provider_and_ties_are_stable():
    hits = [hit("exa", 3), hit("exa", 2), hit("brave", 1)]
    item = merge_hits(hits).items[0]
    assert item["score"] == pytest.approx(1 / 62 + 1 / 61)
    assert len(item["providers"]) == 3
    tied = [hit("exa", url="https://a.org"), hit("brave", url="https://b.org")]
    assert merge_hits(tied).items == merge_hits(reversed(tied)).items


def test_weighted_fusion_requires_explicit_opt_in():
    hits = [hit("exa"), hit("brave", 2)]
    baseline = merge_hits(hits).items[0]["score"]
    assert merge_hits(hits, options={"rrf_weights": {"exa": 10}}).items[0]["score"] == baseline
    assert merge_hits(hits, options={"weighted_rrf": True, "rrf_weights": {"exa": 10}}).items[0]["score"] == pytest.approx(10 / 61 + 1 / 62)
    with pytest.raises(ValueError):
        merge_hits(hits, options={"weighted_rrf": True, "rrf_weights": {"exa": float("nan")}})


def test_snippets_are_compact_and_inputs_are_unmodified():
    source = hit(snippet=" ".join(str(number) for number in range(140)), raw={"evidence": "original"})
    result = merge_hits([source], options={"include_raw": True})
    assert len(result.items[0]["snippet"].split()) == 120
    assert len(source["snippet"].split()) == 140
    assert result.items[0]["providers"][0]["raw"] == source["raw"]
    assert merge_hits([source], limit=0).items == []
    assert len(merge_hits([source], limit=0).identities) == 1


def test_bridge_transport_provenance_is_visible_without_raw_payload():
    source = hit(provider="brave-search", raw={"transport": "omniroute", "token": "not-output"})
    result = merge_hits([source]).items[0]["providers"][0]
    assert result["p"] == "brave-search"
    assert result["transport"] == "omniroute"
    assert "raw" not in result
    assert "token" not in str(result)


def test_result_objects_and_empty_hits():
    @dataclass
    class Result:
        hits: list

    assert len(merge_hits([Result([hit()])]).items) == 1
    assert merge_hits([]).items == []
    assert canonical_handle({}, "https://example.org/paper").startswith("url:")
    for limit in (-1, True, 1.5):
        with pytest.raises(ValueError):
            merge_hits([hit()], limit=limit)


def test_arxiv_versions_normalize_to_one_identity_without_losing_source_ids():
    result = merge_hits([
        hit("arxiv", url="https://arxiv.org/abs/2301.12345v1", ids={"arxiv": "2301.12345v1"}),
        hit("openalex", url="https://arxiv.org/abs/2301.12345v2", ids={"arxiv": "2301.12345v2"}),
    ])
    assert len(result.items) == 1
    assert result.items[0]["handle"] == "arxiv:2301.12345"
    assert {source["source_ids"]["arxiv"] for source in result.items[0]["providers"]} == {"2301.12345v1", "2301.12345v2"}
