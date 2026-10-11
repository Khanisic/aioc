"""Day 25 checkpoint: a real fault diagnosed by the four agents through the deployed service.

    AIOC_API_TOKEN=<t> AIOC_API_LIVE=true docker compose --profile api up -d --build --wait
    AIOC_API_TOKEN=<t> uv run python scripts/check_day25_service.py --deploy

This is `check_day15_integration.py`'s scenario sent to the service instead of called in
process: the demo app deployed at the release that shipped PR #11, a real fault injected,
the situation built from live Prometheus metrics, and then one `live` request to
`POST /v1/requests` with no refinement round and the deterministic synthesis. The only
thing that runs the agents is the service container - the same image a host would run.

**Cost: one live request, ~$0.55** (the HANDOFF sec 1 exception; it needs the owner's yes).
Everything else is free: the GitHub and Prometheus reads, the polls, and reading the trace
back from Langfuse's public API, which this script waits for (it took over two minutes on
the Day 24 rehearsal).

Passes when: the job succeeds; the response is a contract-valid `CoordinatorResponse`;
every planned agent answered; tokens were measured; every recommendation was decided by
the HITL gate; and the trace read back from Langfuse has one agent observation per
invocation, two of which overlapped in time (the parallel path, seen from the trace rather
than from runners this process does not own).

Every printed line is recorded with its offset (`transcript.json`), so
`scripts/render_demo_gif.py --run <dir>` replays it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_day12_deployment import _deploy, _deployed_version  # noqa: E402
from check_day13_sequential import SITUATION_TEMPLATE, _pull_request_release  # noqa: E402
from check_day15_integration import (  # noqa: E402
    DEFAULT_MODE,
    DEFAULT_PR,
    DEFAULT_SERVICE,
    QUERY_TEMPLATE,
    _previous_release,
    _reset_chaos,
)
from demo_day10 import _inject, _Transcript  # noqa: E402
from runlog import RunRecorder  # noqa: E402

from aioc.contracts import CoordinatorResponse, InvocationMode  # noqa: E402
from aioc.llm import LLMSettings  # noqa: E402
from aioc.observability import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
)
from aioc.observability.tracing import TracingSettings  # noqa: E402
from aioc.tools.deployment.server import DeploymentSettings  # noqa: E402
from aioc.tools.github.api import GitHubSettings  # noqa: E402

DEFAULT_URL = "http://localhost:8080"
POLL_SECONDS = 3  # under uvicorn's 5 s keep-alive, so a poll rarely meets a closing socket
JOB_TIMEOUT_SECONDS = 15 * 60
TRACE_TIMEOUT_SECONDS = 8 * 60


def _service_ready(api: httpx.Client) -> str | None:
    """Why the service cannot take a live request, or ``None`` when it can."""
    try:
        index = api.get("/").json()
        ready = api.get("/readyz")
    except httpx.HTTPError as exc:
        return f"the service at {api.base_url} is not answering ({exc})"
    if not index.get("live_requests"):
        return "live requests are off: start the container with AIOC_API_LIVE=true"
    if ready.status_code != 200:
        return f"/readyz is {ready.status_code}: {ready.text}"
    return None


def _wait_for_job(api: httpx.Client, location: str, say: Any) -> dict[str, Any]:
    started = time.monotonic()
    last = None
    while True:
        try:
            job: dict[str, Any] = api.get(location).json()
        except httpx.TransportError:
            # uvicorn closes an idle keep-alive connection after 5 s; a poll can reuse it as
            # it closes (the first Day 25 run died exactly so). A GET is safe to repeat.
            time.sleep(1)
            continue
        if job["status"] != last:
            say(f"      {time.monotonic() - started:>5.1f}s  {job['status']}")
            last = job["status"]
        if job["status"] in ("succeeded", "failed"):
            return job
        if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
            raise SystemExit(f"the job did not finish in {JOB_TIMEOUT_SECONDS}s: {location}")
        time.sleep(POLL_SECONDS)


def _read_trace(trace_id: str, agents: int, say: Any) -> dict[str, Any] | None:
    """The trace from Langfuse's public API, waiting until it is complete: one agent
    observation per invocation and the gate. Langfuse ingests observations separately, and
    the first run of this script read the trace with the Deployment span still missing."""
    settings = TracingSettings()
    if not settings.configured:
        say("      Langfuse keys are not set here; the trace cannot be read back")
        return None
    assert settings.public_key is not None and settings.secret_key is not None
    auth = (settings.public_key.get_secret_value(), settings.secret_key.get_secret_value())
    url = f"{settings.host.rstrip('/')}/api/public/traces/{trace_id}"
    started = time.monotonic()
    while time.monotonic() - started < TRACE_TIMEOUT_SECONDS:
        reply = httpx.get(url, auth=auth, timeout=30)
        if reply.status_code == 200:
            trace: dict[str, Any] = reply.json()
            names = [o.get("name", "") for o in trace.get("observations", [])]
            seen = sum(name.startswith("agent:") for name in names)
            if seen >= agents and "hitl_gate" in names:
                say(f"      readable after {time.monotonic() - started:.0f}s")
                return trace
        time.sleep(15)
    say(f"      not readable after {TRACE_TIMEOUT_SECONDS}s")
    return None


def _when(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _agent_spans(trace: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Each agent observation's interval, keyed by its invocation id."""
    spans: dict[str, tuple[float, float]] = {}
    for obs in trace.get("observations", []):
        if not obs.get("name", "").startswith("agent:"):
            continue
        start, end = _when(obs.get("startTime")), _when(obs.get("endTime"))
        invocation = (obs.get("metadata") or {}).get("invocation_id") or obs.get("id")
        if start is not None and end is not None:
            spans[str(invocation)] = (start, end)
    return spans


