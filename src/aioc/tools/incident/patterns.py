"""Recurring-pattern detection shared by `analyze_logs` and `analyze_events` (Day 13).

Both tools return ``patterns_detected: [{pattern, count, first_at, last_at}]`` (contract sec
7.5 / 7.6). A "pattern" here is a *template*: the text with its variable parts replaced by
placeholders, so that ``GET /process 200 OK`` from two thousand different client ports is
one pattern with a count of two thousand, and a stack trace that fired eleven times is one
pattern with ``first_at`` and ``last_at`` bracketing the burst.

Deliberately simple normalisation, and documented as such:

- UUIDs, long hex runs (7+ characters: SHAs, request ids), IPv4 addresses with an optional
  port, and RFC 3339 timestamps become named placeholders.
- Numbers of four or more digits, and decimals with two or more digits on either side of
  the point, become ``<n>`` - ports, sizes, latencies, and identifiers vary. Three-digit-
  or-shorter integers are kept because in service output they are usually codes (an HTTP
  status, an exit code) rather than identifiers, and a template that collapsed ``500`` into
  ``200`` would hide the one distinction an operator is looking for; short decimals such as
  the ``1.1`` in ``HTTP/1.1`` are kept for the same reason.
- Whitespace collapses to single spaces.

**No `aioc.contracts` import** - this is a tool-server module (contract sec 6).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

DEFAULT_PATTERN_LIMIT = 10

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
_HEX = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,}\b")
# Decimals with two or more digits on either side of the point (latencies, rates, sizes)
# and integers of four or more digits. `1.1` in `HTTP/1.1` and `1.5s` are left alone.
_LONG_NUMBER = re.compile(r"(?<![\w.])(?:\d{2,}\.\d+|\d+\.\d{2,}|\d{4,})(?![\d.])")
_SPACES = re.compile(r"\s+")


def template(text: str) -> str:
    """The pattern a line belongs to - its text with the variable parts replaced."""
    out = _UUID.sub("<uuid>", text)
    out = _TIMESTAMP.sub("<ts>", out)
    out = _IP.sub("<ip>", out)
    out = _HEX.sub("<hex>", out)
    out = _LONG_NUMBER.sub("<n>", out)
    return _SPACES.sub(" ", out).strip()


def detect_patterns(
    items: Iterable[tuple[datetime, str]], *, limit: int = DEFAULT_PATTERN_LIMIT
) -> list[dict[str, Any]]:
    """Group ``(at, text)`` pairs by template; the most frequent ``limit`` patterns, each with
    its count and the first and last time it was seen. Ties break on first appearance so
    the result is deterministic for a given input order."""
    counts: Counter[str] = Counter()
    first: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    order: dict[str, int] = {}
    for position, (at, text) in enumerate(items):
        key = template(text)
        counts[key] += 1
        order.setdefault(key, position)
        if key not in first or at < first[key]:
            first[key] = at
        if key not in last or at > last[key]:
            last[key] = at
    ranked = sorted(counts, key=lambda k: (-counts[k], order[k]))
    return [
        {
            "pattern": key,
            "count": counts[key],
            "first_at": _iso(first[key]),
            "last_at": _iso(last[key]),
        }
        for key in ranked[:limit]
    ]


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
