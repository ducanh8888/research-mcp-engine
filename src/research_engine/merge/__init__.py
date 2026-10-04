"""Canonical identities, conservative merge, fusion and optional reranking."""

from .canonical import canonical_handle, normalize_ids, normalize_url, url_key
from .core import MergeOutput, merge_hits, merge_results

__all__ = [
    "MergeOutput", "canonical_handle", "merge_hits", "merge_results",
    "normalize_ids", "normalize_url", "url_key",
]
