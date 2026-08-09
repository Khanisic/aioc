"""`correlate_events` as a real MCP server over stdio (Day 7, contract sec 7.2).

Signals that moved together around an anchor moment, computed from the incident corpus
seeded on Day 5.

    uv run python -m aioc.tools.incident.correlate_server       # stdio, for an MCP client

**No `aioc.contracts` import anywhere in this file** - the MCP boundary is JSON Schema, not
Pydantic (contract sec 6), so the enum members and the input schema are written out longhand
and `tests/test_correlate_tool.py` asserts the copies still match the Python enums.

**The method, stated plainly because `method` is on the wire.** The window is centred on the
anchor and cut into 60-second bins. Every recorded event in the window becomes part of a
signal - one signal per (service, event kind) pair - and each signal's per-bin counts are
Pearson-correlated against a unit impulse placed at each candidate bin in turn; the reported
`correlation` is the best alignment, `lag_seconds` is the offset of that best bin from the
anchor bin, and `direction` reads `leads` when the signal peaked before the anchor. This is
temporal alignment over recorded events, which is exactly what the corpus can support -
and it is correlation, not causation, as the description says in as many words.

Three deliberate choices to know before changing this file:

**"Aligned points" are 60-second bins, so `INSUFFICIENT_SAMPLES` is a window property.**
The contract defines the error as "fewer than 10 aligned points" (sec 7.2); with fixed
60-second bins that means any window under 600 seconds. It is a `business` error, not
`validation`, and that is faithful to the taxonomy: a 300-second window is well-formed
input the schema accepts; it is the correlation method that cannot honestly answer it.
`sample_size` on each correlation entry is that signal's event count in the window - the
honest number for judging fragility, and a `sample_size` of 1 deserves the caution the
description spells out.

**The transient error code deviates from sec 7.2, deliberately and additively.** The
contract lists `PROMETHEUS_TIMEOUT`, written when correlations were expected to come from
Prometheus. The events live in the Postgres corpus, so a timeout here is a Postgres timeout
and the contract's code would be a false value in a programmatically matched field. This
server emits `EVENT_STORE_TIMEOUT`, the same reasoning (and the same pending sec 0
paperwork) as `TIMELINE_STORE_TIMEOUT` in `timeline_server`. `meta.source` reports
`postgres` so the caller is never guessing.

**The chaos namespace returns a `permission` error** (`aioc.tools.policy`): the injector's
signals are the Day 19 eval's ground truth, and a tool that serves them turns diagnosis
into transcription. See the policy module for why this is `permission` and not `business`.

Framework input validation is off (`validate_input=False`) for the same reason as the
timeline server: the framework returns plain text where the contract requires a structured
`validation` error with `details.field` and `details.expected`.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from aioc.tools.envelope import Timer, err, ok
from aioc.tools.incident.store import dsn as _dsn
from aioc.tools.policy import ground_truth_denied, restricted_names

TOOL_NAME = "correlate_events"

# Longhand copies of the contract enums (sec 7.2). Deliberate duplication - module docstring.
SIGNAL_TYPES = ("metric", "log_rate", "event", "deploy", "other")

# How a recorded event kind reads as a signal type. Total over TimelineEvent.kind, and
# tested against the Python enum: an unmapped kind would silently vanish from results.
KIND_TO_SIGNAL_TYPE = {
    "deploy": "deploy",
    "metric_threshold": "metric",
    "log_pattern": "log_rate",
    "alert": "event",
    "config_change": "event",
    "restart": "event",
    "scale": "event",
    "other": "other",
}

DEFAULT_WINDOW_SECONDS = 900
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 21600
DEFAULT_MIN_CORRELATION = 0.5

BIN_SECONDS = 60
MIN_ALIGNED_BINS = 10  # the contract's "fewer than 10 aligned points" (sec 7.2)

METHOD = f"impulse_pearson_over_{BIN_SECONDS}s_event_bins"


# ------------------------------------------------------------------------ tool description
#
# The four-part template (contract sec 6.5), in order. Part 4 is the intervention the
# Day 13/14 routing case study measures.

DESCRIPTION = """\
Finds which recorded signals moved together around one anchor moment, across services. Each \
signal is a (service, event kind) stream from the incident history; `correlation` is Pearson \
alignment of its 60-second binned counts against the anchor moment, `lag_seconds` is where it \
peaked relative to the anchor, and `direction` is `leads` (peaked before the anchor), `lags`, \
or `coincident`. THIS IS CORRELATION, NOT CAUSATION: a leading signal is a lead, not a proven \
cause. Inputs: anchor by EITHER `anchor_event_id` (an `evt_*` id from `get_incident_timeline`) \
OR `anchor_at` (RFC 3339 UTC with explicit Z) plus `anchor_service` - exactly one of the two \
forms; `window_seconds` (60-21600, default 900) is centred on the anchor; `services`, \
`signal_types` (metric, log_rate, event, deploy, other) and `min_correlation` (0.0-1.0, \
default 0.5) filter the result.

