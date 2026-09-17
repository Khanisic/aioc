"""`analyze_logs` and `analyze_events` as one stdio MCP server (Day 13, contract sec 7.5 / 7.6).

    uv run python -m aioc.tools.incident.analyze_server        # stdio, for an MCP client

**These two tools overlap on purpose.** They are the independent variable of the Domain 2
routing case study (BUILD_PLAN.md Phase 2, CONTRACTS.md sec 0 "the one pre-authorized
exception"). Both take the same `service` / `start` / `end` / `pattern` / `max_matches`
inputs, both return `matches` + `total_matched` + `patterns_detected`, and their part-4
lines are the contract's v1.0.0 text verbatim: "Use this to analyze service output over a
time window" versus "Use this to analyze service activity over a time window". Nothing in
either description names the other tool or states the discriminator. Day 13 records how
often a model given both picks the wrong one across twenty queries
(`scripts/check_tool_routing.py`); Day 14 splits and renames them under the pre-authorized
`1.1.0` bump and re-runs the same twenty (`aioc.tools.incident.search_server`, which
shares this module's implementation under the new names and is the shipped server; this
one stays runnable as the baseline). **Do not sharpen these descriptions here** - the
`.claude/rules/tools.md` rule says so, and `tests/test_analyze_tools.py` pins the weak
part 4 so a helpful edit fails the suite instead of silently destroying the baseline.

What is *not* deliberately weak: the data. Each tool is a real source, with real limits
stated in part 3 of its description.

- `analyze_logs` reads what the service's container actually printed, through
  ``docker compose logs`` (`aioc.tools.incident.logs`). No log collector exists in this
  stack, so the container buffer is the honest source; a recreated container starts empty.
- `analyze_events` reads the recorded operational events - deploys, alerts, config changes,
  restarts - from the seeded incident corpus, the same table `get_incident_timeline` reads.

The two are different *kinds* of data (what a process wrote versus what an operator or a
system recorded happening to it), which is exactly the discriminator the v1 descriptions
withhold and the v1.1 descriptions will state.

Conventions carried over from the other servers, all deliberate: **no `aioc.contracts`
import** (the MCP boundary is JSON Schema, contract sec 6 - the enum copies are longhand
and a test pins them against the Python enums); **framework input validation off**, because
the contract needs a structured `validation` error with `details.field` / `details.expected`
and the framework returns plain text; the chaos ground-truth gate on `service`; and the
four-class error taxonomy through `aioc.tools.envelope`.

Error codes: `INVALID_PATTERN` (validation), `UNKNOWN_SERVICE` (business),
`LOG_STORE_UNAVAILABLE` / `EVENT_STORE_UNAVAILABLE` (transient) are the sec 7.5 / 7.6
codes. `INVALID_TIME_RANGE`, `INVALID_INPUT`, `UNKNOWN_TOOL`, and `CHAOS_SCOPE_REQUIRED` are
the ones every server in this package already emits.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from aioc.tools.envelope import Timer, err, ok
from aioc.tools.incident.logs import LOG_LEVELS, ComposeLogStore, LogStore, LogStoreError
from aioc.tools.incident.patterns import detect_patterns
from aioc.tools.incident.store import dsn as _dsn
from aioc.tools.incident.timeline_server import TIMELINE_EVENT_KINDS, _parse_ts
from aioc.tools.policy import ground_truth_denied, restricted_names

SERVER_NAME = "aioc-analyze"
TOOL_NAMES = ("analyze_logs", "analyze_events")

MAX_WINDOW = timedelta(days=7)
DEFAULT_MAX_MATCHES = 100
HARD_MAX_MATCHES = 500
MAX_PATTERN_LENGTH = 200
EVENT_SCAN_LIMIT = 5000

# Contract sec 7.5 / 7.6, verbatim. The whole of part 4 in v1.0.0; deliberately no
# alternative named and no discriminator stated. Pinned by the tests.
V1_PART_FOUR = {
    "analyze_logs": "Use this to analyze service output over a time window.",
    "analyze_events": "Use this to analyze service activity over a time window.",
}


# ----------------------------------------------------------------------- tool descriptions
#
# Parts 1-3 follow the sec 6.5 template in full. Part 4 is the case-study variable and is
# the contract's v1 sentence and nothing more.

LOGS_DESCRIPTION = f"""\
Searches the output a service's container printed during a time window - request lines, \
warnings, errors, tracebacks, startup messages: whatever the process wrote to stdout or \
stderr. Inputs: `service` is the compose service name such as `checkout-api`; `start` and \
`end` are RFC 3339 UTC timestamps with an explicit Z (`2026-03-02T18:00:00Z`), `end` \
defaulting to now; `pattern` is an optional case-sensitive regular expression matched \
against each line's message; `level` filters to one of {", ".join(LOG_LEVELS)}; \
`max_matches` caps the returned lines (1-{HARD_MAX_MATCHES}, default {DEFAULT_MAX_MATCHES}).

