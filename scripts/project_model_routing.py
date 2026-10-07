"""Model routing, projected at $0 (Day 23): what each coordinator/subagent model split would
have cost on two runs already measured.

    uv run python scripts/project_model_routing.py --plan     # free, offline
    uv run python scripts/project_model_routing.py --count    # free count_tokens calls
    uv run python scripts/project_model_routing.py            # offline, from what is saved
    uv run python scripts/project_model_routing.py --write    # and commit the report

No Claude completion is ever made. ``--count`` is the one network step: it sends the same
requests to the free token-counting endpoint once per model and saves every count, with
the four-agent run's measured total, to ``evaluations/routing/token-counts.json``, so the
projection itself always runs offline from committed files. What is counted:

- every role's cacheable prefix (system prompt plus tool schemas), which is also what the
  cache minimum check reads;
- the four-agent run's two coordinator calls, rebuilt from its record with the shipped
  code: the planning request (`planner.plan_prompt` over the recorded situation and query)
  and the synthesis request (`synthesis.render_prompt` over the recorded responses), and the
  plan the model returned. The run recorded only the request's total; the coordinator's
  share is these counts, and the agents' share is the total less them
  (`aioc.evals.routing.remainder`). The model synthesis was rejected on that run, so what it
  wrote was never kept: the deterministic synthesis that replaced it stands in, and the
  report says so;
- a prompt sample (the run's contexts) and an output sample (its agent reports), which with
  the prefixes give each model's tokenizer ratio to the source model.

Opus 5.5 and Sonnet 5.5 refuse a forced ``tool_choice`` even when counting, so nothing here
sends one; a forced call's small tool-use overhead is the same on every model and cancels
in a ratio. The method and what it cannot say are in `docs/design-notes/model-routing.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aioc.contracts import CoordinatorResponse
from aioc.coordinator.handoff import ROSTER_HEADER
from aioc.evals.routing import (
    ROUTES,
    TokenRatio,
    Workload,
    models_needed,
    project_all,
    record,
    remainder,
    render,
    render_ratios,
)
from aioc.llm import ToolSpec, Usage
from aioc.llm.config import Role
from aioc.llm.pricing import price

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "test-results" / "runs"
FOUR_AGENT_RUN = RUNS / "2026-09-30" / "20260930T184259Z__llm__day15-integration"
BASELINE = ROOT / "evaluations" / "baseline.json"
BASELINE_RUN = "realtime, cached"
OUT = ROOT / "evaluations" / "routing"
COUNTS = OUT / "token-counts.json"

SOURCE_MODEL = "claude-sonnet-5"

# One request, counted: the model id goes in at count time.
CountRequest = dict[str, Any]
Counter = Callable[[str, CountRequest], int]


# ------------------------------------------------------------------- what gets counted


def _wire(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [dict(t.to_api()) for t in tools]


def _server_tools(module: Any) -> list[dict[str, Any]]:
    """A stdio server's tools as the agent sends them, listed in process (no subprocess,
    no network - listing never calls the API behind the tools)."""
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in asyncio.run(module.list_tools())
    ]


def prefixes() -> dict[str, tuple[Role, str, list[dict[str, Any]]]]:
    """Every distinct call shape's cacheable prefix: name -> (role, system, tools)."""
    from aioc.agents import deployment, docs, github, incident
    from aioc.coordinator import planner, synthesis
    from aioc.tools.deployment import server as deployment_server
    from aioc.tools.github import server as github_server

    return {
        "planner": ("coordinator", planner.SELECTION_SYSTEM_PROMPT, _wire([planner._SELECT_TOOL])),
        "synthesis": (
            "coordinator",
            synthesis.SYNTHESIS_SYSTEM_PROMPT,
            _wire([synthesis._EMIT_TOOL]),
        ),
        "incident": (
            "subagent",
            incident.INCIDENT_STRUCTURED_SYSTEM_PROMPT,
            _wire([incident._EMIT_TOOL]),
        ),
        "docs": ("subagent", docs.DOCS_STRUCTURED_SYSTEM_PROMPT, _wire([docs._EMIT_TOOL])),
        "github": (
            "subagent",
            github.GITHUB_SYSTEM_PROMPT,
            _server_tools(github_server) + _wire([github._EMIT_TOOL]),
        ),
        "deployment": (
            "subagent",
            deployment.DEPLOYMENT_SYSTEM_PROMPT,
            _server_tools(deployment_server) + _wire([deployment._EMIT_TOOL]),
        ),
    }


