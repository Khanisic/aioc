"""The Domain 2 routing case study: how often does a model pick the wrong one of two
overlapping tools? (Day 13 records the baseline; Day 14 re-runs the same queries after
the split.)

    uv run python scripts/check_tool_routing.py                 # 40 live calls, both sets
    uv run python scripts/check_tool_routing.py --set plain     # 20 calls
    uv run python scripts/check_tool_routing.py --dry-run       # zero calls: print the sets
    uv run python scripts/check_tool_routing.py --case logs_traceback --case hard_events_log_word
    uv run python scripts/check_tool_routing.py --variant v1_1  # Day 14, once it exists

Cost: one Claude call per query - twenty per set, forty for both - each a short prompt (the
two tool descriptions plus one sentence), about 2.9k input tokens a call. The tool is never
executed: the routing decision is read off the `tool_use` block and the call ends there.

**What is measured.** `analyze_logs` and `analyze_events` (CONTRACTS.md sec 7.5 / 7.6) are
built to overlap: same inputs, same output shape, and part 4 of each description is the
contract's deliberately weak v1.0.0 sentence with no alternative named. The model is given
both, told to pick the single tool that answers the operator's question, and forced to
pick one (`tool_choice: any`). Every query has a ground truth that follows from the *kind
of data* it needs - what the process printed (logs) versus what was recorded as happening
to it (events) - decided when the query was written, not from which word it uses. The
misrouting rate is the fraction picked wrong.

**Two query sets, and why there are two.** The `plain` set is twenty operator questions
phrased the way an operator would phrase them. It was run first and came back 0/20
misrouted: with parts 1 and 3 of the descriptions stating what each tool reads, a query
whose vocabulary matches ("printed", "traceback", "alert fired", "recorded") never reached
part 4. That is a real finding, and it says the plain set cannot measure part 4. The `hard`
set was designed *after* that result and *before* it was run: the same twenty ground
truths, but each query's surface vocabulary leans the wrong way - "the deploy log", "the
operational output", "the error events it emitted", "analyze its activity" - so the tool
name and the words in part 4 pull against the data the question actually needs. That is
the case where a discriminator sentence is supposed to earn its keep. Both sets are
recorded on every run; the case study reports both.

**The query sets are the constant.** Day 14 changes the tool descriptions and nothing
else, so the before/after numbers compare like with like. Each set is hashed into every run
record (`queries_sha256`); a Day 14 record whose hashes differ is not a comparison. Do not
edit a set after its baseline is recorded - add a new set under a new name instead.

**The descriptions come off the wire.** The tool specs are listed from the real stdio
server of the chosen variant, not copied into this script, so what the model sees is
exactly what an agent would see. The variant registry is the one place Day 14 adds to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.llm import LLMClient, LLMSettings, McpStdioToolset, ToolSpec  # noqa: E402

ToolName = Literal["analyze_logs", "analyze_events"]

# variant key -> the stdio server module whose tool descriptions are under test.
VARIANTS: dict[str, str] = {
    "v1": "aioc.tools.incident.analyze_server",
}

ROUTER_SYSTEM_PROMPT = """\
You are the tool router for an incident-response agent. An operator has asked one question.
Choose the single tool that answers it and call that tool with the arguments the question
implies. Do not answer in prose and do not call more than one tool. If a timestamp is not
fully specified, assume today in UTC."""


@dataclass(frozen=True)
class RoutingCase:
    key: str
    query: str
    expected: ToolName
    why: str


# Ten of each. Ground truth is the data the question needs, decided before any model saw
# the set. Neither tool name appears in any query.
PLAIN_CASES: tuple[RoutingCase, ...] = (
    # ---- what the process printed -> analyze_logs
    RoutingCase(
        "logs_error_lines",
        "Show me every ERROR line payments-api printed between 14:00 and 14:15 UTC today.",
        "analyze_logs",
        "lines the process wrote, at a level",
    ),
    RoutingCase(
        "logs_traceback",
        "Find the traceback inventory-api emitted when it started returning 500s around 09:40 UTC.",
        "analyze_logs",
        "a traceback exists only in the process output",
    ),
    RoutingCase(
        "logs_count_phrase",
        "How many times did 'connection refused' appear in checkout-api's output during the "
        "last hour?",
        "analyze_logs",
        "a phrase count over output text",
    ),
    RoutingCase(
        "logs_request_paths",
        "Which request paths were getting 502 responses in checkout-api's access log after "
        "14:05 UTC?",
        "analyze_logs",
        "access-log lines carry the path and status",
    ),
    RoutingCase(
        "logs_stderr_startup",
        "What did payments-api write to stderr when it booted at 08:00 UTC this morning?",
        "analyze_logs",
        "stderr is process output",
    ),
    RoutingCase(
        "logs_warning_pattern",
        "Is inventory-api repeatedly warning about a slow downstream call? Look at the last "
        "30 minutes.",
        "analyze_logs",
        "a repeated warning line is a log pattern",
    ),
    RoutingCase(
        "logs_exception_text",
        "What was the exact exception message checkout-api raised at 14:07:12 UTC?",
        "analyze_logs",
        "the exact text lives in the output",
    ),
    RoutingCase(
        "logs_latency_lines",
        "Pull the lines where payments-api reported a request taking longer than 2 seconds "
        "between 13:50 and 14:20 UTC.",
        "analyze_logs",
        "per-request lines are output",
    ),
    RoutingCase(
        "logs_health_probe",
        "Did checkout-api print anything unusual around its /healthz probes between 15:00 "
        "and 15:10 UTC?",
        "analyze_logs",
        "what it printed",
    ),
    RoutingCase(
        "logs_grep_sha",
        "Grep payments-api's output from the last two hours for the string 'NoneType'.",
        "analyze_logs",
        "a grep over output",
    ),
    # ---- what was recorded as happening to the service -> analyze_events
    RoutingCase(
        "events_deploy_before_alert",
        "Was there a deploy or config change on checkout-api in the hour before the 15:12 "
        "UTC alert on 2026-01-22?",
        "analyze_events",
        "deploys and config changes are recorded operational events",
    ),
    RoutingCase(
        "events_restarts_week",
        "List every restart recorded for payments-api during the week of 2026-02-16.",
        "analyze_events",
        "restarts are recorded events",
    ),
    RoutingCase(
        "events_alerts_day",
        "Which alerts fired on inventory-api on 2026-01-22?",
        "analyze_events",
        "alerts are recorded events, not process output",
    ),
    RoutingCase(
        "events_scale_actions",
        "Did anyone scale postgres up or down between 03:00 and 06:00 UTC on 2026-03-02?",
        "analyze_events",
        "scale actions are recorded events",
    ),
    RoutingCase(
        "events_config_changes",
        "Show me the recorded config changes on checkout-api in the last two days.",
        "analyze_events",
        "config changes are recorded events",
    ),
    RoutingCase(
        "events_rollback_sequence",
        "When was checkout-api rolled back on 2026-01-22, and what was recorded just before it?",
        "analyze_events",
        "a rollback is a deploy event in the history",
    ),
    RoutingCase(
        "events_threshold_crossings",
        "Which metric thresholds did payments-api cross on 2026-02-19 between 14:00 and 15:00 UTC?",
        "analyze_events",
        "threshold crossings are recorded events",
    ),
    RoutingCase(
        "events_what_changed",
        "Did anything change on redis around 15:10 UTC on 2026-04-08 - a deploy, a restart, "
        "a config push?",
        "analyze_events",
        "changes to the service are recorded events",
    ),
    RoutingCase(
        "events_incident_history",
        "According to the incident history, what happened to inventory-api on the afternoon "
        "of 2026-05-05?",
        "analyze_events",
        "the incident history is the event store",
    ),
    RoutingCase(
        "events_deploy_identity",
        "Which release was deployed to checkout-api at 15:11 UTC on 2026-01-22, per the "
        "recorded timeline?",
        "analyze_events",
        "the deploy event carries the release",
    ),
)

# The same twenty ground truths with the surface vocabulary pulling the wrong way. Written
# after the plain set measured 0/20 and before this set was run - see the module docstring.
HARD_CASES: tuple[RoutingCase, ...] = (
    # ---- needs process output, worded like the event store -> analyze_logs
    RoutingCase(
        "hard_logs_events_word",
        "Show me the error events payments-api emitted between 14:00 and 14:15 UTC today.",
        "analyze_logs",
        "'emitted' - what the process wrote, despite 'events'",
    ),
    RoutingCase(
        "hard_logs_timeline_word",
        "Build a timeline of everything checkout-api printed around the 502 burst at 14:07 UTC.",
        "analyze_logs",
        "printed output, despite 'timeline'",
    ),
    RoutingCase(
        "hard_logs_history_word",
        "Search checkout-api's history from the last hour for 'connection refused' and tell "
        "me how often it recurs.",
        "analyze_logs",
        "a phrase recurrence in output, despite 'history'",
    ),
    RoutingCase(
        "hard_logs_saying",
        "Why did inventory-api start throwing NoneType errors at 09:40 UTC - show me what "
        "it was saying.",
        "analyze_logs",
        "'what it was saying' is output; no cue word at all",
    ),
    RoutingCase(
        "hard_logs_activity_word",
        "Analyze payments-api's activity between 13:50 and 14:20 UTC and pull any request "
        "that took over 2 seconds.",
        "analyze_logs",
        "per-request lines are output, despite 'activity' (the events v1 part-4 word)",
    ),
    RoutingCase(
        "hard_logs_recorded_word",
        "What did checkout-api record on stdout during its 08:00 UTC boot?",
        "analyze_logs",
        "stdout is output, despite 'record'",
    ),
    RoutingCase(
        "hard_logs_alert_word",
        "The 14:12 alert says 5xx spiked on checkout-api; find the exception text behind "
        "those 5xx responses.",
        "analyze_logs",
        "exception text is output, despite the alert framing",
    ),
    RoutingCase(
        "hard_logs_change_word",
        "Did anything change in what payments-api was printing after 14:05 UTC compared "
        "with before?",
        "analyze_logs",
        "printed output, despite 'change'",
    ),
    RoutingCase(
        "hard_logs_incident_word",
        "For the incident at 15:00 UTC, get the raw lines inventory-api wrote in the ten "
        "minutes before.",
        "analyze_logs",
        "raw lines it wrote, despite 'incident'",
    ),
    RoutingCase(
        "hard_logs_deploy_word",
        "Right after the 15:11 deploy of checkout-api, what warnings and errors did the new "
        "process print?",
        "analyze_logs",
        "what the process printed, despite the deploy framing",
    ),
    # ---- needs the recorded history, worded like log output -> analyze_events
    RoutingCase(
        "hard_events_log_word",
        "Check the deploy log for checkout-api on 2026-01-22 - was anything pushed between "
        "15:00 and 15:30 UTC?",
        "analyze_events",
        "deploys are recorded events, despite 'log'",
    ),
    RoutingCase(
        "hard_events_output_word",
        "Summarise the operational output for payments-api during the week of 2026-02-16: "
        "restarts, scale-ups, config pushes.",
        "analyze_events",
        "restarts and config pushes are events, despite 'output' (the logs v1 part-4 word)",
    ),
    RoutingCase(
        "hard_events_change_log",
        "Pull the change log for inventory-api's configuration between 2026-03-01 and 2026-03-03.",
        "analyze_events",
        "config changes are events, despite 'log'",
    ),
    RoutingCase(
        "hard_events_alert_log",
        "What does the alert log show for inventory-api on 2026-01-22?",
        "analyze_events",
        "alerts are events, despite 'log'",
    ),
    RoutingCase(
        "hard_events_done_to",
        "What was done to redis around 15:10 UTC on 2026-04-08?",
        "analyze_events",
        "things done to a service are recorded events; no cue word at all",
    ),
    RoutingCase(
        "hard_events_print_word",
        "Print the restart history for payments-api during the week of 2026-02-16.",
        "analyze_events",
        "restarts are events, despite 'print'",
    ),
    RoutingCase(
        "hard_events_grep_word",
        "Grep the 2026-01-22 operational record for checkout-api for the phrase 'rolled back'.",
        "analyze_events",
        "the operational record is the event store, despite 'grep'",
    ),
    RoutingCase(
        "hard_events_message_word",
        "Which threshold-crossing messages were raised against payments-api on 2026-02-19 "
        "between 14:00 and 15:00 UTC?",
        "analyze_events",
        "threshold crossings are events, despite 'messages'",
    ),
    RoutingCase(
        "hard_events_stderr_word",
        "Ignore stderr; I want the sequence of deploys and rollbacks on checkout-api on "
        "2026-01-22.",
        "analyze_events",
        "deploys and rollbacks are events, with stderr named as a decoy",
    ),
    RoutingCase(
        "hard_events_trace_word",
        "Trace the scale actions on postgres between 03:00 and 06:00 UTC on 2026-03-02.",
        "analyze_events",
        "scale actions are events, despite 'trace'",
    ),
)

SETS: dict[str, tuple[RoutingCase, ...]] = {"plain": PLAIN_CASES, "hard": HARD_CASES}


def queries_sha256(cases: tuple[RoutingCase, ...]) -> str:
    """The identity of a query set: key, query, and expected tool, in order."""
    canonical = json.dumps(
        [[c.key, c.query, c.expected] for c in cases], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tools_over_the_wire(module: str) -> list[ToolSpec]:
    """List the variant's tools from its real server, so the descriptions under test are
    the shipped ones. The server is closed again immediately; no tool is ever called."""
    with McpStdioToolset.for_module(module) as toolset:
        specs = toolset.tools
    return [
        ToolSpec(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            handler=_never_runs,
        )
        for spec in specs
    ]


def _never_runs(_args: dict[str, Any]) -> str:
    raise RuntimeError("the routing check reads the tool_use block; tools are never executed")


def _route(client: LLMClient, specs: list[ToolSpec], query: str) -> tuple[str, dict[str, Any], Any]:
    resp = client.complete(
        messages=[{"role": "user", "content": query}],
        system=ROUTER_SYSTEM_PROMPT,
        tools=specs,
        tool_choice={"type": "any"},
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return block.name, dict(block.input or {}), resp.usage
    raise RuntimeError(f"no tool_use block (stop_reason={resp.stop_reason!r})")


def _part_four(description: str) -> str:
    marker = "When to use this vs"
    return description[description.index(marker) :] if marker in description else "(none)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="v1")
    parser.add_argument("--set", choices=[*SETS, "all"], default="all", dest="query_set")
    parser.add_argument("--model", default=None, help="override the harness model")
    parser.add_argument("--case", action="append", help="run only these case keys")
    parser.add_argument("--dry-run", action="store_true", help="print the sets; no calls")
    args = parser.parse_args(argv)

    set_names = list(SETS) if args.query_set == "all" else [args.query_set]
    all_cases = {c.key: (name, c) for name in set_names for c in SETS[name]}
    if args.case:
        unknown = set(args.case) - set(all_cases)
        if unknown:
            parser.error(f"unknown case(s): {sorted(unknown)}")
        chosen = [all_cases[k] for k in all_cases if k in set(args.case)]
    else:
        chosen = list(all_cases.values())

    module = VARIANTS[args.variant]
    specs = _tools_over_the_wire(module)
    by_name = {s.name: s for s in specs}
    if set(by_name) != {"analyze_logs", "analyze_events"}:
        print(f"{module} lists {sorted(by_name)}, expected analyze_logs and analyze_events")
        return 2
    hashes = {name: queries_sha256(SETS[name]) for name in set_names}

    print(f"tool routing - variant {args.variant} ({module}), {len(chosen)} queries")
    for name, spec in by_name.items():
        print(f"  {name}: {_part_four(spec.description)}")
    for name, digest in hashes.items():
        print(f"  set {name:<6} {len(SETS[name])} queries, sha256 {digest[:16]}...")
    print()

    if args.dry_run:
        for set_name, case in chosen:
            print(f"  {set_name:<6} {case.key:<28} -> {case.expected:<15} {case.query}")
        print("\n(dry run: no calls made)")
        return 0

    settings = LLMSettings(model=args.model) if args.model else LLMSettings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    client = LLMClient(settings)

    errors = 0
    total_in = total_out = 0
    per_set: dict[str, dict[str, Any]] = {
        name: {"scored": 0, "misrouted": 0, "by_expected": {}} for name in set_names
    }

    with RunRecorder(
        kind="llm",
        name=f"tool-routing-{args.variant}",
        command=f"check_tool_routing.py --variant {args.variant} --set {args.query_set}",
        metadata={
            "variant": args.variant,
            "server_module": module,
            "model": settings.model,
            "sets": set_names,
            "cases": [c.key for _, c in chosen],
            "queries_sha256": hashes,
            "part_four": {name: _part_four(s.description) for name, s in by_name.items()},
        },
    ) as run:
        run.artifact(
            "queries.json",
            json.dumps({name: [asdict(c) for c in SETS[name]] for name in set_names}, indent=2),
        )
        run.artifact(
            "descriptions.json",
            json.dumps({name: s.description for name, s in by_name.items()}, indent=2),
        )
        results: list[dict[str, Any]] = []
        for set_name, case in chosen:
            start = time.monotonic()
            try:
                picked, arguments, usage = _route(client, specs, case.query)
            except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
                errors += 1
                run.event(
                    case.key,
                    outcome="error",
                    duration_ms=(time.monotonic() - start) * 1000,
                    type="llm_call",
                    message=f"{type(exc).__name__}: {exc}",
                    data={"set": set_name, "query": case.query, "expected": case.expected},
                )
                print(f"  ERROR {case.key}: {type(exc).__name__}: {exc}")
                continue
            duration_ms = (time.monotonic() - start) * 1000
            wrong = picked != case.expected
            total_in += usage.input_tokens
            total_out += usage.output_tokens
            tally = per_set[set_name]
            tally["scored"] += 1
            tally["misrouted"] += wrong
            bucket = tally["by_expected"].setdefault(case.expected, {"n": 0, "misrouted": 0})
            bucket["n"] += 1
            bucket["misrouted"] += wrong
            record = {
                "set": set_name,
                "key": case.key,
                "query": case.query,
                "expected": case.expected,
                "picked": picked,
                "misrouted": wrong,
                "arguments": arguments,
                "why": case.why,
                "duration_ms": round(duration_ms),
                "usage": {"in": usage.input_tokens, "out": usage.output_tokens},
            }
            results.append(record)
            run.event(
                case.key,
                outcome="failed" if wrong else "passed",
                duration_ms=duration_ms,
                type="llm_call",
                data=record,
                message=f"picked {picked}, expected {case.expected}",
            )
            mark = "MISROUTED" if wrong else "ok       "
            print(f"  {mark} {set_name:<6} {case.key:<28} picked {picked:<15} exp {case.expected}")

        summary = {
            "variant": args.variant,
            "model": settings.model,
            "sets": {
                name: {
                    **tally,
                    "misrouting_rate": (
                        tally["misrouted"] / tally["scored"] if tally["scored"] else None
                    ),
                    "queries_sha256": hashes[name],
                }
                for name, tally in per_set.items()
            },
            "errors": errors,
            "usage": {"in": total_in, "out": total_out},
        }
        run.artifact("routing.json", json.dumps({"summary": summary, "results": results}, indent=2))

    print()
    for name, tally in per_set.items():
        scored, wrong = tally["scored"], tally["misrouted"]
        rate = f"{wrong / scored:.0%}" if scored else "n/a"
        print(f"--- tool routing ({args.variant}, {name}): {wrong}/{scored} misrouted = {rate} ---")
        for expected, counts in tally["by_expected"].items():
            print(f"    expected {expected:<15} {counts['misrouted']}/{counts['n']} misrouted")
    print(f"    cost {total_in} in / {total_out} out tokens; model {settings.model}")
    if errors:
        print(f"    {errors} call(s) errored and were not scored")
    print(f"records: {run.dir}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