Example queries this tool answers:
- "Show me every ERROR line payments-api printed between 14:00 and 14:15 UTC."
- "How many times did 'connection refused' appear in checkout-api's output during the outage?"
- "Find the traceback inventory-api emitted when it started returning 500s."
- "Which request paths were getting 502s in checkout-api's access log after 14:05?"

Edge cases and limits: the window must be at most 7 days and `end` must be after `start`. \
Only the container's current log buffer is visible - a container that was recreated starts \
with an empty buffer, so nothing printed before the last recreate can be found here. At most \
50,000 lines are scanned per call and at most {HARD_MAX_MATCHES} matches returned; when \
either cut applies, `meta.truncated` is true and `total_matched` counts every matching line \
that was scanned, so narrow the window rather than trusting a truncated list. `level` is \
read from the line's leading level word; a line without one (a traceback body, a banner) is \
`other`, so a `level: error` filter returns the error line but not the traceback under it. \
`patterns_detected` groups matching lines by template (identifiers, addresses, and long \
numbers replaced by placeholders) so a repeated message counts as one pattern. An empty \
`matches` array is a successful answer meaning nothing the service printed in that window \
matched - it is NOT an error and NOT evidence that nothing happened to the service, because \
a deploy, a restart, or a config change is not something a process prints about itself. \
Chaos-injector signals (any `chaos*` service) are the eval harness's injected ground truth \
and return a `permission` error rather than data.

When to use this vs the alternative: {V1_PART_FOUR["analyze_logs"]}"""


EVENTS_DESCRIPTION = f"""\
Searches the recorded operational events for a service during a time window - deploys, \
alerts, config changes, restarts, scale actions, metric threshold crossings, and log \
patterns that were recorded into the incident history. Inputs: `service` is the service \
name as scraped by Prometheus such as `checkout-api`; `start` and `end` are RFC 3339 UTC \
timestamps with an explicit Z (`2026-03-02T18:00:00Z`), `end` defaulting to now; `pattern` \
is an optional case-sensitive regular expression matched against each event's description; \
`kind` filters to one of {", ".join(TIMELINE_EVENT_KINDS)}; `max_matches` caps the returned \
events (1-{HARD_MAX_MATCHES}, default {DEFAULT_MAX_MATCHES}).

Example queries this tool answers:
- "Was there a deploy or a config change on checkout-api in the hour before the alert?"
- "List every restart recorded for payments-api last week."
- "Which alerts fired on inventory-api on 2026-01-22?"
- "Did anything change on postgres around 15:10 UTC?"

Edge cases and limits: the window must be at most 7 days and `end` must be after `start`. \
Only events that were recorded are visible - this is the incident history, not the \
service's own output, so a symptom nobody wrote down is not here. At most {HARD_MAX_MATCHES} \
matches are returned; when more exist, `meta.truncated` is true and `total_matched` gives \
the real count. `patterns_detected` groups matching events by description template so a \
repeated alert counts as one pattern with a first and last time. An empty `matches` array \
is a successful answer meaning no recorded event for that service in that window matched - \
it is NOT an error and NOT proof that nothing happened, only that nothing was recorded. \
Chaos-injector signals (any `chaos*` service) are the eval harness's injected ground truth \
and return a `permission` error rather than data.

