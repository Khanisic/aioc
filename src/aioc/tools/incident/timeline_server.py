"""`get_incident_timeline` as a real MCP server over stdio (Day 6, contract sec 7.1).

Ordered operational events for one service over a window, read from the incident corpus
seeded on Day 5.

    uv run python -m aioc.tools.incident.timeline_server        # stdio, for an MCP client

**No `aioc.contracts` import anywhere in this file.** The MCP boundary is JSON Schema, not
Pydantic (contract sec 6), so the enum members and the input schema below are written out
longhand. That duplication is the point: it is what makes this server independently
deployable and stops a reasoning-layer refactor from breaking a tool. `tests/test_timeline_tool.py`
asserts the longhand lists still match the Python enums, so the copies cannot drift silently.

Two implementation notes worth knowing before changing this file:

**Input validation is done by hand, with the framework's turned off.** The MCP server's
default `validate_input=True` runs JSON Schema and, on failure, returns a *plain text* error.
The contract requires a schema failure to come back as a structured `validation` error with
`details.field` and `details.expected` (sec 6.1, sec 6.4). Those are incompatible, so
`validate_input=False` and `_validate` produces the contract-shaped error.

**The transient error code deviates from sec 7.1, deliberately and additively.** Section 7.1
lists `PROMETHEUS_TIMEOUT` for this tool, written when the timeline was expected to be derived
from Prometheus. The actual source of timeline events is the Postgres corpus, so a timeout here
is a Postgres timeout and calling it `PROMETHEUS_TIMEOUT` would be a false `code` on a field
the contract says is matched programmatically. This server emits `TIMELINE_STORE_TIMEOUT`
instead. Adding a code is additive-optional, which is a patch-level change under sec 0 - it
needs a sec 9 changelog row and both engineers' agreement, and it is flagged here rather than
done silently. `meta.source` reports `postgres` so the caller is never guessing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from aioc.tools.envelope import Timer, err, ok
from aioc.tools.incident.store import dsn as _dsn
from aioc.tools.policy import ground_truth_denied, restricted_names

TOOL_NAME = "get_incident_timeline"

# Longhand copies of the contract enums. Deliberate duplication - see the module docstring.
TIMELINE_EVENT_KINDS = (
    "deploy",
    "alert",
    "config_change",
    "restart",
    "scale",
    "metric_threshold",
    "log_pattern",
    "other",
)
SEVERITIES = ("sev1", "sev2", "sev3", "sev4")

MAX_WINDOW = timedelta(days=7)
DEFAULT_MAX_EVENTS = 50
HARD_MAX_EVENTS = 200

# Severity is ordinal on the wire but a string in the enum, so `min_severity` needs an
# explicit ranking. sev1 is most severe, so a min_severity of sev3 admits sev1-sev3.
_SEVERITY_RANK = {"sev1": 1, "sev2": 2, "sev3": 3, "sev4": 4}


# ------------------------------------------------------------------------ tool description
#
# The four-part template (contract sec 6.5) is frozen and ordered. Part 4 is the one that
# matters - it is the intervention the Day 13/14 routing case study measures.

DESCRIPTION = """\
Returns operational events for ONE service over a time window, ordered oldest first. Events \
are deploys, alerts, config changes, restarts, scale actions, metric threshold crossings, and \
log patterns drawn from the recorded incident history. Inputs: `service` is a Prometheus \
service name such as `checkout-api`; `start` and `end` are RFC 3339 UTC timestamps with an \
explicit Z (`2026-03-02T18:00:00Z`); `event_kinds` and `min_severity` filter the result; \
`max_events` caps it (1-200, default 50).

Example queries this tool answers:
- "What happened to payments-api between 14:00 and 15:00 yesterday?"
- "Was there a deploy to checkout-api before the error spike started?"
- "Show me every config change on inventory-api in the last two days."
- "Which alerts fired on postgres during the outage window?"

