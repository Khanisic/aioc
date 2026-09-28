"""Day 15 checkpoint, live: one query that needs all four agents, exercising the parallel
*and* the sequential path in a single request.

    uv run python scripts/check_day15_integration.py --deploy       # ~10-20 live Claude calls
    uv run python scripts/check_day15_integration.py --deploy --max-rounds 0
    uv run python scripts/check_day15_integration.py --skip-inject  # reuse whatever chaos is on

Cost: one planning call, one call each for Incident and Docs, the GitHub agent's tool loop
(typically 2-4 calls), the Deployment agent's (typically 3-5), then each refinement round is
another agent run per re-delegated agent (cap `--max-rounds`, default 2), and one synthesis
call (`--deterministic-synthesis` skips it). Needs `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` +
`GITHUB_REPO`, the Docker stack up, and the demo services running the release under test
(`--deploy` does it). Tracing is on when the Langfuse keys are set.

**The scenario.** The shape HANDOFF named on Day 14: an incident whose suspect is a recent
deploy. A real fault is injected (`downstream_latency` by default: payments-api slows down),
the situation block is the live Prometheus metrics plus what the on-call knows about
releases, and the query asks four things no single agent can answer - what is failing
(Incident), what past incidents say about handling it (Docs), what the suspect PR changed
(GitHub), and whether the release that shipped it changed the service's configuration or
images and how its rollout is doing (Deployment). The release's identity is, as on Day 13,
reachable only through the pull request - that is the data dependency that makes
GitHub -> Deployment sequential rather than a parallel pair (war story #9).

**The scenario is honest about its own answer.** The injected fault has nothing to do with
the PR, and nothing in the situation block says it does. A system reasoning correctly should
say so: the on-call's suspicion is the question, not the premise. The check therefore
asserts the orchestration, never the conclusion.

**The PR is small on purpose.** PR #15 (the Day 13/14 default) is 27 files, and its ~22k-token
read was re-sent on every GitHub round (HANDOFF sec 7 item 16). PR #11 is six files, and the
previous release defaults to the commit main was at before the merge, so the release diff is
the PR's own change and nothing else. `--pr`, `--from`, and `--to` override.

**What is asserted.**

- All four agents are planned in round 0 and all four answer.
- The parallel path: at least two round-0 invocations with `mode: parallel` and no
  `depends_on` whose measured intervals overlap. Which pair is recorded, not prescribed -
  Incident + Docs is the expected one, but the planner may legitimately chain Docs behind
  Incident, and GitHub runs in the parallel group too.
- The sequential path: everything `check_day13_sequential.py` asserts (Deployment is
  `sequential` on GitHub, its recorded context carries GitHub's digest after the planner's
  block, it started after GitHub ended), plus the refinement-round rules.
- Cost is measured, not zero.

Every line printed is recorded with its wall-clock offset (`transcript.json`), so
`scripts/render_demo_gif.py --run <dir> --title ... --out ...` can replay the run.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 - runs our own chaos injector, with fixed arguments
import sys
import time
from itertools import combinations
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_day12_deployment import (  # noqa: E402 - needs the sys.path insert above
    _deploy,
    _deployed_version,
    _git,
)
from check_day13_sequential import (  # noqa: E402
    SITUATION_TEMPLATE,
    _pull_request_release,
    _synthesis_source,
    _TimedRunner,
)
from check_day13_sequential import _evaluate as _evaluate_sequential  # noqa: E402
from demo_day10 import _INJECTOR, _REPO_ROOT, _inject, _Transcript  # noqa: E402
from runlog import RunRecorder  # noqa: E402

from aioc.agents import default_retry_log  # noqa: E402
from aioc.contracts import AgentName, InvocationMode  # noqa: E402
from aioc.coordinator import (  # noqa: E402
    DEFAULT_MAX_REFINEMENT_ROUNDS,
    Executor,
    ModelSynthesiser,
    default_runners,
    respond,
)
from aioc.llm import LLMSettings  # noqa: E402
from aioc.observability import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
)
from aioc.observability.tracing import LangfuseTracer, default_tracer  # noqa: E402
from aioc.tools.deployment.server import DeploymentSettings  # noqa: E402
from aioc.tools.github.api import GitHubSettings  # noqa: E402

DEFAULT_PR = 11
DEFAULT_SERVICE = "checkout-api"
DEFAULT_MODE = "downstream_latency"

QUERY_TEMPLATE = (
    "{service} has been slow since the release that shipped PR #{pr} went out, and the "
    "on-call suspects that release. What is failing and what is the most likely cause? What "
    "do our past incidents say about handling this kind of failure? What did PR #{pr} "
    "change, and did the release that shipped it change {service}'s configuration or images "
    "compared with the last recorded release - is the rollout itself healthy?"
)


def _previous_release(to_version: str) -> str | None:
    """Where main stood before the release under test: its first parent. For a merge commit
    that is main before the merge, so the release diff is the PR's change and nothing else."""
    return _git("rev-parse", f"{to_version}^1")


