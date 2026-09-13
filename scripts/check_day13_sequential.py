"""Day 13 done-when, live: the sequential path end to end - the coordinator plans GitHub
then Deployment, GitHub reads the PR, the executor hands GitHub's digest to Deployment,
and Deployment diffs the release with that digest in its context.

    uv run python scripts/check_day13_sequential.py                  # ~6-9 live Claude calls
    uv run python scripts/check_day13_sequential.py --deploy         # recreate the demo
                                                                     # containers at --to first
    uv run python scripts/check_day13_sequential.py --pr 15 --from <ref> --to <ref>

Cost: one planning call, then the GitHub agent's tool loop (typically 2-3 calls) followed
by the Deployment agent's (typically 3-5). Needs `GITHUB_TOKEN` + `GITHUB_REPO` in `.env`,
the Docker stack up, and the demo services running the `--to` release (see
`check_day12_deployment.py` for why; `--deploy` does it). `--to` defaults to the PR's head
commit, read from the GitHub server over the wire before any Claude call, and `--from` to
sixteen first-parent commits before it. Tracing is on when the Langfuse keys are set, and
the trace is the visual half of the proof: two agent spans, the second starting after the
first ends, with the composed context as the second span's input.

**The scenario has to carry a real dependency.** The first live run told the coordinator
both release SHAs in the situation block, and it planned GitHub and Deployment in parallel
- correctly, because with the SHAs in hand Deployment needed nothing from GitHub. Dynamic
selection is graded behaviour and it was right; the scenario was wrong. Now the on-call
knows the last recorded release and that a PR shipped since, but not the new release's
identity: that comes from GitHub's report, through the executor's handoff, into
Deployment's context. The check asserts the plan is sequential, that Deployment's recorded
`context_passed` carries GitHub's digest after the planner's block, and that Deployment
started only after GitHub returned.

**What is being proven.** The offline tests prove the executor composes a dependent's
context from its dependency's digest and records it verbatim; the Day 11 and Day 12 checks
prove each agent alone. What only a live run can show is the whole chain with a real model
at every step: the coordinator choosing `sequential` with a `depends_on` edge on its own,
GitHub producing a report the digest is built from, and Deployment producing a grounded
report *with GitHub's findings in its context* - which is visible in the response, because
`context_passed` on the Deployment invocation is the block Deployment actually saw.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_day12_deployment import (  # noqa: E402 - needs the sys.path insert above
    _default_from,
    _deploy,
    _deployed_version,
)
from runlog import RunRecorder  # noqa: E402

from aioc.agents.github import GITHUB_SERVER_MODULE  # noqa: E402
from aioc.contracts import AgentName, AgentResponse, InvocationMode  # noqa: E402
from aioc.coordinator import Executor, default_runners, respond  # noqa: E402
from aioc.coordinator.handoff import HANDOFF_HEADER  # noqa: E402
from aioc.llm import LLMSettings, McpStdioToolset, Usage  # noqa: E402
from aioc.observability.tracing import LangfuseTracer, default_tracer  # noqa: E402
from aioc.tools.deployment.server import DeploymentSettings  # noqa: E402
from aioc.tools.github.api import GitHubSettings  # noqa: E402

DEFAULT_PR = 15
DEFAULT_SERVICE = "checkout-api"

# What the on-call knows. Deliberately NOT the identity of the release under test: the first
# live run handed the coordinator both SHAs, and it correctly planned GitHub and Deployment
# in parallel, because Deployment then needed nothing from GitHub. Dynamic selection working
# as designed - and a sequential scenario needs a real data dependency, so here the new
# release's identity is only reachable through the pull request.
SITUATION_TEMPLATE = (
    "Repository: {repo}. Environment: {env} (the only monitored environment). Service under "
    "review: {service}. The last release operations has a record of is {from_version}. Pull "
    "request #{pr} was merged into main since then and its build has been rolled out to "
    "{service}, but the identity of that release (its commit) is not known to operations - "
    "it has to be read from the pull request. Releases are git refs of the repository."
)


class _TimedRunner:
    """Wraps a real runner to record when each invocation started and finished, so the
    ordering the trace shows is also asserted here without reading Langfuse back."""

    def __init__(self, inner: Any, log: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._log = log

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> AgentResponse:
        entry: dict[str, Any] = {"invocation_id": invocation_id, "start": time.monotonic()}
        self._log.append(entry)
        try:
            response: AgentResponse = self._inner.run(
                query,
                context=context,
                request_id=request_id,
                invocation_id=invocation_id,
                usage=usage,
            )
        finally:
            entry["end"] = time.monotonic()
        return response


def _pull_request_head(pr: int) -> tuple[str | None, dict[str, Any]]:
    """The PR's head commit, from the GitHub server itself (zero Claude calls). It is the
    release under test: the demo is deployed at it, and GitHub's report will name it."""
    with McpStdioToolset.for_module(GITHUB_SERVER_MODULE) as toolset:
        result = toolset.call("get_pull_request", {"number": pr})
    payload = json.loads(result.content)
    if not payload.get("ok"):
        return None, payload
    return payload["data"]["pull_request"].get("head_sha"), payload


