"""Small, conservative identifier and URL canonicalization helpers."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from url_normalize import url_normalize

STRONG_IDS = ("doi", "arxiv", "pmid", "pmcid")
ID_PRIORITY = (*STRONG_IDS, "openalex", "s2", "gh")
_TRACKERS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "_ga", "_gl",
    "mc_cid", "mc_eid", "igshid", "mkt_tok", "vero_conv", "vero_id",
})
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
_ARXIV = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v\d+)?", re.I)
_ID_NAMES = {
    "arxiv_id": "arxiv", "arxivid": "arxiv", "pubmed": "pmid",
    "pubmedid": "pmid", "pubmedcentral": "pmcid", "openalex_id": "openalex",
    "openalexid": "openalex", "semanticscholar": "s2", "semantic_scholar": "s2",
    "semantic_scholar_id": "s2", "paperid": "s2", "github": "gh",
}


def _digest(value: str) -> str:
    return base64.b32encode(hashlib.sha256(value.encode()).digest()).decode().lower()[:20]


def normalize_url(url: str | None) -> str:
    """Normalize syntax and trackers, retaining a usable HTTP(S) target URL."""
    if not url or not isinstance(url, str):
        return ""
    value = url.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        normalized = url_normalize(value, default_scheme="https")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        params = [
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKERS
        ]
        query = urlencode(sorted(params))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))
    except (ValueError, UnicodeError):
        return ""


def url_key(url: str | None) -> str:
    """Fold scheme, www and trailing slash only for exact URL comparison."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    host = parsed.netloc.removeprefix("www.")
    return urlunsplit(("", host, parsed.path.rstrip("/"), parsed.query, "")).removeprefix("//")


def _doi(value: str) -> str | None:
    value = unquote(value.strip())
    match = _DOI.search(value)
    if not match:
        return None
    doi = match.group().lower()
    # URL queries/fragments and surrounding citation punctuation are not DOI data.
    doi = doi.split("?", 1)[0].split("#", 1)[0].rstrip(".,;")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi if _DOI.fullmatch(doi) else None


def _arxiv(value: str) -> str | None:
    value = unquote(value.strip()).lower()
    value = re.sub(r"^arxiv\s*:\s*", "", value)
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None
        value = re.sub(r"^/(?:abs|pdf|html)/", "", parsed.path).removesuffix(".pdf")
    if not _ARXIV.fullmatch(value):
        return None
    return re.sub(r"v\d+$", "", value)


def _normalize_id(namespace: str, value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if namespace == "doi":
        return _doi(value)
    if namespace == "arxiv":
        return _arxiv(value)
    if namespace == "pmid":
        value = re.sub(r"^pmid\s*:\s*", "", value, flags=re.I)
        if "://" in value:
            value = urlsplit(value).path.strip("/").split("/")[-1]
        return str(int(value)) if value.isdecimal() and int(value) > 0 else None
    if namespace == "pmcid":
        match = re.fullmatch(r"(?:pmcid\s*:\s*)?(?:PMC)?(\d+)", value, flags=re.I)
        return f"PMC{int(match.group(1))}" if match and int(match.group(1)) > 0 else None
    if namespace == "openalex":
        value = value.rstrip("/").split("/")[-1].removeprefix("openalex:")
        return value.upper() if re.fullmatch(r"W\d+", value, flags=re.I) else None
    if namespace == "s2":
        value = value.rstrip("/").split("/")[-1]
        value = re.sub(r"^(?:s2|semanticscholar):", "", value, flags=re.I)
        if re.fullmatch(r"[0-9a-f]{40}", value, flags=re.I):
            return value.lower()
        corpus = re.fullmatch(r"(?:corpusid:|corpus:)(\d+)", value, flags=re.I)
        return f"CorpusId:{int(corpus.group(1))}" if corpus else None
    if namespace == "gh":
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.hostname not in {"github.com", "www.github.com"}:
                return None
            value = parsed.path
        value = value.removeprefix("gh:").strip("/")
        parts = value.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        parts[0], parts[1] = parts[0].lower(), parts[1].removesuffix(".git").lower()
        return "/".join(parts)
    return value


def normalize_ids(ids: Mapping[str, Any] | None = None, url: str | None = None) -> dict[str, str]:
    """Normalize supplied IDs and extract supported scholarly IDs from URLs.

    Unknown IDs are retained; merge callers must scope them to their provider.
    Supplied IDs win over extraction when a URL contains a contradictory ID.
    """
    out: dict[str, str] = {}
    for name, raw in (ids or {}).items():
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            continue
        namespace = _ID_NAMES.get(str(name).lower(), str(name).lower())
        normalized = _normalize_id(namespace, str(raw))
        if normalized:
            out[namespace] = normalized
    if not url:
        return out
    try:
        parsed = urlsplit(url)
        host, path = (parsed.hostname or "").lower(), unquote(parsed.path)
        extracted: dict[str, str] = {}
        doi = _doi(path)
        if doi:
            extracted["doi"] = doi
        if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            arxiv = _arxiv(url)
            if arxiv:
                extracted["arxiv"] = arxiv
        if host == "pubmed.ncbi.nlm.nih.gov":
            pmid = _normalize_id("pmid", path.strip("/"))
            if pmid:
                extracted["pmid"] = pmid
        if host.endswith("ncbi.nlm.nih.gov"):
            pmcid = re.search(r"\bPMC\d+\b", path, re.I)
            if pmcid:
                extracted["pmcid"] = pmcid.group().upper()
        if host in {"openalex.org", "api.openalex.org"}:
            oa = _normalize_id("openalex", path)
            if oa:
                extracted["openalex"] = oa
        if host in {"semanticscholar.org", "www.semanticscholar.org"}:
            s2 = _normalize_id("s2", path)
            if s2:
                extracted["s2"] = s2
        if host in {"github.com", "www.github.com"}:
            gh = _normalize_id("gh", url)
            if gh:
                extracted["gh"] = gh
        for namespace, value in extracted.items():
            out.setdefault(namespace, value)
    except (ValueError, UnicodeError):
        pass
    return out


def id_handles(ids: Mapping[str, str]) -> list[str]:
    """All explicit exact aliases, strongest first."""
    ordered = [f"{name}:{ids[name]}" for name in ID_PRIORITY if ids.get(name)]
    ordered.extend(f"{name}:{value}" for name, value in sorted(ids.items()) if name not in ID_PRIORITY)
    return ordered


def canonical_handle(ids: Mapping[str, str] | None, url: str | None) -> str:
    normalized = normalize_ids(ids, url)
    handles = id_handles(normalized)
    if handles:
        return handles[0]
    key = url_key(url)
    return f"url:{_digest(key)}" if key else ""


def url_handle(url: str | None) -> str:
    key = url_key(url)
    return f"url:{_digest(key)}" if key else ""