When to use this vs the alternative: {V1_PART_FOUR["analyze_events"]}"""

DESCRIPTIONS = {"analyze_logs": LOGS_DESCRIPTION, "analyze_events": EVENTS_DESCRIPTION}


def _common_properties(service_hint: str) -> dict[str, Any]:
    return {
        "service": {"type": "string", "description": service_hint},
        "start": {
            "type": "string",
            "description": "Window start, RFC 3339 UTC with an explicit Z.",
        },
        "end": {
            "type": ["string", "null"],
            "default": None,
            "description": "Window end, RFC 3339 UTC. Defaults to now. Must be after `start`.",
        },
        "pattern": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "Optional regular expression (Python syntax, case-sensitive) each match "
                "must satisfy. Omit to return everything in the window."
            ),
        },
        "max_matches": {
            "type": "integer",
            "minimum": 1,
            "maximum": HARD_MAX_MATCHES,
            "default": DEFAULT_MAX_MATCHES,
            "description": f"Cap on returned matches, 1-{HARD_MAX_MATCHES}.",
        },
    }


LOGS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "start"],
    "properties": {
        **_common_properties("Compose service name, e.g. `checkout-api`."),
        "level": {
            "type": ["string", "null"],
            "default": None,
            "enum": [*LOG_LEVELS, None],
            "description": "Restrict to lines at this level. Omit for every level.",
        },
    },
}

EVENTS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "start"],
    "properties": {
        **_common_properties("Service name as scraped by Prometheus, e.g. `checkout-api`."),
        "kind": {
            "type": ["string", "null"],
            "default": None,
            "enum": [*TIMELINE_EVENT_KINDS, None],
            "description": "Restrict to events of this kind. Omit for every kind.",
        },
    },
}

INPUT_SCHEMAS = {"analyze_logs": LOGS_INPUT_SCHEMA, "analyze_events": EVENTS_INPUT_SCHEMA}


# ------------------------------------------------------------------------- input validation


class _Invalid(Exception):
    def __init__(self, field: str, expected: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.expected = expected


def _validate(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Normalise and validate the input for either tool, raising `_Invalid` with the field
    and expectation the contract's structured `validation` error needs."""
    schema = INPUT_SCHEMAS[name]
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise _Invalid(
            sorted(unknown)[0],
            f"one of {sorted(schema['properties'])}",
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

    pattern = args.get("pattern")
    compiled: re.Pattern[str] | None = None
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern.strip():
            raise _Invalid("pattern", "a non-empty regular expression", "pattern must be text")
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise _Invalid(
                "pattern",
                f"a regular expression of at most {MAX_PATTERN_LENGTH} characters",
                f"pattern is {len(pattern)} characters long",
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise _Invalid(
                "pattern",
                "a valid Python regular expression",
                f"pattern does not compile: {exc}",
            ) from exc

    max_matches = args.get("max_matches", DEFAULT_MAX_MATCHES)
    if not isinstance(max_matches, int) or isinstance(max_matches, bool):
        raise _Invalid(
            "max_matches", f"an integer 1-{HARD_MAX_MATCHES}", "max_matches must be an integer"
        )
    if not 1 <= max_matches <= HARD_MAX_MATCHES:
        raise _Invalid(
            "max_matches",
            f"an integer 1-{HARD_MAX_MATCHES}",
            f"max_matches out of range: {max_matches}",
        )

    params: dict[str, Any] = {
        "service": service.strip(),
        "start": start,
        "end": end,
        "pattern": compiled,
        "max_matches": max_matches,
    }
    if name == "analyze_logs":
        level = args.get("level")
        if level is not None and level not in LOG_LEVELS:
            raise _Invalid("level", f"one of {list(LOG_LEVELS)}", f"unknown level: {level!r}")
        params["level"] = level
    else:
        kind = args.get("kind")
        if kind is not None and kind not in TIMELINE_EVENT_KINDS:
            raise _Invalid(
                "kind", f"one of {list(TIMELINE_EVENT_KINDS)}", f"unknown event kind: {kind!r}"
            )
        params["kind"] = kind
    return params


def _validation_error(exc: _Invalid) -> types.CallToolResult:
    if exc.field == "pattern":
        code = "INVALID_PATTERN"
    elif exc.field in {"start", "end"}:
        code = "INVALID_TIME_RANGE"
    else:
        code = "INVALID_INPUT"
    return err(
        "validation",
        code,
        str(exc),
        remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
        details={"field": exc.field, "expected": exc.expected},
    )


# ---------------------------------------------------------------------------- analyze_logs


def analyze_logs(
    params: dict[str, Any],
    *,
    store: LogStore | None = None,
    events_tool: str = "analyze_events",
) -> types.CallToolResult:
    """Read the window from the log store, filter, cap, and detect patterns.

    ``events_tool`` is the name the `UNKNOWN_SERVICE` remediation points at - this tool's
    sibling on the same server. The Day 14 `1.1.0` server (`search_server`) shares this
    implementation under new names and passes its own."""
    store = store if store is not None else ComposeLogStore.from_settings()
    service = params["service"]
    try:
        with Timer() as timer:
            lines, scan_truncated = store.read(service, params["start"], params["end"])
    except LogStoreError as exc:
        if exc.failure == "unknown_service":
            return err(
                "business",
                "UNKNOWN_SERVICE",
                f"No container logs for a service named {service!r}: {exc}.",
                remediation=(
                    "Check the service name against the compose file. The demo app's "
                    "services are checkout-api, payments-api, and inventory-api; the "
                    f"datastores are postgres and redis. Use `{events_tool}` for recorded "
                    "operational events if the service has no container here."
                ),
                details={"service": service},
            )
        return err(
            "transient",
            "LOG_STORE_UNAVAILABLE",
            f"Could not read container logs ({exc.failure}): {exc}.",
            retry_after_ms=2000,
            remediation=(
                "Retry after 2s. If it persists, Docker is not running or the stack is "
                "down - `docker compose up -d --wait`."
            ),
            details={"store": "docker_compose_logs", "reason": exc.failure},
        )

    level = params["level"]
    pattern: re.Pattern[str] | None = params["pattern"]
    matched = [
        line
        for line in lines
        if (level is None or line.level == level)
        and (pattern is None or pattern.search(line.message) is not None)
    ]
    shown = matched[: params["max_matches"]]
    return ok(
        {
            "matches": [
                {
                    "at": _iso(line.at),
                    "service": service,
                    "level": line.level,
                    "message": line.message,
                    "source_ref": f"compose:{service}:{line.line_no}",
                }
                for line in shown
            ],
            "total_matched": len(matched),
            "patterns_detected": detect_patterns((line.at, line.message) for line in matched),
        },
        returned=len(shown),
        truncated=scan_truncated or len(matched) > len(shown),
        total_available=len(matched),
        query_ms=timer.ms,
        source="docker_compose_logs",
        as_of=_iso(datetime.now(UTC)),
    )


# -------------------------------------------------------------------------- analyze_events

_SELECT_EVENTS = """
SELECT e.id, e.at, e.service, e.description, e.kind
  FROM incident_timeline_events e
 WHERE e.service = %(service)s
   AND e.at >= %(start)s AND e.at <= %(end)s
   AND (%(kind)s::text IS NULL OR e.kind = %(kind)s::text)
 ORDER BY e.at, e.id
 LIMIT %(limit)s
"""

_KNOWN_SERVICE = "SELECT 1 FROM incident_timeline_events WHERE service = %(service)s LIMIT 1"


def analyze_events(params: dict[str, Any], *, dsn: str | None = None) -> types.CallToolResult:
    """Read the window from the corpus, filter by pattern, cap, and detect patterns."""
    service = params["service"]
    query_args = {
        "service": service,
        "start": params["start"],
        "end": params["end"],
        "kind": params["kind"],
        "limit": EVENT_SCAN_LIMIT,
    }
    try:
        with Timer() as timer, psycopg.connect(dsn or _dsn(), connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_EVENTS, query_args)
                rows = cur.fetchall()
                if not rows:
                    # Same distinction the timeline server draws: nothing in the window for
                    # a known service is an empty success; a service the corpus has never
                    # heard of is a business error the agent must not read as "quiet".
                    cur.execute(_KNOWN_SERVICE, {"service": service})
                    if cur.fetchone() is None:
                        return err(
                            "business",
                            "UNKNOWN_SERVICE",
                            f"No recorded events for a service named {service!r}.",
                            remediation=(
                                "Check the service name against Prometheus. The demo app's "
                                "services are checkout-api, payments-api, and inventory-api; "
                                "the datastores are postgres and redis."
                            ),
                            details={"service": service},
                        )
    except psycopg.Error as exc:
        return err(
            "transient",
            "EVENT_STORE_UNAVAILABLE",
            f"Could not read the incident store ({type(exc).__name__}).",
            retry_after_ms=2000,
            remediation="Retry after 2s. If it persists, the stack may be down - `make up`.",
            details={"store": "postgres"},
        )

    pattern: re.Pattern[str] | None = params["pattern"]
    matched = [row for row in rows if pattern is None or pattern.search(row[3]) is not None]
    shown = matched[: params["max_matches"]]
    return ok(
        {
            "matches": [
                {
                    "at": _iso(row[1]),
                    "service": row[2],
                    "kind": row[4],
                    "description": row[3],
                    "source_ref": row[0],
                }
                for row in shown
            ],
            "total_matched": len(matched),
            "patterns_detected": detect_patterns(
                (row[1].astimezone(UTC), row[3]) for row in matched
            ),
        },
        returned=len(shown),
        truncated=len(matched) > len(shown),
        total_available=len(matched),
        query_ms=timer.ms,
        source="postgres",
        as_of=_iso(datetime.now(UTC)),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------- MCP plumbing

server = Server(SERVER_NAME)


@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=DESCRIPTIONS[name], inputSchema=INPUT_SCHEMAS[name])
        for name in TOOL_NAMES
    ]


def call(
    name: str,
    arguments: dict[str, Any],
    *,
    store: LogStore | None = None,
    dsn: str | None = None,
) -> types.CallToolResult:
    """Dispatch one call: validate, apply the chaos gate, run the tool. Synchronous so the
    tests drive it without a server; `call_tool` is the async wire wrapper."""
    if name not in TOOL_NAMES:
        return err(
            "validation",
            "UNKNOWN_TOOL",
            f"This server exposes only {', '.join(TOOL_NAMES)}.",
            remediation=f"Call one of {', '.join(TOOL_NAMES)} instead.",
            details={"field": "name", "expected": list(TOOL_NAMES), "received": name},
        )
    try:
        params = _validate(name, arguments or {})
    except _Invalid as exc:
        return _validation_error(exc)
    # The chaos namespace is the eval's injected ground truth (shared policy): a
    # `permission` error, not `UNKNOWN_SERVICE` - the signals exist, the caller lacks scope.
    restricted = restricted_names([params["service"]])
    if restricted:
        return ground_truth_denied("service", restricted)
    if name == "analyze_logs":
        return analyze_logs(params, store=store)
    return analyze_events(params, dsn=dsn)


# validate_input=False on purpose: the framework's own validation returns a plain-text
# error, and the contract requires a structured `validation` error (module docstring).
@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    # Both stores are synchronous (a subprocess, psycopg); run off the event loop so a slow
    # read cannot stall the server's other traffic.
    return await asyncio.to_thread(call, name, arguments or {})


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
