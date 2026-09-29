"""The seeded incident corpus, read from its SQL (Day 19): the eval's answer key.

`docker/postgres/init/03-seed-incidents.sql` is the one place the 18 incidents and their
65 timeline events are written down. The Docs agent retrieves from the rows it produces;
the eval scores against the same rows' ``true_*`` columns. Reading the file rather than
the database keeps the eval set and its answer key buildable with no stack, and makes the
two impossible to edit apart - an ``integration`` test compares this reading with the
live table, so the parser cannot drift from what Postgres holds either.

The parser is a SQL literal reader for the two ``INSERT ... VALUES`` statements in that
file and nothing more: quoted strings (with ``''`` as an escaped quote), numbers, ``NULL``,
and ``'{a,b}'`` array literals. Anything else in a value position is an error, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# src/aioc/evals/seed.py -> the repository root is three directories up from the package.
SEED_PATH = (
    Path(__file__).resolve().parents[3] / "docker" / "postgres" / "init" / "03-seed-incidents.sql"
)

IMPACT_COLUMNS = (
    "error_rate_before",
    "error_rate_after",
    "p50_latency_ms_before",
    "p50_latency_ms_after",
    "p99_latency_ms_before",
    "p99_latency_ms_after",
    "requests_affected",
)


class SeedError(ValueError):
    """The seed file does not read as the two INSERT statements this module expects."""


@dataclass(frozen=True, slots=True)
class SeedEvent:
    id: str
    incident_id: str
    at: datetime
    service: str
    description: str
    kind: str
    kind_detail: str | None
    severity: str | None


@dataclass(frozen=True, slots=True)
class SeedIncident:
    """One corpus row. The ``true_*`` fields are the answer key; an agent under
    evaluation never sees them, the title, or the resolution (`aioc.evals.cases`)."""

    id: str
    title: str
    summary: str
    started_at: datetime
    ended_at: datetime | None
    affected_services: tuple[str, ...]
    true_severity: str
    true_failure_mode: str
    true_failure_mode_detail: str | None
    true_root_cause: str
    resolution: str
    impact: dict[str, float | int | None]
    events: tuple[SeedEvent, ...]

    @property
    def document_id(self) -> str:
        """The id the Docs agent cites this incident's post-mortem by. The same mapping
        as `aioc.retrieval.corpus.doc_id_for`, which a test holds this to."""
        return "doc_" + self.id.removeprefix("inc_")


# ------------------------------------------------------------------------------- reading


def _strip_comments(sql: str) -> str:
    """``--`` comments removed, quoted strings left alone (a string may contain ``--``)."""
    out: list[str] = []
    i, n, quoted = 0, len(sql), False
    while i < n:
        ch = sql[i]
        if quoted:
            out.append(ch)
            if ch == "'":
                if sql[i + 1 : i + 2] == "'":
                    out.append("'")
                    i += 1
                else:
                    quoted = False
        elif ch == "'":
            quoted = True
            out.append(ch)
        elif sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _rows(sql: str, table: str) -> tuple[list[str], list[list[Any]]]:
    """The column list and the value tuples of ``INSERT INTO <table> (...) VALUES ...``."""
    head = f"INSERT INTO {table}"
    start = sql.find(head)
    if start < 0:
        raise SeedError(f"no `{head}` statement in the seed file")
    open_cols = sql.index("(", start)
    close_cols = sql.index(")", open_cols)
    columns = [c.strip() for c in sql[open_cols + 1 : close_cols].split(",")]
    values_at = sql.find("VALUES", close_cols)
    if values_at < 0:
        raise SeedError(f"`{head}` has no VALUES clause")

    rows: list[list[Any]] = []
    i, n = values_at + len("VALUES"), len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace() or ch == ",":
            i += 1
        elif ch == "(":
            row, i = _tuple(sql, i + 1)
            if len(row) != len(columns):
                raise SeedError(
                    f"{table}: a row has {len(row)} values for {len(columns)} columns: {row[:2]}"
                )
            rows.append(row)
        else:
            break  # ON CONFLICT ... or the closing semicolon
    if not rows:
        raise SeedError(f"`{head}` has no rows")
    return columns, rows


def _tuple(sql: str, i: int) -> tuple[list[Any], int]:
    """One parenthesised value list, from just inside its ``(``. Returns the values and
    the index just past its ``)``."""
    values: list[Any] = []
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace() or ch == ",":
            i += 1
        elif ch == ")":
            return values, i + 1
        elif ch == "'":
            text, i = _string(sql, i + 1)
            values.append(text)
        else:
            j = i
            while j < n and sql[j] not in ",)" and not sql[j].isspace():
                j += 1
            values.append(_bare(sql[i:j]))
            i = j
    raise SeedError("unterminated value list in the seed file")


def _string(sql: str, i: int) -> tuple[str, int]:
    out: list[str] = []
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            if sql[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise SeedError("unterminated string literal in the seed file")


def _bare(token: str) -> float | int | None:
    if token.upper() == "NULL":
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        raise SeedError(f"unexpected value in the seed file: {token!r}") from None


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SeedError(f"expected an RFC 3339 timestamp with an explicit Z, got {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _array(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not (value.startswith("{") and value.endswith("}")):
        raise SeedError(f"expected an array literal, got {value!r}")
    inner = value[1:-1]
    return tuple(part.strip() for part in inner.split(",")) if inner else ()


def parse_seed(sql: str) -> list[SeedIncident]:
    """The incidents in seed order, each with its events in ascending time order."""
    code = _strip_comments(sql)
    event_columns, event_rows = _rows(code, "incident_timeline_events")
    events: dict[str, list[SeedEvent]] = {}
    for values in event_rows:
        row = dict(zip(event_columns, values, strict=True))
        events.setdefault(row["incident_id"], []).append(
            SeedEvent(
                id=row["id"],
                incident_id=row["incident_id"],
                at=_timestamp(row["at"]),
                service=row["service"],
                description=row["description"],
                kind=row["kind"],
                kind_detail=row["kind_detail"],
                severity=row["severity"],
            )
        )

    columns, rows = _rows(code, "incidents")
    incidents: list[SeedIncident] = []
    for values in rows:
        row = dict(zip(columns, values, strict=True))
        incidents.append(
            SeedIncident(
                id=row["id"],
                title=row["title"],
                summary=row["summary"],
                started_at=_timestamp(row["started_at"]),
                ended_at=None if row["ended_at"] is None else _timestamp(row["ended_at"]),
                affected_services=_array(row["affected_services"]),
                true_severity=row["true_severity"],
                true_failure_mode=row["true_failure_mode"],
                true_failure_mode_detail=row["true_failure_mode_detail"],
                true_root_cause=row["true_root_cause"],
                resolution=row["resolution"],
                impact={name: row[name] for name in IMPACT_COLUMNS},
                events=tuple(sorted(events.get(row["id"], []), key=lambda e: e.at)),
            )
        )
    orphans = set(events) - {incident.id for incident in incidents}
    if orphans:
        raise SeedError(f"timeline events for incidents that are not seeded: {sorted(orphans)}")
    return incidents


@lru_cache(maxsize=1)
def load_seed() -> tuple[SeedIncident, ...]:
    """The committed seed file, parsed once per process."""
    return tuple(parse_seed(SEED_PATH.read_text(encoding="utf-8")))


def seed_by_id() -> dict[str, SeedIncident]:
    return {incident.id: incident for incident in load_seed()}