Example queries this tool answers:
- "When payments-api alerted at 14:12, what else moved across the stack?"
- "Did anything on inventory-api lead the checkout-api error spike?"
- "Which deploys line up with event evt_0002_2?"
- "Around 2026-01-22T15:12Z, did log patterns on any service move with the alert?"

Edge cases and limits: the window must give at least 10 aligned 60-second bins, so \
`window_seconds` below 600 returns INSUFFICIENT_SAMPLES (business) - widen the window. An \
empty `correlations` array is a successful answer meaning nothing recorded moved together \
above `min_correlation` - it is NOT an error and NOT proof of independence, because only \
recorded events are visible here. `sample_size` is the number of events behind each signal; \
treat a correlation with `sample_size` 1 as a coincidence to verify, not a finding. Signals \
are aligned at 60-second resolution, so `lag_seconds` is quantised to 60. Chaos-injector \
signals (any `chaos*` service) are the eval harness's injected ground truth and return a \
`permission` error rather than data.

When to use this vs. the alternative: use `correlate_events` when you have a known incident \
moment and want to know WHICH SIGNALS MOVED TOGETHER across services - finding co-movers and \
leads. Use `get_incident_timeline` instead when you need WHAT HAPPENED AND IN WHAT ORDER for \
a single service. If the anchor is a deployment and you want to know what changed inside it, \
use `diff_release`. "What moved with what": this tool; "what came first on one service": the \
timeline; "what was in the deploy": the diff."""


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "anchor_event_id": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "Anchor by recorded event: an `evt_*` id from `get_incident_timeline`. "
                "Exactly one of `anchor_event_id` or `anchor_at` must be given."
            ),
        },
        "anchor_at": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "Anchor by moment: RFC 3339 UTC with an explicit Z. Requires "
                "`anchor_service`. Exactly one of `anchor_event_id` or `anchor_at`."
            ),
        },
        "anchor_service": {
            "type": ["string", "null"],
            "default": None,
            "description": "The service the anchor moment belongs to. Only with `anchor_at`.",
        },
        "window_seconds": {
            "type": "integer",
            "minimum": MIN_WINDOW_SECONDS,
            "maximum": MAX_WINDOW_SECONDS,
            "default": DEFAULT_WINDOW_SECONDS,
            "description": (
                f"Window centred on the anchor, {MIN_WINDOW_SECONDS}-{MAX_WINDOW_SECONDS} "
                "seconds. Below 600 there are fewer than 10 aligned bins."
            ),
        },
        "services": {
            "type": ["array", "null"],
            "default": None,
            "items": {"type": "string"},
            "description": "Restrict candidate signals to these services. Omit for all.",
        },
        "signal_types": {
            "type": ["array", "null"],
            "default": None,
            "items": {"type": "string", "enum": list(SIGNAL_TYPES)},
            "description": "Restrict to these signal types. Omit for all types.",
        },
        "min_correlation": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": DEFAULT_MIN_CORRELATION,
            "description": "Drop correlations below this absolute value, 0.0-1.0.",
        },
    },
}


# ------------------------------------------------------------------------- input validation


class _Invalid(Exception):
    """A validation failure carrying the field and expectation the contract requires."""

    def __init__(self, field: str, expected: str, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.expected = expected
        self.code = code or "INVALID_INPUT"


def _parse_ts(raw: object, field: str) -> datetime:
    if not isinstance(raw, str):
        raise _Invalid(
            field, "an RFC 3339 UTC string", f"{field} must be a string, got {type(raw).__name__}"
        )
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _Invalid(
            field, "RFC 3339 UTC, e.g. 2026-03-02T18:00:00Z", f"{field} is not a valid timestamp"
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _validate(args: dict[str, Any]) -> dict[str, Any]:
    """Normalise and validate the input, raising `_Invalid` with a field and expectation."""
    unknown = set(args) - set(INPUT_SCHEMA["properties"])
    if unknown:
        raise _Invalid(
            sorted(unknown)[0],
            f"one of {sorted(INPUT_SCHEMA['properties'])}",
            f"unknown input field(s): {sorted(unknown)}",
        )

    anchor_event_id = args.get("anchor_event_id")
    anchor_at_raw = args.get("anchor_at")
    # Exactly one anchor form. Both and neither are the same mistake - the tool cannot know
    # which moment the caller means - and the contract gives it a dedicated code (sec 7.2).
    if (anchor_event_id is None) == (anchor_at_raw is None):
        raise _Invalid(
            "anchor_event_id",
            "exactly one of `anchor_event_id` or `anchor_at`",
            "supply exactly one of anchor_event_id or anchor_at, not "
            + ("both" if anchor_event_id is not None else "neither"),
            code="AMBIGUOUS_ANCHOR",
        )

    anchor_service = args.get("anchor_service")
    anchor_at: datetime | None = None
    if anchor_event_id is not None:
        if not isinstance(anchor_event_id, str) or not anchor_event_id.startswith("evt_"):
            raise _Invalid(
                "anchor_event_id",
                "an opaque id starting `evt_`, as returned by get_incident_timeline",
                f"anchor_event_id {anchor_event_id!r} is not an evt_* id",
            )
        if anchor_service is not None:
            raise _Invalid(
                "anchor_service",
                "omitted when `anchor_event_id` is given (the event carries its service)",
                "anchor_service conflicts with anchor_event_id",
            )
    else:
        anchor_at = _parse_ts(anchor_at_raw, "anchor_at")
        if not isinstance(anchor_service, str) or not anchor_service.strip():
            raise _Invalid(
                "anchor_service",
                "a non-empty service name (required with `anchor_at`)",
                "anchor_service is required when anchoring by anchor_at",
            )
        anchor_service = anchor_service.strip()

    window_seconds = args.get("window_seconds", DEFAULT_WINDOW_SECONDS)
    if not isinstance(window_seconds, int) or isinstance(window_seconds, bool):
        raise _Invalid(
            "window_seconds",
            f"an integer {MIN_WINDOW_SECONDS}-{MAX_WINDOW_SECONDS}",
            "window_seconds must be an integer",
        )
    if not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS:
        raise _Invalid(
            "window_seconds",
            f"an integer {MIN_WINDOW_SECONDS}-{MAX_WINDOW_SECONDS}",
            f"window_seconds out of range: {window_seconds}",
        )

    services = args.get("services")
    if services is not None:
        if not isinstance(services, list) or not services:
            raise _Invalid(
                "services",
                "a non-empty array of service names",
                "services must be a non-empty array",
            )
        bad = [s for s in services if not isinstance(s, str) or not s.strip()]
        if bad:
            raise _Invalid(
                "services", "non-empty service name strings", f"invalid service name(s): {bad}"
            )
        services = [s.strip() for s in services]

    signal_types = args.get("signal_types")
    if signal_types is not None:
        if not isinstance(signal_types, list) or not signal_types:
            raise _Invalid(
                "signal_types",
                f"a non-empty array of {list(SIGNAL_TYPES)}",
                "signal_types must be a non-empty array",
            )
        bad_types = [t for t in signal_types if t not in SIGNAL_TYPES]
        if bad_types:
            raise _Invalid(
                "signal_types",
                f"members of {list(SIGNAL_TYPES)}",
                f"unknown signal type(s): {bad_types}",
            )

    min_correlation = args.get("min_correlation", DEFAULT_MIN_CORRELATION)
    if isinstance(min_correlation, bool) or not isinstance(min_correlation, (int, float)):
        raise _Invalid(
            "min_correlation", "a number between 0.0 and 1.0", "min_correlation must be a number"
        )
    if not 0.0 <= float(min_correlation) <= 1.0:
        raise _Invalid(
            "min_correlation",
            "a number between 0.0 and 1.0",
            f"min_correlation out of range: {min_correlation}",
        )

    return {
        "anchor_event_id": anchor_event_id,
        "anchor_at": anchor_at,
        "anchor_service": anchor_service,
        "window_seconds": window_seconds,
        "services": list(services) if services else None,
        "signal_types": list(signal_types) if signal_types else None,
        "min_correlation": float(min_correlation),
    }


# --------------------------------------------------------------------- correlation (pure)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Plain Pearson r, or None when either series has zero variance."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def correlate(
    events: list[dict[str, Any]],
    *,
    anchor_at: datetime,
    window_seconds: int,
    anchor_event_id: str | None = None,
    signal_types: list[str] | None = None,
    min_correlation: float = DEFAULT_MIN_CORRELATION,
) -> tuple[list[dict[str, Any]], int]:
    """Align every (service, kind) signal against the anchor moment.

    Pure and deterministic so the math is testable without a database. ``events`` are dicts
    with ``id``, ``at`` (aware datetime), ``service``, ``kind``. Returns the correlation
    entries above ``min_correlation`` (best first) plus the count of candidate signals
    before that filter, which becomes ``meta.total_available``.
    """
    n_bins = window_seconds // BIN_SECONDS
    start = anchor_at - timedelta(seconds=window_seconds / 2)
    anchor_bin = min(n_bins - 1, max(0, int((anchor_at - start).total_seconds() // BIN_SECONDS)))

    # One signal per (service, kind), the anchor event itself excluded - an event always
    # perfectly "correlates" with its own occurrence, and reporting that would put a
    # tautology at the top of every result.
    signals: dict[tuple[str, str], list[int]] = {}
    counts_by_signal: dict[tuple[str, str], int] = {}
    for event in events:
        if anchor_event_id is not None and event["id"] == anchor_event_id:
            continue
        kind = str(event["kind"])
        if signal_types is not None and KIND_TO_SIGNAL_TYPE.get(kind, "other") not in signal_types:
            continue
        offset = (event["at"] - start).total_seconds()
        if not 0 <= offset <= window_seconds:
            continue
        bin_index = min(n_bins - 1, int(offset // BIN_SECONDS))
        key = (str(event["service"]), kind)
        signals.setdefault(key, [0] * n_bins)[bin_index] += 1
        counts_by_signal[key] = counts_by_signal.get(key, 0) + 1

    results: list[dict[str, Any]] = []
    for (service, kind), series in sorted(signals.items()):
        floats = [float(c) for c in series]
        best: tuple[float, int] | None = None
        for candidate_bin in range(n_bins):
            impulse = [1.0 if i == candidate_bin else 0.0 for i in range(n_bins)]
            r = _pearson(impulse, floats)
            if r is None:
                break  # zero variance is a property of the series, not the impulse position
            # Strictly-greater keeps the earliest best bin on ties, for determinism.
            if best is None or r > best[0]:
                best = (r, candidate_bin)
        if best is None:
            continue
        correlation, best_bin = best
        lag_seconds = (best_bin - anchor_bin) * BIN_SECONDS
        results.append(
            {
                "signal": kind,
                "service": service,
                "correlation": round(correlation, 4),
                "lag_seconds": lag_seconds,
                "direction": (
                    "coincident"
                    if best_bin == anchor_bin
                    else ("leads" if lag_seconds < 0 else "lags")
                ),
                "sample_size": counts_by_signal[(service, kind)],
            }
        )

    candidates = len(results)
    kept = [r for r in results if r["correlation"] >= min_correlation]
    kept.sort(key=lambda r: (-r["correlation"], r["service"], r["signal"]))
    return kept, candidates


# ------------------------------------------------------------------------------ data access

_ANCHOR = "SELECT id, at, service FROM incident_timeline_events WHERE id = %(id)s"

_EVENTS = """
SELECT e.id, e.at, e.service, e.kind
  FROM incident_timeline_events e
 WHERE e.at >= %(start)s AND e.at <= %(end)s
   AND (%(services)s::text[] IS NULL OR e.service = ANY(%(services)s::text[]))
 ORDER BY e.at, e.id