def _event(run: Path) -> dict[str, Any]:
    line = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    data: dict[str, Any] = json.loads(line)["data"]
    return data


def _plan_payload(response: CoordinatorResponse) -> dict[str, Any]:
    """The plan as the model returned it: round-0 invocations with the planner's own block
    (the executor appends the roster and any handoff after it), and the skipped agents.
    The plan's own gaps are not recorded apart from the run's, so none are counted."""
    invocations = []
    for inv in response.selected_agents:
        if inv.round:
            continue
        block = inv.context_passed.split(f"\n\n{ROSTER_HEADER}", 1)[0]
        invocations.append(
            {
                "invocation_id": inv.invocation_id,
                "agent": inv.agent.value,
                "reason": inv.reason,
                "mode": inv.mode.value,
                "depends_on": list(inv.depends_on),
                "context_passed": block,
            }
        )
    return {
        "intent": response.intent.model_dump(mode="json"),
        "selected_agents": invocations,
        "skipped_agents": [s.model_dump(mode="json") for s in response.skipped_agents],
        "gaps": [],
    }


def coordinator_requests(run: Path) -> dict[str, CountRequest]:
    """The four-agent run's two coordinator calls rebuilt with the shipped code, and the
    two outputs: the plan, and the synthesis that was kept (the deterministic stand-in)."""
    from aioc.coordinator import planner, synthesis

    data = _event(run)
    situation = (run / "situation.txt").read_text(encoding="utf-8")
    response = CoordinatorResponse.model_validate_json(
        (run / "response.json").read_text(encoding="utf-8")
    )
    by_id = {inv.invocation_id: inv for inv in response.selected_agents}
    request = synthesis.SynthesisRequest(
        query=response.query,
        intent=response.intent,
        reports=[(by_id[r.invocation_id], r) for r in response.agent_responses],
        execution_gaps=[],
        unresolved_gaps=response.unresolved_gaps,
        refinement_rounds=response.refinement_rounds,
    )
    stand_in = {
        "synthesis": response.synthesis,
        "answer": response.answer.value,
        "confidence": response.answer.confidence,
        "evidence": response.answer.evidence,
        "reasoning": response.answer.reasoning,
    }
    _, plan_system, plan_tools = prefixes()["planner"]
    _, synthesis_system, synthesis_tools = prefixes()["synthesis"]
    return {
        "input:plan": {
            "system": plan_system,
            "tools": plan_tools,
            "messages": [
                {"role": "user", "content": planner.plan_prompt(data["query"], situation)}
            ],
        },
        "input:synthesis": {
            "system": synthesis_system,
            "tools": synthesis_tools,
            "messages": [{"role": "user", "content": synthesis.render_prompt(request)}],
        },
        "output:plan": {
            "messages": [{"role": "user", "content": json.dumps(_plan_payload(response))}]
        },
        "output:synthesis": {"messages": [{"role": "user", "content": json.dumps(stand_in)}]},
    }


def count_requests(run: Path) -> dict[str, CountRequest]:
    """Label -> request, the same for every model. ``prefix:`` and ``input:`` labels are
    prompts, ``output:`` labels are what a model writes; the prefix requests carry a
    one-character user turn because a request needs one."""
    requests: dict[str, CountRequest] = {}
    for name, (_, system, tools) in prefixes().items():
        requests[f"prefix:{name}"] = {
            "system": system,
            "tools": tools,
            "messages": [{"role": "user", "content": "."}],
        }
    requests.update(coordinator_requests(run))
    contexts = json.loads((run / "contexts.json").read_text(encoding="utf-8"))
    response = json.loads((run / "response.json").read_text(encoding="utf-8"))
    prompt_sample = "\n\n".join(c["context_passed"] for c in contexts)
    output_sample = "\n\n".join(json.dumps(r["findings"]) for r in response["agent_responses"])
    requests["input:contexts"] = {"messages": [{"role": "user", "content": prompt_sample}]}
    requests["output:reports"] = {"messages": [{"role": "user", "content": output_sample}]}
    return requests


