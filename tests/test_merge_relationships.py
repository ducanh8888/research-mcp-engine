import pytest

from research_engine.merge import merge_hits
from research_engine.merge.relationships import normalized_title


def paper(doi, title="Evidence retrieval with conservative identifiers", **fields):
    return {
        "provider": "crossref", "rank": 1, "ids": {"doi": doi},
        "url": f"https://doi.org/{doi}", "title": title, "year": 2024, "authors": ["Alice Smith"], **fields,
    }


def test_fuzzy_relations_require_explicit_opt_in_and_preserve_distinct_dois():
    sources = [paper("10.1000/a"), paper("10.1000/b")]
    baseline = merge_hits(sources)
    assert all("related" not in item for item in baseline.items)
    enhanced = merge_hits(sources, options={"fuzzy_relationships": True})
    assert len(enhanced.items) == 2
    assert {item["handle"] for item in enhanced.items} == {"doi:10.1000/a", "doi:10.1000/b"}
    assert all(item["related"][0]["type"] == "possible_duplicate" for item in enhanced.items)
    assert [item["score"] for item in enhanced.items] == [item["score"] for item in baseline.items]


def test_fuzzy_relations_need_year_and_author_agreement():
    sources = [paper("10.1000/a"), paper("10.1000/b", year=2010), paper("10.1000/c", authors=["Bob Jones"])]
    assert all("related" not in item for item in merge_hits(sources, options={"fuzzy_relationships": True}).items)
    assert normalized_title("  Résults: ＣＡＳＥ! ") == "résults case"


def test_explicit_preprint_relationship_is_preserved_without_collapsing():
    sources = [
        paper("10.1000/preprint", raw={"relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1000/published"}]}}),
        paper("10.1000/published"),
    ]
    result = merge_hits(sources, options={"version_links": True})
    assert len(result.items) == 2
    preprint = next(item for item in result.items if item["handle"] == "doi:10.1000/preprint")
    assert preprint["related"] == [{"type": "is-preprint-of", "handle": "doi:10.1000/published", "asserted_by": "crossref"}]
    assert all("related" not in item for item in merge_hits(sources).items)


def test_near_duplicate_snippets_add_links_without_joining_ids():
    text = " ".join(f"token{index}" for index in range(100))
    sources = [paper("10.1000/a", snippet=text), paper("10.1000/b", snippet=text)]
    enhanced = merge_hits(sources, options={"near_duplicate_links": True})
    assert len(enhanced.items) == 2
    assert all(item["related"][0]["type"] == "near_duplicate" for item in enhanced.items)
    assert all(item["related"][0]["distance"] == 0 for item in enhanced.items)


def test_relationship_thresholds_are_validated():
    with pytest.raises(ValueError):
        merge_hits([paper("10.1000/a")], options={"fuzzy_relationships": True, "fuzzy_title_threshold": 2})


def test_hierarchical_fusion_counts_correlated_provider_group_once():
    sources = [
        {**paper("10.1000/a"), "provider": "index-a"},
        {**paper("10.1000/a"), "provider": "index-b"},
        {**paper("10.1000/b"), "provider": "independent"},
    ]
    groups = {"index-a": "shared-index", "index-b": "shared-index"}
    baseline = merge_hits(sources, options={"independence_groups": groups})
    assert next(item for item in baseline.items if item["ids"]["doi"] == "10.1000/a")["score"] == pytest.approx(2 / 61)
    enhanced = merge_hits(sources, options={"hierarchical_rrf": True, "independence_groups": groups})
    assert all(item["score"] == pytest.approx(1 / 61) for item in enhanced.items)
    assert all(item["fusion"]["method"] == "hierarchical_rrf" for item in enhanced.items)
    weighted = merge_hits(sources, options={"hierarchical_rrf": True, "independence_groups": groups, "group_weights": {"independent": 2}})
    assert weighted.items[0]["ids"]["doi"] == "10.1000/b"