def _evaluate(
    job: dict[str, Any], resp: CoordinatorResponse, trace: dict[str, Any] | None
) -> tuple[list[str], list[list[str]]]:
    complaints: list[str] = []
    planned = {inv.agent for inv in resp.selected_agents}
    answered = {r.agent for r in resp.agent_responses}
    for agent in sorted(planned - answered, key=lambda a: a.value):
        complaints.append(f"{agent.value} was planned and produced no response")
    if resp.cost.input_tokens == 0:
        complaints.append("no tokens were measured; this was not a live run")
    recommendations = sum(
        len(getattr(r.findings, "recommended_actions", None) or []) for r in resp.agent_responses
    )
    if recommendations and not job["approvals"]:
        complaints.append(f"{recommendations} recommendation(s) and no gate decision recorded")

    overlapping: list[list[str]] = []
    if trace is None:
        complaints.append("the trace could not be read back from Langfuse")
        return complaints, overlapping
    spans = _agent_spans(trace)
    if len(spans) != len(resp.selected_agents):
        complaints.append(
            f"the trace has {len(spans)} agent observation(s) for "
            f"{len(resp.selected_agents)} invocation(s)"
        )
    group = [
        inv
        for inv in resp.selected_agents
        if inv.mode is InvocationMode.PARALLEL and not inv.depends_on
    ]
    for a, b in combinations(group, 2):
        sa, sb = spans.get(a.invocation_id), spans.get(b.invocation_id)
        if sa and sb and sa[0] < sb[1] and sb[0] < sa[1]:
            overlapping.append([a.agent.value, b.agent.value])
    if len(group) >= 2 and not overlapping:
        complaints.append("no two parallel invocations overlapped in the trace")
    return complaints, overlapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="the service's base URL")
    parser.add_argument("--pr", type=int, default=DEFAULT_PR)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--mode", default=DEFAULT_MODE, help="failure mode to inject")
    parser.add_argument("--deploy", action="store_true", help="recreate the demo at the PR")
    parser.add_argument("--settle-seconds", type=int, default=45)
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument(
        "--replay",
        default=None,
        help="re-print a recorded run's step 5 from its job.json into transcript.replayed.json",
    )
    parser.add_argument(
        "--job",
        default=None,
        help="read back a job this service is already running (job_...), paying nothing more",
    )
    args = parser.parse_args(argv)

    if args.replay is not None:
        return _replay(Path(args.replay))

    token = os.environ.get("AIOC_API_TOKEN", "").strip()
    if not token:
        print("AIOC_API_TOKEN must be set to the token the container was started with.")
        return 2
    gh = GitHubSettings()
    if gh.token_value() is None or gh.repository is None:
        print("GITHUB_TOKEN and GITHUB_REPO must be set (shell or .env).", file=sys.stderr)
        return 2
    api = httpx.Client(base_url=args.url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    why = _service_ready(api)
    if why is not None:
        print(why, file=sys.stderr)
        return 2

    t = _Transcript()
    say = t.say
    say("=" * 72)
    say("AIOC - Enterprise AI Operations Center")
    say("Day 25: a real fault, diagnosed by four agents through the deployed service")
    say("=" * 72)
    say()

    if args.job is not None:
        say(f"[1-4/6] Attached to {args.job}, submitted by an earlier run of this script")
        job = _wait_for_job(api, f"/v1/requests/{args.job}", say)
        say()
        return _report(job, args, say, t, situation=None, versions=(None, None))

    to_version, probe = _pull_request_release(args.pr)
    if to_version is None:
        print(f"GitHub could not resolve PR #{args.pr}: {probe.get('error')}", file=sys.stderr)
        return 2
    from_version = _previous_release(to_version)
    if from_version is None:
        print("git cannot resolve the release before the PR.", file=sys.stderr)
        return 2
    environment = DeploymentSettings().monitored_environment()
    say(f"[1/6] The release under test: PR #{args.pr}, {from_version[:10]} -> {to_version[:10]}")
    deployed, probe = _deployed_version(args.service, environment)
    if deployed != to_version:
        if not args.deploy:
            print(f"deployed {deployed!r}, not {to_version[:10]}; run with --deploy")
            return 2
        _deploy(to_version)
        deployed, probe = _deployed_version(args.service, environment)
        if deployed != to_version:
            print(f"still {deployed!r} after deploy; is service_build_info being scraped?")
            return 2
    say(f"      {args.service} is running {deployed[:10]}")
    say(f"      the service at {args.url} is ready and takes live requests")
    say()

    say(f"[2/6] Breaking production: injecting {args.mode}")
    if not _inject(args.mode, t):
        print("chaos injection failed - is the stack up?", file=sys.stderr)
        return 2
    try:
        say(f"      waiting {args.settle_seconds}s for Prometheus rate() windows to fill...")
        time.sleep(args.settle_seconds)
        say()

        say("[3/6] Live metrics + what the on-call knows -> the situation block")
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

        query = QUERY_TEMPLATE.format(service=args.service, pr=args.pr)
        body = {
            "query": query,
            "situation": situation,
            "mode": "live",
            "synthesis": "deterministic",
            "max_refinement_rounds": 0,
        }
        say(f'[4/6] POST {args.url}/v1/requests  (mode live, ~$0.55): "{query}"')
        submitted = api.post("/v1/requests", json=body)
        if submitted.status_code != 202:
            print(f"refused: {submitted.status_code} {submitted.text}", file=sys.stderr)
            return 1
        location = submitted.headers["Location"]
        say(f"      202 Accepted, Location: {location}")
        job = _wait_for_job(api, location, say)
    finally:
        if not _reset_chaos():
            print("chaos reset failed - run inject.py --reset by hand", file=sys.stderr)
    say()
    return _report(job, args, say, t, situation=situation, versions=(from_version, to_version))


def _replay(run_dir: Path) -> int:
    """A recorded run's transcript with step 5 re-printed by today's `_print_answer` from the
    run's own recorded job. Free and offline. Every other line, and every line's offset, is
    the recorded one; step 5's lines share the offset step 5 started at. Written beside the
    original as ``transcript.replayed.json``, which `render_demo_gif.py --transcript` reads."""
    lines = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    resp = CoordinatorResponse.model_validate(job["response"])
    starts = [i for i, e in enumerate(lines) if e["line"].startswith(("[5/6]", "[6/6]"))]
    if len(starts) != 2:
        print(f"{run_dir} has no complete step 5 to re-print", file=sys.stderr)
        return 2
    begin, end = starts
    printed: list[str] = []
    _print_answer(job, resp, lambda text="": printed.extend(text.split("\n")))
    at = lines[begin]["t"]
    rebuilt = lines[:begin] + [{"t": at, "line": line} for line in printed] + lines[end:]
    out = run_dir / "transcript.replayed.json"
    out.write_text(json.dumps(rebuilt, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"wrote {out} ({len(rebuilt)} lines; step 5 re-printed from job.json)")
    return 0


def _print_answer(job: dict[str, Any], resp: CoordinatorResponse, say: Any) -> None:
    """Step 5: the coordinator's answer as the service returned it. Shared with
    ``--replay``, which re-prints a recorded run's step 5 from its recorded job."""
    say("[5/6] The coordinator's answer, as the service returned it")
    say()
    intent = resp.intent.value.value if resp.intent.value else "unclassified"
    say(f"  intent      {intent} @ {resp.intent.confidence:.2f}")
    for inv in resp.selected_agents:
        deps = f" after {', '.join(inv.depends_on)}" if inv.depends_on else ""
        say(f"  selected    {inv.agent.value:<11} {inv.mode.value}{deps}")
    for skipped in resp.skipped_agents:
        say(f"  skipped     {skipped.agent.value:<11} {skipped.reason}")
    say()
    for r in resp.agent_responses:
        tools = ", ".join(f"{tc.tool_name}[{'ok' if tc.ok else 'err'}]" for tc in r.tool_calls)
        say(
            f"  -- {r.agent.value} agent ({r.status.value}, confidence "
            f"{r.overall_confidence:.2f}) tools: {tools or 'none'}"
        )
        say(f"     {r.summary}")
    say()
    say(f"  answer ({resp.answer.confidence:.2f}): {resp.answer.value}")
    say()
    for decision in job["approvals"]:
        # The whole action: the renderer wraps, and a cut action reads as a different one.
        say(
            f"  gate        {decision['decision']:<13} "
            f"{decision['request']['agent']}: {decision['request']['action']}"
        )
    say()
    cached = resp.cost.cache_read_tokens or 0
    say(
        f"  status {resp.status.value} | cost {resp.cost.input_tokens} in "
        f"({cached} from cache) / {resp.cost.output_tokens} out | "
        f"gaps {len(resp.unresolved_gaps)}"
    )
    say()


def _report(
    job: dict[str, Any],
    args: argparse.Namespace,
    say: Any,
    t: _Transcript,
    *,
    situation: str | None,
    versions: tuple[str | None, str | None],
) -> int:
    """Steps 5 and 6: the answer as the service returned it, and the trace read back."""
    from_version, to_version = versions
    query = job["query"]
    with RunRecorder(
        kind="llm",
        name="day25-service",
        command="check_day25_service.py",
        metadata={
            "url": args.url,
            "query": query,
            "pr": args.pr,
            "service": args.service,
            "mode": args.mode,
            "from_version": from_version,
            "to_version": to_version,
            "job_id": job["id"],
        },
    ) as run:
        if situation is not None:
            run.artifact("situation.txt", situation)
        run.artifact("job.json", json.dumps(job, indent=2))
        if job["status"] != "succeeded":
            say(f"  the job failed: {job['error']}")
            run.event("service", outcome="failed", type="llm_call", message=job["error"])
            run.artifact("transcript.json", t.to_json())
            return 1
        resp = CoordinatorResponse.model_validate(job["response"])
        run.artifact("response.json", resp.model_dump_json(indent=2))
        run.event(
            "service",
            outcome="passed",
            type="llm_call",
            data={
                "model": LLMSettings().for_role("subagent").model,
                "cost": resp.cost.model_dump(mode="json"),
            },
        )

        _print_answer(job, resp, say)

        say("[6/6] Reading the trace back from Langfuse")
        trace = (
            _read_trace(resp.trace_id, len(resp.selected_agents), say) if resp.trace_id else None
        )
        if trace is not None:
            run.artifact("trace.json", json.dumps(trace, indent=2, default=str))
        complaints, overlapping = _evaluate(job, resp, trace)
        for pair in overlapping:
            say(f"      overlapped  {pair[0]} + {pair[1]} ran concurrently in the container")
        say(f"      trace  {job['trace_url'] or resp.trace_id}")
        for complaint in complaints:
            say(f"  ! {complaint}")
        say()
        passed = not complaints
        say(
            "--- a real fault, four agents, the deployed service: "
            f"{'PASS' if passed else 'FAIL'} ---"
        )
        run.artifact("transcript.json", t.to_json())
        run.set_outcome("passed" if passed else "failed", exit_code=0 if passed else 1)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