"""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_correlations(params: dict[str, Any], *, dsn: str | None = None) -> types.CallToolResult:
    """Resolve the anchor, pull the window's events, and build the response.

    Separated from the MCP plumbing so it is testable without a server, and so the error
    mapping is one readable block.
    """
    n_bins = params["window_seconds"] // BIN_SECONDS
    if n_bins < MIN_ALIGNED_BINS:
        # Business, not validation: the input is well-formed, the method cannot honestly
        # answer it (module docstring). The remediation names the concrete fix.
        return err(
            "business",
            "INSUFFICIENT_SAMPLES",
            f"A {params['window_seconds']}s window gives {n_bins} aligned {BIN_SECONDS}s bins; "
            f"correlation needs at least {MIN_ALIGNED_BINS}.",
            remediation=(
                f"Widen window_seconds to at least {MIN_ALIGNED_BINS * BIN_SECONDS} "
                "(the default 900 works), or use get_incident_timeline if you only need "
                "the ordered events near the anchor."
            ),
            details={"aligned_bins": n_bins, "required": MIN_ALIGNED_BINS},
        )

    try:
        with Timer() as timer, psycopg.connect(dsn or _dsn(), connect_timeout=5) as conn:
            with conn.cursor() as cur:
                anchor_at: datetime | None = params["anchor_at"]
                anchor_service = params["anchor_service"]
                if params["anchor_event_id"] is not None:
                    cur.execute(_ANCHOR, {"id": params["anchor_event_id"]})
                    row = cur.fetchone()
                    if row is None:
                        return err(
                            "business",
                            "UNKNOWN_EVENT",
                            f"No recorded event with id {params['anchor_event_id']!r}.",
                            remediation=(
                                "List the window's events with get_incident_timeline and "
                                "anchor on one of the ids it returns, or anchor by "
                                "anchor_at + anchor_service instead."
                            ),
                            details={"anchor_event_id": params["anchor_event_id"]},
                        )
                    anchor_at = row[1].astimezone(UTC)
                    anchor_service = row[2]
                assert anchor_at is not None  # one anchor form is guaranteed by _validate

                half = timedelta(seconds=params["window_seconds"] / 2)
                cur.execute(
                    _EVENTS,
                    {
                        "start": anchor_at - half,
                        "end": anchor_at + half,
                        "services": params["services"],
                    },
                )
                rows = cur.fetchall()
    except psycopg.OperationalError as exc:
        return err(
            "transient",
            "EVENT_STORE_TIMEOUT",
            f"Could not reach the incident store ({type(exc).__name__}).",
            retry_after_ms=2000,
            remediation="Retry after 2s. If it persists, the stack may be down - `make up`.",
            details={"store": "postgres"},
        )
    except psycopg.Error as exc:
        return err(
            "transient",
            "EVENT_STORE_ERROR",
            f"The incident store rejected the query ({type(exc).__name__}).",
            retry_after_ms=1000,
            remediation="Retry once. A repeat failure is a bug in the tool, not the request.",
        )

    events = [
        {"id": row[0], "at": row[1].astimezone(UTC), "service": row[2], "kind": row[3]}
        for row in rows
    ]
    correlations, candidates = correlate(
        events,
        anchor_at=anchor_at,
        window_seconds=params["window_seconds"],
        anchor_event_id=params["anchor_event_id"],
        signal_types=params["signal_types"],
        min_correlation=params["min_correlation"],
    )

    half = timedelta(seconds=params["window_seconds"] / 2)
    return ok(
        {
            "anchor": {
                "event_id": params["anchor_event_id"],
                "service": anchor_service,
                "at": _stamp(anchor_at),
                "window": {"start": _stamp(anchor_at - half), "end": _stamp(anchor_at + half)},
            },
            "correlations": correlations,
            "method": METHOD,
        },
        returned=len(correlations),
        truncated=False,
        total_available=candidates,
        query_ms=timer.ms,
        source="postgres",
        as_of=_stamp(datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------- MCP plumbing

server = Server("aioc-incident-correlate")


# The two `type: ignore`s below are the mcp library's decorators being untyped, not a
# looseness on our side - both handlers keep full annotations.
@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name=TOOL_NAME, description=DESCRIPTION, inputSchema=INPUT_SCHEMA)]


# validate_input=False on purpose: the framework's own validation returns a plain-text
# error, and the contract requires a structured `validation` error. See the module docstring.
@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    if name != TOOL_NAME:
        return err(
            "validation",
            "UNKNOWN_TOOL",
            f"This server exposes only {TOOL_NAME}.",
            remediation=f"Call {TOOL_NAME} instead.",
            details={"field": "name", "expected": TOOL_NAME, "received": name},
        )
    try:
        params = _validate(arguments or {})
    except _Invalid as exc:
        return err(
            "validation",
            exc.code,
            str(exc),
            remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
            details={"field": exc.field, "expected": exc.expected},
        )
    # The chaos namespace is the eval's injected ground truth (shared policy, Day 7).
    anchor_service = params["anchor_service"]
    restricted = restricted_names(
        [*(params["services"] or []), *([anchor_service] if anchor_service else [])]
    )
    if restricted:
        return ground_truth_denied("services", restricted)
    # psycopg is synchronous; run it off the event loop.
    return await asyncio.to_thread(fetch_correlations, params)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
