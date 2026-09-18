"""`search_container_logs` and `search_recorded_events` as one stdio MCP server (Day 14,
contract sec 7.5 / 7.6 at `1.1.0`).

    uv run python -m aioc.tools.incident.search_server         # stdio, for an MCP client

**This is the "after" of the routing case study.** `analyze_logs` and `analyze_events`
(`aioc.tools.incident.analyze_server`, contract `1.0.0`) shipped deliberately overlapping:
the same inputs, the same output shape, and a part 4 that named no alternative. This server
is the pre-authorized `1.1.0` split (CONTRACTS.md sec 0; the dated rationale is in
`docs/design-notes/contract-changes.md`): the two tools are renamed so the name itself
carries the discriminator, and part 4 of each description names the other tool and states
when to use which. Nothing else moves - parts 1-3 of each description are the v1 text
verbatim (asserted by a test), the input schemas are the v1 schemas, and the
implementations are imported from the v1 module, so the case study's independent variable
is exactly the name plus part 4. `scripts/check_tool_routing.py --variant v1_1` lists this
server over the wire and re-runs the same forty queries; the v1 module stays runnable so the
baseline can be re-measured on the same wire.

The discriminator, in one line: `search_container_logs` reads what the service's process
*wrote* (its stdout/stderr through ``docker compose logs``); `search_recorded_events` reads
what was *recorded as happening to* the service (deploys, restarts, alerts, config changes,
from the seeded incident history). A question is routed by the kind of data it needs, not by
the word it uses - "the deploy log" is a question about deploys.

Conventions are the v1 module's: no `aioc.contracts` import (the MCP boundary is JSON
Schema, contract sec 6), framework input validation off in favour of the structured
`validation` error, the chaos ground-truth gate on `service`, and the four-class taxonomy
through `aioc.tools.envelope`. Error codes are unchanged from sec 7.5 / 7.6.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from aioc.tools.envelope import err
from aioc.tools.incident import analyze_server as v1
from aioc.tools.incident.logs import LogStore
from aioc.tools.policy import ground_truth_denied, restricted_names

SERVER_NAME = "aioc-search"
TOOL_NAMES = ("search_container_logs", "search_recorded_events")

# The 1.1.0 name -> the 1.0.0 name whose implementation, input schema, and parts 1-3 it keeps.
V1_NAMES = {
    "search_container_logs": "analyze_logs",
    "search_recorded_events": "analyze_events",
}

# Contract sec 7.5 / 7.6 at 1.1.0: part 4 names the alternative and states the discriminator.
PART_FOUR = {
    "search_container_logs": (
        "When to use this vs `search_recorded_events`: use this tool when the question needs "
        "what the service's process itself wrote - lines, messages, tracebacks, request "
        "entries, anything on stdout or stderr. A deploy, a rollback, a restart, a scale "
        "action, a config change, or an alert is something recorded *about* the service, not "
        "something it printed; for those use `search_recorded_events`. The kind of data the "
        "question needs decides, not the word it uses: 'the deploy log' is a question about "
        "deploys."
    ),
    "search_recorded_events": (
        "When to use this vs `search_container_logs`: use this tool when the question needs "
        "what was recorded as happening *to* the service - deploys, rollbacks, restarts, "
        "scale actions, config changes, alerts, threshold crossings - whether the question "
        "calls that a log, an output, a history, or a record. The text a process printed, its "
        "tracebacks, and its request lines are not events unless someone recorded them; for "
        "those use `search_container_logs`. The kind of data the question needs decides, not "
        "the word it uses: 'what it printed after the deploy' is a question about printed "
        "lines."
    ),
}

_PART_FOUR_MARKER = "When to use this vs"


def parts_one_to_three(description: str) -> str:
    """Everything before part 4 of a sec 6.5 description, trailing whitespace stripped."""
    return description[: description.index(_PART_FOUR_MARKER)].rstrip()


# Parts 1-3 are the v1 text verbatim; only the name and part 4 differ. Composed rather than
# copied so the "only the variable changed" claim is true by construction (and pinned by a
# test in case someone later inlines an edit here).
DESCRIPTIONS = {
    name: f"{parts_one_to_three(v1.DESCRIPTIONS[V1_NAMES[name]])}\n\n{PART_FOUR[name]}"
    for name in TOOL_NAMES
}

INPUT_SCHEMAS = {name: v1.INPUT_SCHEMAS[V1_NAMES[name]] for name in TOOL_NAMES}


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
    """Dispatch one call: validate, apply the chaos gate, run the v1 implementation under
    the 1.1.0 name. Synchronous so the tests drive it without a server."""
    if name not in TOOL_NAMES:
        return err(
            "validation",
            "UNKNOWN_TOOL",
            f"This server exposes only {', '.join(TOOL_NAMES)}.",
            remediation=f"Call one of {', '.join(TOOL_NAMES)} instead.",
            details={"field": "name", "expected": list(TOOL_NAMES), "received": name},
        )
    v1_name = V1_NAMES[name]
    try:
        params = v1._validate(v1_name, arguments or {})
    except v1._Invalid as exc:
        return v1._validation_error(exc)
    restricted = restricted_names([params["service"]])
    if restricted:
        return ground_truth_denied("service", restricted)
    if v1_name == "analyze_logs":
        return v1.analyze_logs(params, store=store, events_tool="search_recorded_events")
    return v1.analyze_events(params, dsn=dsn)


# validate_input=False on purpose: the framework's own validation returns a plain-text
# error, and the contract requires a structured `validation` error (v1 module docstring).
@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    return await asyncio.to_thread(call, name, arguments or {})


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
