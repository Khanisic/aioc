"""Day 11 done-when, live: does the GitHub agent read a real PR through the MCP wire and
return a contract-valid, grounded report?

    uv run python scripts/check_day11_github.py                # ~3-5 live Claude calls
    uv run python scripts/check_day11_github.py --pr 9
    uv run python scripts/check_day11_github.py --query "..."

Cost: one Claude call per tool-loop round (typically 2-3: fetch, maybe read the patch, then
stop) plus one forced `emit_github_report` call. GitHub reads are free within the rate
limit. Needs `GITHUB_TOKEN` (read-only fine-grained PAT) and `GITHUB_REPO` in `.env`; the
Docker stack is not needed - this agent reads GitHub, not the corpus.

**What this checks that the offline tests cannot.** `tests/test_github_agent.py` proves the
stamping and grounding logic against scripted payloads and `tests/test_mcp_toolset.py`
proves the wire; here the *model* drives the real `aioc-github` server and writes the
report, so what is being proven is that a real model, given real tool envelopes, produces
a `GitHubAgentResponse` whose every PR, commit, and excerpt traces back to a tool reply -
the agent's own checks raise otherwise, so a passing run *is* the proof.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import GitHubAgent  # noqa: E402
from aioc.llm import LLMSettings, Usage  # noqa: E402
from aioc.tools.github.api import GitHubSettings  # noqa: E402

DEFAULT_PR = 12

CONTEXT_TEMPLATE = (
    "Change-analysis question; no live incident is in progress. Repository under review: "
    "{repo}. The pull request of interest is #{pr}. Report what it changed, how risky it "
    "is to run, and whether anything in it could affect a running service."
)


def _evaluate(resp: Any, usage: Usage, pr: int) -> list[str]:
    """Return the list of complaints. Empty means the done-when held live."""
    complaints: list[str] = []
    if not resp.tool_calls:
        complaints.append("no tool call was made - the agent never read GitHub")
    if not any(tc.ok for tc in resp.tool_calls):
        complaints.append("no tool call succeeded")
    if not any(tc.server == "aioc-github" for tc in resp.tool_calls):
        complaints.append("tool calls are not attributed to the aioc-github server")
    numbers = [p.number for p in resp.findings.pull_requests]
    if pr not in numbers:
        complaints.append(f"PR #{pr} is not in findings.pull_requests ({numbers})")
    for p in resp.findings.pull_requests:
        if not p.head_sha or not p.title:
            complaints.append(f"PR #{p.number} facts were not stamped")
    if resp.findings.diff_summary.value is None and resp.status.value not in (
        "insufficient_evidence",
        "error",
    ):
        complaints.append("diff_summary.value is null on a non-insufficient status")
    if not resp.evidence:
        complaints.append("no evidence")
    if not all(
        e.tool_call_id for e in resp.evidence if e.source_type.value in ("commit", "pull_request")
    ):
        complaints.append("an evidence entry is missing its stamped tool_call_id")
    if usage.input_tokens <= 0 or usage.output_tokens <= 0:
        complaints.append("cost is zero - the Usage accumulator did not thread through")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, default=DEFAULT_PR, help="pull request number")
    parser.add_argument("--query", default=None, help="override the query")
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    gh = GitHubSettings()
    if gh.token_value() is None or gh.repository is None:
        print("GITHUB_TOKEN and GITHUB_REPO must be set (shell or .env).", file=sys.stderr)
        return 2

    query = args.query or f"What did PR #{args.pr} change, and how risky is it to run?"
    context = CONTEXT_TEMPLATE.format(repo=gh.repository, pr=args.pr)
    print(f"Day 11 github check - ~3-5 live Claude calls; repo {gh.repository}, PR #{args.pr}\n")

    agent = GitHubAgent()
    usage = Usage()
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day11-github",
        command="check_day11_github.py",
        metadata={"query": query, "repository": gh.repository, "pr": args.pr},
    ) as run:
        try:
            resp = agent.analyze(query, context=context, usage=usage)
        except Exception as exc:
            run.event(
                "github",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        complaints = _evaluate(resp, usage, args.pr)
        passed = not complaints

        run.event(
            "github",
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
                    }
                    for tc in resp.tool_calls
                ],
                "pull_requests": [p.number for p in resp.findings.pull_requests],
                "commits": len(resp.findings.commits),
                "suspect_changes": len(resp.findings.suspect_changes),
                "evidence": len(resp.evidence),
                "gaps": len(resp.gaps),
                "overall_confidence": resp.overall_confidence,
                "usage": {"in": usage.input_tokens, "out": usage.output_tokens},
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "github agent reported from the wire",
        )
        run.artifact("github_response.json", resp.model_dump_json(indent=2))

        print(f"  status     {resp.status.value}")
        for tc in resp.tool_calls:
            mark = "ok" if tc.ok else f"ERR {tc.error_class.value if tc.error_class else '?'}"
            tok = tc.tokens_returned
            print(f"  tool       {tc.tool_name} [{mark}] {tc.duration_ms} ms, ~{tok} tok")
        for p in resp.findings.pull_requests:
            print(
                f"  pr         #{p.number} {p.state.value} {p.head_sha[:7]} "
                f"{p.files_changed} files +{p.additions}/-{p.deletions}; risk "
                f"{p.risk.value} ({p.risk.confidence:.2f})"
            )
            print(f"             {p.summary.value}")
        print(f"  commits    {len(resp.findings.commits)} stamped")
        for s in resp.findings.suspect_changes:
            link = s.symptom_link.confidence
            print(f"  suspect    {s.change_ref} {s.change_type.value} ({link:.2f})")
        print(
            f"  diff       ({resp.findings.diff_summary.confidence:.2f}) "
            f"{resp.findings.diff_summary.value}"
        )
        print(f"  evidence   {len(resp.evidence)}, gaps {len(resp.gaps)}")
        seconds = duration_ms / 1000
        print(f"  cost       {usage.input_tokens} in / {usage.output_tokens} out, {seconds:.1f}s")
        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- github agent: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