def anthropic_counter() -> Counter:
    import anthropic

    from aioc.llm import LLMSettings

    key = LLMSettings().anthropic_api_key
    client = anthropic.Anthropic(api_key=key.get_secret_value() if key else None)

    def count(model: str, request: CountRequest) -> int:
        return int(client.messages.count_tokens(model=model, **request).input_tokens)

    return count


def count_all(run: Path, models: Sequence[str], counter: Counter) -> dict[str, Any]:
    requests = count_requests(run)
    data = _event(run)
    contexts = json.loads((run / "contexts.json").read_text(encoding="utf-8"))
    return {
        "counted_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_model": SOURCE_MODEL,
        "four_agent_run": {
            "run_id": run.name,
            "agent_invocations": len(contexts),
            "usage": data["usage"],
            "wall_seconds": data.get("wall_seconds"),
            "synthesis_source": data.get("synthesis_source"),
        },
        "counts": {m: {label: counter(m, req) for label, req in requests.items()} for m in models},
    }


def ratios_from(saved: Mapping[str, Any]) -> dict[str, TokenRatio]:
    """Each model's ratio to the source model over the same texts: prompts (``prefix:`` and
    ``input:``) and outputs (``output:``) apart."""
    counts: Mapping[str, Mapping[str, int]] = saved["counts"]
    source = counts[saved["source_model"]]
    prompts = [k for k in source if not k.startswith("output:")]
    outputs = [k for k in source if k.startswith("output:")]
    return {
        model: TokenRatio(
            input=TokenRatio.of([c[k] for k in prompts], [source[k] for k in prompts]),
            output=TokenRatio.of([c[k] for k in outputs], [source[k] for k in outputs]),
        )
        for model, c in counts.items()
        if model != saved["source_model"]
    }


def prefix_tokens(saved: Mapping[str, Any]) -> dict[Role, dict[str, int]]:
    """Each role's prefixes on the source model, by name."""
    source = saved["counts"][saved["source_model"]]
    roles: dict[str, Role] = {name: role for name, (role, _, _) in prefixes().items()}
    out: dict[Role, dict[str, int]] = {}
    for label, n in source.items():
        if label.startswith("prefix:"):
            name = label.removeprefix("prefix:")
            out.setdefault(roles[name], {})[name] = n
    return out


# ------------------------------------------------------------------------ the two workloads


def four_agent_workload(saved: Mapping[str, Any]) -> Workload:
    """The run's measured total, split: the coordinator's two calls as counted, and the
    agents' share as what is left. Each coordinator call is a single forced call that writes
    its prefix to the cache and reads nothing back, which is how the cache counters split."""
    run = saved["four_agent_run"]
    u = run["usage"]
    total = Usage(
        input_tokens=u["input_tokens"],
        output_tokens=u["output_tokens"],
        cache_read_tokens=u.get("cache_read_tokens"),
        cache_write_tokens=u.get("cache_write_tokens"),
    )
    source = saved["counts"][saved["source_model"]]
    cached = total.cache_write_tokens is not None
    coordinator = Usage(
        input_tokens=source["input:plan"] + source["input:synthesis"],
        output_tokens=source["output:plan"] + source["output:synthesis"],
        cache_read_tokens=0 if cached else None,
        cache_write_tokens=(source["prefix:planner"] + source["prefix:synthesis"])
        if cached
        else None,
    )
    stand_in = (
        "the model synthesis was rejected on this run and its output was never kept; the "
        f"deterministic synthesis that replaced it ({source['output:synthesis']:,} tokens) "
        "stands in for it"
        if run.get("synthesis_source") != "model"
        else None
    )
    return Workload(
        name=f"Four agents on one query ({run['run_id'][:8]}, Day 21)",
        source_model=saved["source_model"],
        usage={"coordinator": coordinator, "subagent": remainder(total, coordinator)},
        invocations={"coordinator": 2, "subagent": run["agent_invocations"]},
        prefixes=prefix_tokens(saved),
        wall_seconds=run.get("wall_seconds"),
        notes=tuple(
            n
            for n in (
                f"as run: ${price(saved['source_model'], total) or 0:.2f} with caching on",
                "the run recorded only its total: the coordinator's two calls are rebuilt "
                "from the record and counted, the agents' share is what is left",
                stand_in,
            )
            if n
        ),
    )


