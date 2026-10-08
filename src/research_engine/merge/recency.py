"""Publication-date evidence for a requested search recency window.

Upstream recency filters are requests, not guarantees: OmniRoute accepts
``time_range`` for Nimble but its normalizer never returns a date, and Firecrawl
news dates may be relative ("2 weeks ago"). Only an absolute calendar date that a
provider returned for a sighting counts as evidence. Nothing is inferred from
URLs, snippets, relative phrases or retrieval time.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

WINDOW_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december")
_MONTHS = {**{name: index for index, name in enumerate(_MONTH_NAMES, 1)},
           **{name[:3]: index for index, name in enumerate(_MONTH_NAMES, 1)}, "sept": 9}
_WRITTEN = re.compile(r"([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})")


def parse_published(value: Any) -> date | None:
    """Return the calendar date of an absolute timestamp, else ``None``.

    Accepted: ISO 8601 dates/datetimes and English "Mon D, YYYY" dates. Relative
    phrases, bare years and malformed or impossible dates are not evidence.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if match := _ISO.fullmatch(text):
        year, month, day = (int(part) for part in match.groups())
    elif match := _WRITTEN.fullmatch(text):
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            return None
        day, year = int(match.group(2)), int(match.group(3))
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def enforce_recency(items: list[dict[str, Any]], recency: str, today: date) -> tuple[list[dict[str, Any]], dict]:
    """Drop known out-of-window items and label the evidence for every kept item.

    An item is ``verified`` when every sighting date parsed is inside the window,
    ``conflicting`` when its sources disagree across the boundary, and
    ``unverified`` when no source supplied an absolute date. Kept items retain
    their fused order and scores.
    """
    start = today - timedelta(days=WINDOW_DAYS[recency])
    # One day of slack for providers in time zones ahead of the server.
    end = today + timedelta(days=1)
    counts = {"verified": 0, "unverified": 0, "conflicting": 0, "excluded_out_of_window": 0}
    by_provider: dict[str, dict[str, int]] = defaultdict(
        lambda: {"in_window": 0, "out_of_window": 0, "no_date": 0})
    kept: list[dict[str, Any]] = []
    for item in items:
        verdicts = set()
        for source in item.get("providers", []):
            parsed = parse_published(source.get("published"))
            verdict = "no_date" if parsed is None else "in_window" if start <= parsed <= end else "out_of_window"
            by_provider[source["p"]][verdict] += 1
            verdicts.add(verdict)
        dated = verdicts - {"no_date"}
        if dated == {"out_of_window"}:
            counts["excluded_out_of_window"] += 1
            continue
        status = ("conflicting" if dated == {"in_window", "out_of_window"} else
                  "verified" if dated == {"in_window"} else "unverified")
        counts[status] += 1
        kept.append({**item, "recency_check": status})
    report = {"requested": recency, "window_start": start.isoformat(), "window_end": today.isoformat(),
              **counts, "by_provider": dict(sorted(by_provider.items()))}
    return kept, report