def _reset_chaos() -> bool:
    result = subprocess.run(  # noqa: S603 - our own script, fixed args
        [sys.executable, str(_INJECTOR), "--reset"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    return result.returncode == 0


def _evaluate_parallel(
    resp: Any, timings: list[dict[str, Any]]
) -> tuple[list[str], list[list[str]]]:
    """The half Day 13 does not cover: four agents planned and answering, and a parallel
    group that really overlapped in wall-clock time. Returns the complaints and the
    overlapping pairs (as agent names) for the record."""
    complaints: list[str] = []
    round_zero = [inv for inv in resp.selected_agents if inv.round == 0]
    planned = {inv.agent for inv in round_zero}
    for agent in AgentName:
        if agent not in planned:
            complaints.append(f"the plan did not select {agent.value}")
    answered = {r.agent for r in resp.agent_responses}
    for agent in AgentName:
        if agent in planned and agent not in answered:
            complaints.append(f"{agent.value} produced no response")

    group = [
        inv for inv in round_zero if inv.mode is InvocationMode.PARALLEL and not inv.depends_on
    ]
    if len(group) < 2:
        complaints.append(
            f"the parallel group has {len(group)} member(s); a parallel path needs at least two"
        )
        return complaints, []

    spans = {t["invocation_id"]: t for t in timings if "end" in t}
    overlapping: list[list[str]] = []
    for a, b in combinations(group, 2):
        sa, sb = spans.get(a.invocation_id), spans.get(b.invocation_id)
        if sa is None or sb is None:
            continue
        if sa["start"] < sb["end"] and sb["start"] < sa["end"]:
            overlapping.append([a.agent.value, b.agent.value])
    if not overlapping:
        complaints.append("no two invocations of the parallel group overlapped in time")
    return complaints, overlapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, default=DEFAULT_PR)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--from", dest="from_version", default=None, help="previous release")
    parser.add_argument(
        "--to", dest="to_version", default=None, help="release under test (default: PR merge)"
    )
    parser.add_argument("--query", default=None, help="override the query")
    parser.add_argument("--deploy", action="store_true", help="recreate the demo at --to")
    parser.add_argument(
        "--mode",
        choices=[
            "resource_exhaustion",
            "downstream_latency",
            "code_regression",
            "bad_config_deploy",
        ],
        default=DEFAULT_MODE,
        help="failure mode to inject",
    )
    parser.add_argument("--settle-seconds", type=int, default=45)
    parser.add_argument(
        "--skip-inject", action="store_true", help="diagnose the app as it is; no injection"
    )
    parser.add_argument(
        "--keep-chaos", action="store_true", help="do not reset the chaos knobs afterwards"
    )
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_REFINEMENT_ROUNDS,
        help="refinement round cap",
    )
    parser.add_argument(
        "--deterministic-synthesis",
        action="store_true",
        help="skip the model-written synthesis call",
    )
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    gh = GitHubSettings()
    if gh.token_value() is None or gh.repository is None:
        print("GITHUB_TOKEN and GITHUB_REPO must be set (shell or .env).", file=sys.stderr)
        return 2

    t = _Transcript()
    say = t.say
    say("=" * 72)
    say("AIOC - Enterprise AI Operations Center")
    say("Day 15: four agents live - parallel and sequential paths in one request")
    say("=" * 72)
    say()

    to_version = args.to_version
    if to_version is None:
        to_version, probe = _pull_request_release(args.pr)
        if to_version is None:
            error = probe.get("error", {})
            print(f"GitHub could not resolve PR #{args.pr}: {error.get('code')}", file=sys.stderr)
            return 2
    from_version = args.from_version or _previous_release(to_version)
    if from_version is None:
        print("--from is required when git cannot resolve the default.", file=sys.stderr)
        return 2
    environment = DeploymentSettings().monitored_environment()

    # Deploy before injecting: the knobs live in the service processes, and recreating the
    # containers at the release under test resets them.
    say(f"[1/5] The release under test: PR #{args.pr}, {from_version[:10]} -> {to_version[:10]}")
    deployed, probe = _deployed_version(args.service, environment)
    if not probe.get("ok"):
        error = probe.get("error", {})
        print(f"the stack is not answering: {error.get('code')}: {error.get('message')}")
        return 2
    if deployed != to_version:
        if not args.deploy:
            print(
                f"deployed {deployed!r}, but the release under test is {to_version[:10]}; run "
                f"with --deploy, or:  DEMO_GIT_SHA={to_version} docker compose up -d --wait"
            )
            return 2
        _deploy(to_version)
        deployed, probe = _deployed_version(args.service, environment)
        if deployed != to_version:
            print(f"still {deployed!r} after deploy; is service_build_info being scraped?")
            return 2
    say(f"      {args.service} is running {deployed[:10]} (check_rollout_health, 0 Claude calls)")
    say()

    if not args.skip_inject:
        say(f"[2/5] Injecting a real fault: {args.mode}")
        if not _inject(args.mode, t):
            print("chaos injection failed - is the stack up?", file=sys.stderr)
            return 2
        say(f"      waiting {args.settle_seconds}s for Prometheus rate() windows to fill...")
        time.sleep(args.settle_seconds)
    else:
        say("[2/5] Skipping injection; diagnosing the app as it is.")
    say()

    say("[3/5] Live metrics + what the on-call knows -> the coordinator's situation block")
    release_note = SITUATION_TEMPLATE.format(
        repo=gh.repository,
        env=environment,
        service=args.service,
        from_version=from_version,
        pr=args.pr,
    )
    try:
        situation = build_incident_context(
            PrometheusClient(), Window.last(args.window_minutes), extra_notes=release_note
        )
    except PrometheusError as exc:
        print(f"Prometheus is unreachable: {exc}", file=sys.stderr)
        return 2
    for line in situation.splitlines():
        say(f"    {line}")
    say()

    query = args.query or QUERY_TEMPLATE.format(service=args.service, pr=args.pr)
    tracer = default_tracer()
    traced = isinstance(tracer, LangfuseTracer)
    timings: list[dict[str, Any]] = []
    runners = {name: _TimedRunner(runner, timings) for name, runner in default_runners().items()}
    executor = Executor(
        runners,  # type: ignore[arg-type]
        tracer=tracer,
        synthesiser=None if args.deterministic_synthesis else ModelSynthesiser(),
        max_refinement_rounds=args.max_rounds,
    )
    say(f'[4/5] Asking the coordinator: "{query}"')
    say(
        f"      (~10-20 Claude calls; refinement cap {args.max_rounds}, synthesis "
        f"{'deterministic' if args.deterministic_synthesis else 'model-written'}"
        + (", traced to Langfuse)" if traced else "; tracing off - no Langfuse keys)")
    )
    say()

    passed = False
    with RunRecorder(
        kind="llm",
        name="day15-integration",
        command="check_day15_integration.py",
        metadata={
            "query": query,
            "repository": gh.repository,
            "pr": args.pr,
            "service": args.service,
            "from_version": from_version,
            "to_version": to_version,
            "mode": None if args.skip_inject else args.mode,
            "traced": traced,
            "max_rounds": args.max_rounds,
            "model_synthesis": not args.deterministic_synthesis,
        },
    ) as run:
        run.artifact("situation.txt", situation)
        start = time.monotonic()
        try:
            default_retry_log().clear()
            resp = respond(query, situation=situation, executor=executor, tracer=tracer)
            wall_seconds = time.monotonic() - start
        except Exception as exc:
            tracer.flush()
            run.event(
                "integration",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            run.artifact("transcript.json", t.to_json())
            print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        finally:
            if not args.skip_inject and not args.keep_chaos and not _reset_chaos():
                print("chaos reset failed - run inject.py --reset by hand", file=sys.stderr)
        tracer.flush()

        parallel_complaints, overlapping = _evaluate_parallel(resp, timings)
        complaints = parallel_complaints + [
            c for c in _evaluate_sequential(resp, timings, args.pr) if c not in parallel_complaints
        ]
        passed = not complaints
        trace_url = (
            tracer.trace_url(resp.trace_id) if traced and resp.trace_id is not None else None
        )
        t0 = min((x["start"] for x in timings), default=start)
        spans = {
            x["invocation_id"]: {
                "start_s": round(x["start"] - t0, 1),
                "end_s": round(x.get("end", x["start"]) - t0, 1),
            }
            for x in timings
        }

        say("[5/5] The coordinator's answer")
        say()
        intent = resp.intent.value.value if resp.intent.value else "unclassified"
        say(f"  intent      {intent} @ {resp.intent.confidence:.2f}")
        for inv in resp.selected_agents:
            deps = f" after {', '.join(inv.depends_on)}" if inv.depends_on else ""
            span = spans.get(inv.invocation_id)
            when = f"  [{span['start_s']:>5.1f}s -> {span['end_s']:>5.1f}s]" if span else ""
            rnd = f" (refinement round {inv.round})" if inv.round else ""
            say(f"  selected    {inv.agent.value:<11} {inv.mode.value}{deps}{rnd}{when}")
        for skipped in resp.skipped_agents:
            say(f"  skipped     {skipped.agent.value:<11} {skipped.reason}")
        for pair in overlapping:
            say(f"  overlapped  {pair[0]} + {pair[1]} ran concurrently")
        say()
        for r in resp.agent_responses:
            tools = ", ".join(f"{tc.tool_name}[{'ok' if tc.ok else 'err'}]" for tc in r.tool_calls)
            say(
                f"  -- {r.agent.value} agent ({r.status.value}, confidence "
                f"{r.overall_confidence:.2f}) tools: {tools or 'none'}"
            )
            say(f"     {r.summary}")
            for g in r.gaps:
                target = f" -> {g.suggested_agent.value}" if g.suggested_agent else ""
                flag = "resolvable" if g.resolvable else "unresolvable"
                say(f"     gap {g.id} [{flag}]{target}: {g.description[:120]}")
            say()
        say(f"  synthesis ({_synthesis_source(resp)}):")
        for line in resp.synthesis.splitlines():
            say(f"     {line}")
        say()
        say(f"  answer ({resp.answer.confidence:.2f}): {resp.answer.value}")
        say()
        say(
            f"  status {resp.status.value} | cost {resp.cost.input_tokens} in / "
            f"{resp.cost.output_tokens} out | {wall_seconds:.1f}s wall | "
            f"refinement rounds {resp.refinement_rounds} | gaps {len(resp.unresolved_gaps)}"
        )
        say(f"  trace  {trace_url or resp.trace_id or 'not traced'}")
        for line in default_retry_log().render_summary().splitlines():
            say(f"  {line}")
        for complaint in complaints:
            say(f"  ! {complaint}")
        # The verdict goes out before the transcript is written, or the replay ends without it.
        say()
        say(f"--- four agents, parallel and sequential: {'PASS' if passed else 'FAIL'} ---")

        run.artifact(
            "retries.json",
            json.dumps([r.to_dict() for r in default_retry_log().records], indent=2),
        )
        run.artifact("response.json", resp.model_dump_json(indent=2))
        run.artifact("transcript.json", t.to_json())
        run.artifact(
            "contexts.json",
            json.dumps(
                [
                    {
                        "invocation_id": i.invocation_id,
                        "agent": i.agent.value,
                        "round": i.round,
                        "mode": i.mode.value,
                        "depends_on": i.depends_on,
                        "reason": i.reason,
                        "context_passed": i.context_passed,
                        "span": spans.get(i.invocation_id),
                    }
                    for i in resp.selected_agents
                ],
                indent=2,
            ),
        )
        run.event(
            "integration",
            outcome="passed" if passed else "failed",
            duration_ms=wall_seconds * 1000,
            type="llm_call",
            data={
                "query": query,
                "status": resp.status.value,
                "intent": intent,
                "plan": [
                    {
                        "agent": i.agent.value,
                        "invocation_id": i.invocation_id,
                        "mode": i.mode.value,
                        "depends_on": i.depends_on,
                        "round": i.round,
                        "context_chars": len(i.context_passed),
                    }
                    for i in resp.selected_agents
                ],
                "overlapping_pairs": overlapping,
                "refinement_rounds": resp.refinement_rounds,
                "unresolved_gaps": [g.id for g in resp.unresolved_gaps],
                "synthesis_source": _synthesis_source(resp),
                "skipped": [s.agent.value for s in resp.skipped_agents],
                "spans": spans,
                "agents_responded": [r.agent.value for r in resp.agent_responses],
                "tool_calls": {
                    r.invocation_id: [
                        {"tool": tc.tool_name, "ok": tc.ok, "ms": tc.duration_ms}
                        for tc in r.tool_calls
                    ]
                    for r in resp.agent_responses
                },
                "usage": {"in": resp.cost.input_tokens, "out": resp.cost.output_tokens},
                "wall_seconds": round(wall_seconds, 1),
                "trace_id": resp.trace_id,
                "trace_url": trace_url,
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "four agents ran live",
        )

    say()
    say(f"records: {run.dir}")
    if args.deploy:
        say("the demo is still on the release under test; to restore baseline:")
        say("    docker compose up -d --wait")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