Edge cases and limits: the window must be at most 7 days and `end` must be after `start`. \
Returns at most 200 events; when more exist, `meta.truncated` is true and \
`meta.total_available` gives the real count, so narrow the window rather than trusting a \
truncated list. An empty `events` array is a successful answer meaning no events were \
recorded for that service in that window - it is NOT an error and NOT evidence that nothing \
happened, because only recorded events are visible here. This tool covers one service at a \
time; it has no view of unrecorded activity, of metrics themselves, or of other services. \
Chaos-injector signals (any `chaos*` service) are the eval harness's injected ground truth \
and return a `permission` error rather than data.

When to use this vs. the alternative: use `get_incident_timeline` when you need to know WHAT \
HAPPENED AND IN WHAT ORDER for a single service - reconstructing a sequence, or checking \
whether a deploy preceded a symptom. Use `correlate_events` instead when you need to know \
WHICH SIGNALS MOVED TOGETHER across several services around a known point in time. If you are \
asking "what came first", this tool; if you are asking "what moved with what", that one."""


INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "start"],
    "properties": {
        "service": {
            "type": "string",
            "description": "Service name as scraped by Prometheus, e.g. `checkout-api`.",
        },
        "start": {
            "type": "string",
            "description": "Window start, RFC 3339 UTC with an explicit Z.",
        },
        "end": {
            "type": ["string", "null"],
            "default": None,
            "description": "Window end, RFC 3339 UTC. Defaults to now. Must be after `start`.",
        },
        "event_kinds": {
            "type": ["array", "null"],
            "default": None,
            "items": {"type": "string", "enum": list(TIMELINE_EVENT_KINDS)},
            "description": "Restrict to these event kinds. Omit for all kinds.",
        },
        "min_severity": {
            "type": ["string", "null"],
            "default": None,
            "enum": [*SEVERITIES, None],
            "description": (
                "Least severe level to include; sev1 is most severe, so `sev3` admits "
                "sev1-sev3. Omit to include every event, including those with no severity."
            ),
        },
        "max_events": {
            "type": "integer",
            "minimum": 1,
            "maximum": HARD_MAX_EVENTS,
            "default": DEFAULT_MAX_EVENTS,
            "description": f"Cap on returned events, 1-{HARD_MAX_EVENTS}.",
        },
    },
}


# ------------------------------------------------------------------------- input validation


class _Invalid(Exception):
    """A validation failure carrying the field and expectation the contract requires."""

    def __init__(self, field: str, expected: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.expected = expected


def _parse_ts(raw: object, field: str) -> datetime:
    if not isinstance(raw, str):
        raise _Invalid(
            field, "an RFC 3339 UTC string", f"{field} must be a string, got {type(raw).__name__}"
        )
    text = raw.strip()
    # `fromisoformat` accepts `+00:00` but not `Z` on some versions; normalise first so the
    # contract's required `Z` form is always the one that works.
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
    """Normalise and validate the input, raising `_Invalid` with a field and expectation.

    Done by hand rather than by the framework so the failure comes back as the contract's
    structured `validation` error - see the module docstring.
    """
    unknown = set(args) - set(INPUT_SCHEMA["properties"])
    if unknown:
        raise _Invalid(
            sorted(unknown)[0],
            f"one of {sorted(INPUT_SCHEMA['properties'])}",
            f"unknown input field(s): {sorted(unknown)}",
        )

    service = args.get("service")
    if not isinstance(service, str) or not service.strip():
        raise _Invalid("service", "a non-empty service name", "service is required")

    if "start" not in args:
        raise _Invalid("start", "an RFC 3339 UTC timestamp", "start is required")
    start = _parse_ts(args["start"], "start")
    end = _parse_ts(args["end"], "end") if args.get("end") is not None else datetime.now(UTC)

    if end <= start:
        raise _Invalid("end", "a timestamp strictly after `start`", "end must be after start")
    if end - start > MAX_WINDOW:
        raise _Invalid(
            "start",
            f"a window of at most {MAX_WINDOW.days} days",
            f"window spans {(end - start).days} days; the limit is {MAX_WINDOW.days}",
        )

    kinds = args.get("event_kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not kinds:
            raise _Invalid(
                "event_kinds",
                f"a non-empty array of {list(TIMELINE_EVENT_KINDS)}",
                "event_kinds must be a non-empty array",
            )
        bad = [k for k in kinds if k not in TIMELINE_EVENT_KINDS]
        if bad:
            raise _Invalid(
                "event_kinds",
                f"members of {list(TIMELINE_EVENT_KINDS)}",
                f"unknown event kind(s): {bad}",
            )

    min_severity = args.get("min_severity")
    if min_severity is not None and min_severity not in _SEVERITY_RANK:
        raise _Invalid(
            "min_severity", f"one of {list(SEVERITIES)}", f"unknown severity: {min_severity!r}"
        )

    max_events = args.get("max_events", DEFAULT_MAX_EVENTS)
    if not isinstance(max_events, int) or isinstance(max_events, bool):
        raise _Invalid(
            "max_events", f"an integer 1-{HARD_MAX_EVENTS}", "max_events must be an integer"
        )
    if not 1 <= max_events <= HARD_MAX_EVENTS:
        raise _Invalid(
            "max_events",
            f"an integer 1-{HARD_MAX_EVENTS}",
            f"max_events out of range: {max_events}",
        )

    return {
        "service": service.strip(),
        "start": start,
        "end": end,
        "event_kinds": list(kinds) if kinds else None,
        "min_severity": min_severity,
        "max_events": max_events,
    }


# ------------------------------------------------------------------------------ data access


# Connection settings live in `aioc.tools.incident.store`, shared with `correlate_server` -
# both servers read the same corpus, and the DSN logic (notably the port override that the
# handoff's environment-trap section exists for) must not fork between them.

_SELECT = """
SELECT e.id, e.at, e.service, e.description, e.kind, e.kind_detail, e.severity
  FROM incident_timeline_events e
 WHERE e.service = %(service)s
   AND e.at >= %(start)s AND e.at <= %(end)s
   AND (%(kinds)s::text[] IS NULL OR e.kind = ANY(%(kinds)s::text[]))
   AND (%(max_rank)s::int IS NULL
        OR (e.severity IS NOT NULL AND %(ranks)s::jsonb ->> e.severity IS NOT NULL
            AND (%(ranks)s::jsonb ->> e.severity)::int <= %(max_rank)s::int))
 ORDER BY e.at, e.id
 LIMIT %(limit)s