def eval_workload(saved: Mapping[str, Any]) -> Workload:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    run = next(r for r in baseline["runs"] if r["label"] == BASELINE_RUN)
    t = run["tokens"]
    usage = Usage(
        input_tokens=t["in"],
        output_tokens=t["out"],
        cache_read_tokens=t["cache_read"],
        cache_write_tokens=t["cache_write"],
    )
    rates = run["rates"]
    fm, cited = rates["failure_mode"], rates["recall_cited"]
    subagent = prefix_tokens(saved).get("subagent", {})
    return Workload(
        name=f"The eval set, {BASELINE_RUN} (Day 20)",
        source_model=baseline["model"],
        usage={"subagent": usage},
        invocations={"subagent": run["items"]},
        prefixes={"subagent": {k: v for k, v in subagent.items() if k in ("incident", "docs")}},
        wall_seconds=run["seconds"],
        notes=(
            "subagents only (Incident and Docs), so only the subagent model matters here",
            f"quality was measured on {baseline['model']} only: failure mode "
            f"{fm['hits']}/{fm['total']}, recall cited {cited['hits']}/{cited['total']}",
        ),
    )


# ----------------------------------------------------------------------------------- main


def report(saved: Mapping[str, Any], workloads: Sequence[Workload]) -> tuple[str, dict[str, Any]]:
    ratios = ratios_from(saved)
    parts = [
        "# Model routing, projected",
        "",
        "Generated by `scripts/project_model_routing.py` from recorded runs and free token "
        f"counts ({saved['counted_at']}). **A projection, not a measurement**: cost only; "
        "latency and quality per model need calls and are not estimated. Method and limits: "
        "`docs/design-notes/model-routing.md`.",
        "",
        "## Tokenizer ratios",
        "",
        render_ratios(saved["source_model"], ratios),
        "",
        "## Routes",
        "",
    ]
    records = []
    for w in workloads:
        costs = project_all(w, ratios)
        parts += [render(w, costs, ratios), ""]
        records.append(record(w, costs))
    data = {
        "counted_at": saved["counted_at"],
        "source_model": saved["source_model"],
        "ratios": {m: {"input": r.input, "output": r.output} for m, r in ratios.items()},
        "workloads": records,
    }
    return "\n".join(parts).rstrip() + "\n", data


def main(argv: Sequence[str] | None = None, *, counter: Counter | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="say what --count would ask")
    parser.add_argument("--count", action="store_true", help="count tokens per model (free)")
    parser.add_argument("--write", action="store_true", help="write the report under evaluations/")
    parser.add_argument("--run", type=Path, default=FOUR_AGENT_RUN, help="four-agent run record")
    args = parser.parse_args(argv)

    models = models_needed(ROUTES)
    if SOURCE_MODEL not in models:
        models.insert(0, SOURCE_MODEL)

    if args.plan:
        requests = count_requests(args.run)
        print(
            f"count_tokens: {len(requests)} requests x {len(models)} models = "
            f"{len(requests) * len(models)} calls, free (separate rate limit)"
        )
        for label in requests:
            print(f"  {label}")
        print(f"  models: {', '.join(models)}")
        print(f"saved counts: {'yes' if COUNTS.exists() else 'no'} ({COUNTS.relative_to(ROOT)})")
        return 0

    if args.count:
        saved = count_all(args.run, models, counter or anthropic_counter())
        OUT.mkdir(parents=True, exist_ok=True)
        COUNTS.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {COUNTS.relative_to(ROOT)}")

    if not COUNTS.exists():
        raise SystemExit(f"{COUNTS.relative_to(ROOT)} is missing; run --count (free) first")
    saved = json.loads(COUNTS.read_text(encoding="utf-8"))
    text, data = report(saved, [four_agent_workload(saved), eval_workload(saved)])
    print(text)
    if args.write:
        (OUT / "projection.md").write_text(text, encoding="utf-8")
        (OUT / "projection.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {(OUT / 'projection.md').relative_to(ROOT)} and projection.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