def _evaluate(resp: Any, timings: list[dict[str, Any]], pr: int) -> list[str]:
    complaints: list[str] = []
    by_agent = {inv.agent: inv for inv in resp.selected_agents}
    if AgentName.GITHUB not in by_agent:
        complaints.append("the plan did not select github")
    if AgentName.DEPLOYMENT not in by_agent:
        complaints.append("the plan did not select deployment")
    if complaints:
        return complaints

    gh, dep = by_agent[AgentName.GITHUB], by_agent[AgentName.DEPLOYMENT]
    if dep.mode is not InvocationMode.SEQUENTIAL:
        complaints.append(f"deployment mode is {dep.mode.value}, expected sequential")
    if gh.invocation_id not in dep.depends_on:
        complaints.append(f"deployment.depends_on {dep.depends_on} does not name github")

    responded = {r.agent for r in resp.agent_responses}
    if AgentName.GITHUB not in responded:
        complaints.append("github produced no response")
    if AgentName.DEPLOYMENT not in responded:
        complaints.append("deployment produced no response")

    ctx = dep.context_passed
    if HANDOFF_HEADER not in ctx or '<handoff from="github"' not in ctx:
        complaints.append("deployment.context_passed carries no github handoff")
    else:
        planner_block = ctx.split(HANDOFF_HEADER)[0].strip()
        if not planner_block:
            complaints.append("the planner's block was replaced rather than appended to")
        if f"#{pr}" not in ctx:
            complaints.append(f"the handoff does not mention PR #{pr}")

    started = {t["invocation_id"]: t for t in timings}
    if gh.invocation_id in started and dep.invocation_id in started:
        if started[dep.invocation_id]["start"] < started[gh.invocation_id].get("end", 0):
            complaints.append("deployment started before github finished")
    else:
        complaints.append("timings are missing for one of the two invocations")

    if resp.cost.input_tokens <= 0:
        complaints.append("cost is zero - the Usage accumulator did not thread through")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, default=DEFAULT_PR)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--from", dest="from_version", default=None, help="previous release")
    parser.add_argument(
        "--to", dest="to_version", default=None, help="release under test (default: PR head)"
    )
    parser.add_argument("--query", default=None, help="override the query")
    parser.add_argument("--deploy", action="store_true", help="recreate the demo at --to")
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    gh = GitHubSettings()
    if gh.token_value() is None or gh.repository is None:
        print("GITHUB_TOKEN and GITHUB_REPO must be set (shell or .env).", file=sys.stderr)
        return 2

    to_version = args.to_version
    if to_version is None:
        to_version, probe = _pull_request_head(args.pr)
        if to_version is None:
            error = probe.get("error", {})
            code, message = error.get("code"), error.get("message")
            print(f"  GitHub could not resolve PR #{args.pr}: {code}: {message}")
            return 2
    from_version = args.from_version or _default_from(to_version)
    if from_version is None:
        print("--from is required when git cannot resolve the default.", file=sys.stderr)
        return 2
    environment = DeploymentSettings().monitored_environment()

    print(
        f"Day 13 sequential check - ~6-9 live Claude calls; PR #{args.pr}, {args.service} in "
        f"{environment}, {from_version[:10]} -> {to_version[:10]}\n"
    )

    deployed, probe = _deployed_version(args.service, environment)
    if not probe.get("ok"):
        error = probe.get("error", {})
        print(f"  the stack is not answering: {error.get('code')}: {error.get('message')}")
        return 2
    if deployed != to_version:
        print(f"  deployed   {deployed!r}, but the release under test is {to_version[:10]}")
        if not args.deploy:
            print(
                f"  run with --deploy, or:  DEMO_GIT_SHA={to_version} docker compose up -d --wait"
            )
            return 2
        _deploy(to_version)
        deployed, probe = _deployed_version(args.service, environment)
        if deployed != to_version:
            print(f"  still {deployed!r} after deploy; is service_build_info being scraped?")
            return 2
    print(f"  deployed   {deployed[:10]} confirmed by check_rollout_health (0 Claude calls)")

    query = args.query or (
        f"PR #{args.pr} was merged into main. What did it change, and did the release that "
        f"shipped it change {args.service}'s configuration or images compared with the last "
        "recorded release? Is the current rollout healthy?"
    )
    situation = SITUATION_TEMPLATE.format(
        repo=gh.repository,
        env=environment,
        service=args.service,
        to_version=to_version,
        from_version=from_version,
        pr=args.pr,
    )

    tracer = default_tracer()
    traced = isinstance(tracer, LangfuseTracer)
    timings: list[dict[str, Any]] = []
    runners = {name: _TimedRunner(runner, timings) for name, runner in default_runners().items()}
    executor = Executor(runners, tracer=tracer)  # type: ignore[arg-type]
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day13-sequential",
        command="check_day13_sequential.py",
        metadata={
            "query": query,
            "situation": situation,
            "repository": gh.repository,
            "pr": args.pr,
            "service": args.service,
            "from_version": from_version,
            "to_version": to_version,
            "traced": traced,
        },
    ) as run:
        try:
            resp = respond(query, situation=situation, executor=executor, tracer=tracer)
        except Exception as exc:
            tracer.flush()
            run.event(
                "sequential",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            report = getattr(exc, "report", None)
            if report is not None:
                run.artifact("rejected_report.json", report.model_dump_json(indent=2))
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1
        wall_seconds = time.monotonic() - start
        tracer.flush()

        complaints = _evaluate(resp, timings, args.pr)
        passed = not complaints
        trace_url = (
            tracer.trace_url(resp.trace_id) if traced and resp.trace_id is not None else None
        )
        t0 = min((t["start"] for t in timings), default=start)
        spans = {
            t["invocation_id"]: {
                "start_s": round(t["start"] - t0, 1),
                "end_s": round(t.get("end", t["start"]) - t0, 1),
            }
            for t in timings
        }
        dep_inv = next((i for i in resp.selected_agents if i.agent is AgentName.DEPLOYMENT), None)
        if dep_inv is not None:
            run.artifact("deployment_context.txt", dep_inv.context_passed)
        run.artifact("response.json", resp.model_dump_json(indent=2))
        run.event(
            "sequential",
            outcome="passed" if passed else "failed",
            duration_ms=wall_seconds * 1000,
            type="llm_call",
            data={
                "query": query,
                "status": resp.status.value,
                "intent": resp.intent.value.value if resp.intent.value else None,
                "plan": [
                    {
                        "agent": i.agent.value,
                        "invocation_id": i.invocation_id,
                        "mode": i.mode.value,
                        "depends_on": i.depends_on,
                        "context_chars": len(i.context_passed),
                    }
                    for i in resp.selected_agents
                ],
                "skipped": [s.agent.value for s in resp.skipped_agents],
                "spans": spans,
                "agents_responded": [r.agent.value for r in resp.agent_responses],
                "tool_calls": {
                    r.agent.value: [
                        {"tool": tc.tool_name, "ok": tc.ok, "ms": tc.duration_ms}
                        for tc in r.tool_calls
                    ]
                    for r in resp.agent_responses
                },
                "usage": {"in": resp.cost.input_tokens, "out": resp.cost.output_tokens},
                "trace_id": resp.trace_id,
                "trace_url": trace_url,
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "sequential handoff ran live",
        )

        intent = resp.intent.value.value if resp.intent.value else "unclassified"
        print(f"  intent     {intent} @ {resp.intent.confidence:.2f}")
        for inv in resp.selected_agents:
            deps = f" after {', '.join(inv.depends_on)}" if inv.depends_on else ""
            span = spans.get(inv.invocation_id, {})
            when = f"  [{span['start_s']:>5.1f}s -> {span['end_s']:>5.1f}s]" if span else ""
            print(
                f"  selected   {inv.agent.value:<11} {inv.mode.value}{deps}{when}  "
                f"context {len(inv.context_passed)} chars"
            )
        for skipped in resp.skipped_agents:
            print(f"  skipped    {skipped.agent.value:<11} {skipped.reason[:80]}")
        for r in resp.agent_responses:
            tools = ", ".join(f"{tc.tool_name}[{'ok' if tc.ok else 'err'}]" for tc in r.tool_calls)
            print(
                f"  -- {r.agent.value} ({r.status.value}, {r.overall_confidence:.2f}) "
                f"tools: {tools or 'none'}"
            )
            print(f"     {r.summary}")
        if dep_inv is not None and HANDOFF_HEADER in dep_inv.context_passed:
            handoff = dep_inv.context_passed.split(HANDOFF_HEADER, 1)[1].strip()
            print("  handoff seen by deployment (first lines):")
            for line in handoff.splitlines()[:6]:
                print(f"     {line[:110]}")
        print(f"  answer     ({resp.answer.confidence:.2f}) {resp.answer.value}")
        print(
            f"  status     {resp.status.value} | cost {resp.cost.input_tokens} in / "
            f"{resp.cost.output_tokens} out | {wall_seconds:.1f}s | gaps "
            f"{len(resp.unresolved_gaps)}"
        )
        print(f"  trace      {trace_url or resp.trace_id or 'not traced'}")
        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- sequential path: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