"""

_COUNT = """
SELECT count(*)
  FROM incident_timeline_events e
 WHERE e.service = %(service)s
   AND e.at >= %(start)s AND e.at <= %(end)s
   AND (%(kinds)s::text[] IS NULL OR e.kind = ANY(%(kinds)s::text[]))
   AND (%(max_rank)s::int IS NULL
        OR (e.severity IS NOT NULL AND %(ranks)s::jsonb ->> e.severity IS NOT NULL
            AND (%(ranks)s::jsonb ->> e.severity)::int <= %(max_rank)s::int))
"""

_KNOWN_SERVICE = "SELECT 1 FROM incident_timeline_events WHERE service = %(service)s LIMIT 1"


def _rows_to_events(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Rows to `TimelineEvent` shape. `evidence_id` is null: these are recorded facts from the
    corpus, not observations this tool derived, so there is nothing for it to cite."""
    return [
        {
            "id": row[0],
            "at": row[1].astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "service": row[2],
            "description": row[3],
            "kind": row[4],
            "kind_detail": row[5],
            "severity": row[6],
            "evidence_id": None,
        }
        for row in rows
    ]


def fetch_timeline(params: dict[str, Any], *, dsn: str | None = None) -> types.CallToolResult:
    """Run the query and build the response. Separated from the MCP plumbing so it is
    testable without a server, and so the error mapping is one readable block."""
    import json as _json

    query_args = {
        "service": params["service"],
        "start": params["start"],
        "end": params["end"],
        "kinds": params["event_kinds"],
        "max_rank": _SEVERITY_RANK.get(params["min_severity"]) if params["min_severity"] else None,
        "ranks": _json.dumps(_SEVERITY_RANK),
        "limit": params["max_events"],
    }

    try:
        with Timer() as timer, psycopg.connect(dsn or _dsn(), connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT, query_args)
                rows = cur.fetchall()
                cur.execute(_COUNT, query_args)
                count_row = cur.fetchone()
                total = int(count_row[0]) if count_row else 0
                if not rows and total == 0:
                    # Distinguish "this service has no events in this window" (success, empty)
                    # from "this service does not exist" (business error). Conflating them
                    # tells the agent nothing happened when the truth is it asked the wrong
                    # question, and the agent cannot tell those apart without being told.
                    cur.execute(_KNOWN_SERVICE, {"service": params["service"]})
                    if cur.fetchone() is None:
                        return err(
                            "business",
                            "UNKNOWN_SERVICE",
                            f"No recorded events for a service named {params['service']!r}.",
                            remediation=(
                                "Check the service name against Prometheus. The demo app's "
                                "services are checkout-api, payments-api, and inventory-api; "
                                "the datastores are postgres and redis."
                            ),
                            details={"service": params["service"]},
                        )
    except psycopg.OperationalError as exc:
        # Connection refused, timeout, too many connections - all upstream availability, all
        # genuinely worth retrying. This is the class the contract makes retryable.
        return err(
            "transient",
            "TIMELINE_STORE_TIMEOUT",
            f"Could not reach the incident store ({type(exc).__name__}).",
            retry_after_ms=2000,
            remediation="Retry after 2s. If it persists, the stack may be down - `make up`.",
            details={"store": "postgres"},
        )
    except psycopg.Error as exc:
        return err(
            "transient",
            "TIMELINE_STORE_ERROR",
            f"The incident store rejected the query ({type(exc).__name__}).",
            retry_after_ms=1000,
            remediation="Retry once. A repeat failure is a bug in the tool, not the request.",
        )

    events = _rows_to_events(rows)
    window_start, window_end = params["start"], params["end"]
    return ok(
        {
            "events": events,
            "window": {
                "start": window_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end": window_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            "services_covered": sorted({e["service"] for e in events}),
        },
        returned=len(events),
        truncated=total > len(events),
        total_available=total,
        query_ms=timer.ms,
        source="postgres",
        as_of=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


# ---------------------------------------------------------------------------- MCP plumbing

server = Server("aioc-incident-timeline")


# The two `type: ignore`s below are the mcp library's decorators being untyped, not a looseness
# on our side: both handlers keep full annotations, so everything inside them is still checked.
@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name=TOOL_NAME, description=DESCRIPTION, inputSchema=INPUT_SCHEMA)]


# validate_input=False on purpose: the framework's own validation returns a plain-text error,
# and the contract requires a structured `validation` error. See the module docstring.
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
            "INVALID_TIME_RANGE" if exc.field in {"start", "end"} else "INVALID_INPUT",
            str(exc),
            remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
            details={"field": exc.field, "expected": exc.expected},
        )
    # The chaos namespace is the eval's injected ground truth (Day 7, shared policy). This
    # is a `permission` error, not `UNKNOWN_SERVICE`: the signals exist, the caller lacks
    # the scope, and telling an agent "no such service" would be a disprovable lie.
    restricted = restricted_names([params["service"]])
    if restricted:
        return ground_truth_denied("service", restricted)
    # psycopg is synchronous; run it off the event loop so a slow query cannot stall the
    # server's other traffic.
    return await asyncio.to_thread(fetch_timeline, params)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
