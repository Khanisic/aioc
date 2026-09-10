"""Day 12 done-when, live: does the Deployment agent diff a real release and read a real
rollout through the MCP wire, and return a contract-valid, grounded report?

    uv run python scripts/check_day12_deployment.py                  # ~3-5 live Claude calls
    uv run python scripts/check_day12_deployment.py --deploy         # also recreate the demo
                                                                     # containers at --to first
    uv run python scripts/check_day12_deployment.py --from <ref> --to <ref> --service payments-api

Cost: one Claude call per tool-loop round (typically 2-3: diff, health, then stop) plus one
forced `emit_deployment_report` call. GitHub reads are free within the rate limit and the
Prometheus reads are local. Needs `GITHUB_TOKEN` + `GITHUB_REPO` in `.env`, the Docker stack
up (`docker compose up -d --wait`), and the demo services running the `--to` release.

**The deployed version has to be the one under test.** The demo services report whatever
`DEMO_GIT_SHA` they were started with (`baseline` by default), and `check_rollout_health`
reads that from Prometheus. So this script first asks the deployment server (zero Claude
calls) which version is deployed; if it is not `--to`, it either recreates the containers
at `--to` (`--deploy`, which runs `docker compose up -d` with `DEMO_GIT_SHA` set and waits
for Prometheus to scrape the new identity) or prints the command and exits 2. `--to` must
be a ref GitHub knows: the default is `origin/main` resolved locally, and `--from` defaults
to sixteen first-parent commits before it (the root commit on a shorter
history), so the diff has commits, config keys, and an image to report.

**What this checks that the offline tests cannot.** `tests/test_deployment_agent.py` proves
the stamping and grounding logic against scripted payloads, `tests/test_deployment_tool.py`
proves the two tools against fake GitHub and fake Prometheus, and `tests/test_mcp_toolset.py`
proves the wire; here the *model* drives the real `aioc-deployment` server against the real
repository and the live stack and writes the report, so what is being proven is that a real
model, given real tool envelopes, produces a `DeploymentAgentResponse` whose service,
releases, keys, images, signals, and excerpts all trace back to a tool reply - the agent's
own checks raise otherwise, so a passing run *is* the proof.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import DeploymentAgent  # noqa: E402
from aioc.agents.deployment import DEPLOYMENT_SERVER_MODULE  # noqa: E402
from aioc.llm import LLMSettings, McpStdioToolset, Usage  # noqa: E402
from aioc.tools.deployment.server import DeploymentSettings  # noqa: E402
from aioc.tools.github.api import GitHubSettings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVICE = "checkout-api"
DEFAULT_FROM_DISTANCE = 16
SCRAPE_SETTLE_SECONDS = 20

CONTEXT_TEMPLATE = (
    "Deployment check for {service} in the {env} environment. Releases are git refs of the "
    "repository {repo}. The previous release was {from_version} and the currently deployed "
    "release is {to_version}. Report what changed between the two releases (configuration "
    "keys, images, manifests, commits) and how the current rollout is behaving, and "
    "recommend what an operator should do."
)


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _default_from(to_version: str) -> str | None:
    """`DEFAULT_FROM_DISTANCE` first-parent commits before `to`, or the root commit when the
    history is shorter - a ref that always exists, unlike a bare `~N`."""
    listed = _git(
        "rev-list", "--first-parent", f"--max-count={DEFAULT_FROM_DISTANCE + 1}", to_version
    )
    if not listed:
        return None
    oldest = listed.splitlines()[-1].strip()
    return oldest if oldest != to_version else None


def _deployed_version(service: str, environment: str) -> tuple[str | None, dict[str, Any]]:
    """Ask the server itself (zero Claude calls) which version is running."""
    with McpStdioToolset.for_module(DEPLOYMENT_SERVER_MODULE) as toolset:
        result = toolset.call(
            "check_rollout_health",
            {"service": service, "environment": environment, "lookback_minutes": 5},
        )
    payload = json.loads(result.content)
    if not payload.get("ok"):
        return None, payload
    return payload["data"].get("version"), payload


def _deploy(version: str) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("docker is not on PATH; cannot --deploy")
    env = {**os.environ, "DEMO_GIT_SHA": version}
    print(f"  deploying  DEMO_GIT_SHA={version} docker compose up -d --wait")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [docker, "compose", "up", "-d", "--wait"], cwd=REPO_ROOT, env=env, check=True
    )
    print(f"  settling   {SCRAPE_SETTLE_SECONDS}s for Prometheus to scrape the new identity")
    time.sleep(SCRAPE_SETTLE_SECONDS)


def _evaluate(resp: Any, usage: Usage, service: str, to_version: str) -> list[str]:
    """Return the list of complaints. Empty means the done-when held live."""
    complaints: list[str] = []
    names = [tc.tool_name for tc in resp.tool_calls]
    if not resp.tool_calls:
        complaints.append("no tool call was made - the agent never read the deployment")
    if not any(tc.ok for tc in resp.tool_calls):
        complaints.append("no tool call succeeded")
    if not any(tc.server == "aioc-deployment" for tc in resp.tool_calls):
        complaints.append("tool calls are not attributed to the aioc-deployment server")
    if "diff_release" not in names:
        complaints.append("diff_release was never called")
    if "check_rollout_health" not in names:
        complaints.append("check_rollout_health was never called")
    f = resp.findings
    if f.service != service:
        complaints.append(f"findings.service is {f.service!r}, expected {service!r}")
    if f.releases_compared.to_version != to_version:
        complaints.append(
            f"releases_compared.to_version is {f.releases_compared.to_version!r}, "
            f"expected {to_version!r}"
        )
    health_ok = any(tc.tool_name == "check_rollout_health" and tc.ok for tc in resp.tool_calls)
    if health_ok and f.health_signals.replicas_ready is None:
        complaints.append("health succeeded but health_signals were not stamped")
    if not f.approval.requires_approval:
        complaints.append("approval.requires_approval is not true")
    if not resp.evidence:
        complaints.append("no evidence")
    if usage.input_tokens <= 0 or usage.output_tokens <= 0:
        complaints.append("cost is zero - the Usage accumulator did not thread through")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--from", dest="from_version", default=None, help="older release ref")
    parser.add_argument("--to", dest="to_version", default=None, help="deployed release ref")
    parser.add_argument("--query", default=None, help="override the query")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="recreate the demo containers at --to if they run something else",
    )
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    gh = GitHubSettings()
    if gh.token_value() is None or gh.repository is None:
        print("GITHUB_TOKEN and GITHUB_REPO must be set (shell or .env).", file=sys.stderr)
        return 2

    to_version = args.to_version or _git("rev-parse", "origin/main")
    if to_version is None:
        print("--to is required when git cannot resolve origin/main.", file=sys.stderr)
        return 2
    from_version = args.from_version or _default_from(to_version)
    if from_version is None:
        print("--from is required when git cannot resolve the default.", file=sys.stderr)
        return 2
    environment = DeploymentSettings().monitored_environment()
    service = args.service

    print(
        f"Day 12 deployment check - ~3-5 live Claude calls; {service} in {environment}, "
        f"{from_version[:10]} -> {to_version[:10]}\n"
    )

    deployed, probe = _deployed_version(service, environment)
    if not probe.get("ok"):
        error = probe.get("error", {})
        print(f"  the stack is not answering: {error.get('code')}: {error.get('message')}")
        print(f"  {error.get('remediation')}")
        return 2
    if deployed != to_version:
        print(f"  deployed   {deployed!r}, but the release under test is {to_version[:10]}")
        if not args.deploy:
            print(
                "  run with --deploy, or:  "
                f"DEMO_GIT_SHA={to_version} docker compose up -d --wait   (then re-run)"
            )
            return 2
        _deploy(to_version)
        deployed, probe = _deployed_version(service, environment)
        if deployed != to_version:
            print(f"  still {deployed!r} after deploy; is service_build_info being scraped?")
            return 2
    print(f"  deployed   {deployed[:10]} confirmed by check_rollout_health (0 Claude calls)")

    query = args.query or (
        f"Did release {to_version[:10]} of {service} change anything risky compared with "
        f"{from_version[:10]}, and is the current rollout healthy?"
    )
    context = CONTEXT_TEMPLATE.format(
        service=service,
        env=environment,
        repo=gh.repository,
        from_version=from_version,
        to_version=to_version,
    )

    agent = DeploymentAgent()
    usage = Usage()
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day12-deployment",
        command="check_day12_deployment.py",
        metadata={
            "query": query,
            "repository": gh.repository,
            "service": service,
            "from_version": from_version,
            "to_version": to_version,
        },
    ) as run:
        try:
            resp = agent.assess(query, context=context, usage=usage)
        except Exception as exc:
            run.event(
                "deployment",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            report = getattr(exc, "report", None)
            if report is not None:
                run.artifact("rejected_report.json", report.model_dump_json(indent=2))
                print(f"  the rejected report is saved as rejected_report.json in {run.dir}")
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        complaints = _evaluate(resp, usage, service, to_version)
        passed = not complaints
        f = resp.findings

        run.event(
            "deployment",
            outcome="passed" if passed else "failed",
            duration_ms=duration_ms,
            type="llm_call",
            data={
                "query": query,
                "status": resp.status.value,
                "tool_calls": [
                    {
                        "tool": tc.tool_name,
                        "ok": tc.ok,
                        "error_class": tc.error_class,
                        "ms": tc.duration_ms,
                        "tokens": tc.tokens_returned,
                    }
                    for tc in resp.tool_calls
                ],
                "releases": f.releases_compared.model_dump(),
                "changed_config_keys": f.changed_config_keys,
                "image_changes": len(f.image_changes),
                "health_signals": f.health_signals.model_dump(),
                "rollout_status": f.rollout_status.value,
                "regression_suspected": f.regression_suspected.value,
                "rollback_recommendation": f.rollback_recommendation.value,
                "risk": f.approval.risk.value,
                "evidence": len(resp.evidence),
                "gaps": len(resp.gaps),
                "overall_confidence": resp.overall_confidence,
                "usage": {"in": usage.input_tokens, "out": usage.output_tokens},
                "complaints": complaints,
            },
            message="; ".join(complaints)
            if complaints
            else "deployment agent reported from the wire",
        )
        run.artifact("deployment_response.json", resp.model_dump_json(indent=2))

        print(f"  status     {resp.status.value}")
        for tc in resp.tool_calls:
            mark = "ok" if tc.ok else f"ERR {tc.error_class.value if tc.error_class else '?'}"
            tokens = tc.tokens_returned
            print(f"  tool       {tc.tool_name} [{mark}] {tc.duration_ms} ms, ~{tokens} tok")
        print(
            f"  releases   {f.releases_compared.from_version} -> {f.releases_compared.to_version}"
        )
        print(f"  keys       {f.changed_config_keys}")
        for image in f.image_changes:
            print(f"  image      {image.container}: {image.from_image} -> {image.to_image}")
        h = f.health_signals
        print(
            f"  health     ready {h.replicas_ready}/{h.replicas_desired}, error_rate "
            f"{h.error_rate}, p99 {h.p99_latency_ms} ms, restarts {h.restart_count}, "
            f"probe failures {h.probe_failures}, over {h.observed_over_seconds}s"
        )
        print(f"  rollout    {f.rollout_status.value} ({f.rollout_status.confidence:.2f})")
        print(
            f"  regression {f.regression_suspected.value} ({f.regression_suspected.confidence:.2f})"
        )
        print(
            f"  recommend  {f.rollback_recommendation.value} "
            f"({f.rollback_recommendation.confidence:.2f}); risk {f.approval.risk.value}, "
            f"blast radius {f.approval.blast_radius}"
        )
        print(f"  summary    {resp.summary}")
        print(f"  evidence   {len(resp.evidence)}, gaps {len(resp.gaps)}")
        seconds = duration_ms / 1000
        print(f"  cost       {usage.input_tokens} in / {usage.output_tokens} out, {seconds:.1f}s")
        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- deployment agent: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
