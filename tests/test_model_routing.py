"""Day 23: model routing - the per-role model settings, and the cost projection at $0.

Offline: no API key, no network. The settings half proves routing is a configuration
change: each role's override reaches the code that role runs, from the shell or from `.env`
(before Day 23 the coordinator's override was read from the shell only and the subagents'
was not read at all). The projection half proves the arithmetic against hand-computed
prices and the script end to end against a scripted token counter over a run record of the
real shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import project_model_routing as script
import pytest

from aioc.agents.deployment import DeploymentAgent
from aioc.agents.docs import DocsAgent
from aioc.agents.github import GitHubAgent
from aioc.agents.incident import IncidentAgent
from aioc.coordinator.planner import Coordinator
from aioc.coordinator.synthesis import ModelSynthesiser
from aioc.evals.routing import (
    CACHE_MINIMUM_TOKENS,
    FORCED_TOOL_CHOICE_REFUSED,
    ROUTES,
    Route,
    TokenRatio,
    Workload,
    models_needed,
    project,
    project_all,
    record,
    remainder,
    render,
    rescale,
)
from aioc.llm import LLMSettings, Usage
from aioc.llm.pricing import price, price_uncached

# ------------------------------------------------------------------ the role settings


@pytest.fixture
def no_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from an empty directory with no model variables in the shell, so neither the
    developer's `.env` nor their shell decides what a test sees."""
    monkeypatch.chdir(tmp_path)
    for var in ("AIOC_MODEL", "AIOC_COORDINATOR_MODEL", "AIOC_SUBAGENT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-key")
    return tmp_path


def test_without_overrides_both_roles_run_the_harness_model(no_dotenv: Path) -> None:
    settings = LLMSettings()
    assert settings.for_role("coordinator").model == "claude-sonnet-5"
    assert settings.for_role("subagent").model == "claude-sonnet-5"
    assert settings.for_role("coordinator") is settings  # nothing to copy


def test_each_override_moves_its_own_role_only(no_dotenv: Path) -> None:
    settings = LLMSettings(coordinator_model="claude-opus-5")
    assert settings.for_role("coordinator").model == "claude-opus-5"
    assert settings.for_role("subagent").model == "claude-sonnet-5"
    settings = LLMSettings(subagent_model="claude-haiku-4-5", max_tokens=1234)
    sub = settings.for_role("subagent")
    assert sub.model == "claude-haiku-4-5" and sub.max_tokens == 1234  # the rest is shared
    assert settings.for_role("coordinator").model == "claude-sonnet-5"


def test_the_overrides_are_read_from_dotenv(no_dotenv: Path) -> None:
    # The pre-Day-23 defect: `.env.example` said to set AIOC_COORDINATOR_MODEL in `.env`,
    # and the planner read it from os.environ only, so the line did nothing.
    (no_dotenv / ".env").write_text(
        "AIOC_COORDINATOR_MODEL=claude-opus-5\nAIOC_SUBAGENT_MODEL=claude-haiku-4-5\n",
        encoding="utf-8",
    )
    settings = LLMSettings()
    assert settings.for_role("coordinator").model == "claude-opus-5"
    assert settings.for_role("subagent").model == "claude-haiku-4-5"


def test_every_component_runs_its_roles_model(
    no_dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Planning and the model-written synthesis are both the coordinator's calls; before
    # Day 23 the synthesiser ran the subagents' model.
    monkeypatch.setenv("AIOC_COORDINATOR_MODEL", "claude-opus-5")
    monkeypatch.setenv("AIOC_SUBAGENT_MODEL", "claude-haiku-4-5")
    for coordinator_side in (Coordinator(), ModelSynthesiser()):
        assert coordinator_side._client.settings.model == "claude-opus-5"
    for agent in (IncidentAgent(), DocsAgent(), GitHubAgent(), DeploymentAgent()):
        assert agent._client.settings.model == "claude-haiku-4-5"


# ------------------------------------------------------------------- the projection


SOURCE = "claude-sonnet-5"
HAIKU = TokenRatio(input=0.8, output=0.75)
SAME = TokenRatio(input=1.0, output=1.0)
RATIOS = {
    "claude-opus-5": SAME,
    "claude-haiku-4-5": HAIKU,
    "claude-opus-5-5": SAME,
    "claude-sonnet-5-5": SAME,
}


def _workload(**over: Any) -> Workload:
    base: dict[str, Any] = {
        "name": "w",
        "source_model": SOURCE,
        "usage": {
            "coordinator": Usage(
                input_tokens=10_000,
                output_tokens=2_000,
                cache_read_tokens=0,
                cache_write_tokens=4_000,
            ),
            "subagent": Usage(
                input_tokens=300_000,
                output_tokens=40_000,
                cache_read_tokens=190_000,
                cache_write_tokens=100_000,
            ),
        },
        "invocations": {"coordinator": 2, "subagent": 5},
        "prefixes": {"coordinator": {"planner": 3_000}, "subagent": {"incident": 7_300}},
    }
    base.update(over)
    return Workload(**base)


def test_the_route_as_run_prices_exactly_what_was_measured() -> None:
    w = _workload()
    cost = project(w, ROUTES[0], RATIOS)
    expected = price(SOURCE, w.usage["coordinator"]) + price(SOURCE, w.usage["subagent"])  # type: ignore[operator]
    assert cost.usd == pytest.approx(expected)
    assert cost.runnable and cost.notes == ()
    assert [r.usage for r in cost.roles] == [w.usage["coordinator"], w.usage["subagent"]]


def test_a_route_reprices_each_role_on_its_own_model_by_hand() -> None:
    w = _workload()
    cost = project(w, Route("x", "claude-opus-5", "claude-haiku-4-5"), RATIOS)
    coordinator, subagent = cost.roles
    # Opus 5 at $5 / $25, cache write 1.25x; same tokenizer as the source.
    assert coordinator.usd == pytest.approx((6_000 * 5 + 4_000 * 5 * 1.25 + 2_000 * 25) / 1e6)
    # Haiku counts 0.8x the prompt and 0.75x the output; cache counters keep their share.
    assert subagent.usage == Usage(
        input_tokens=240_000,
        output_tokens=30_000,
        cache_read_tokens=152_000,
        cache_write_tokens=80_000,
    )
    assert subagent.usd == pytest.approx(
        (8_000 * 1 + 80_000 * 1 * 1.25 + 152_000 * 0.1 + 30_000 * 5) / 1e6
    )
    assert cost.usd == pytest.approx(coordinator.usd + subagent.usd)


def test_the_ceiling_prices_every_prompt_token_uncached() -> None:
    cost = project(_workload(), ROUTES[2], RATIOS)
    assert cost.usd_without_cache == pytest.approx(
        sum(price_uncached(r.model, r.usage) or 0 for r in cost.roles)
    )
    assert cost.usd_without_cache > cost.usd  # the run read its cache more than it wrote


def test_a_model_that_refuses_forced_tool_choice_is_priced_but_not_runnable() -> None:
    cost = project(_workload(), Route("x", "claude-opus-5-5", "claude-sonnet-5-5"), RATIOS)
    assert not cost.runnable
    assert cost.usd > 0
    assert any("forced tool_choice" in n for n in cost.notes)
    assert {"claude-opus-5-5", "claude-sonnet-5-5"} <= FORCED_TOOL_CHOICE_REFUSED
    # ...and the plan's own Opus is the one the harness can call today.
    assert project(_workload(), ROUTES[1], RATIOS).runnable


def test_a_prefix_under_the_models_cache_minimum_is_named() -> None:
    # 5,000 tokens on Sonnet is 4,000 on Haiku at 0.8 - under Haiku 4.5's 4096 minimum.
    w = _workload(prefixes={"subagent": {"github": 5_000, "incident": 7_300}})
    cost = project(w, Route("x", SOURCE, "claude-haiku-4-5"), RATIOS)
    subagent = cost.roles[1]
    assert subagent.uncached_prefixes == ("github",)
    assert any("github below the 4096-token cache minimum" in n for n in cost.notes)
    assert CACHE_MINIMUM_TOKENS["claude-haiku-4-5"] == 4096
    # The same prefix on Sonnet (1024 minimum) is fine.
    assert project(w, ROUTES[0], RATIOS).roles[1].uncached_prefixes == ()


def test_a_model_with_no_ratio_is_refused_not_assumed() -> None:
    with pytest.raises(KeyError, match="no token ratio for claude-haiku-4-5"):
        project(_workload(), ROUTES[2], {"claude-opus-5": SAME})


def test_a_model_with_no_price_is_refused() -> None:
    with pytest.raises(KeyError, match="no listed price"):
        project(_workload(), Route("x", "claude-unknown-9", SOURCE), {"claude-unknown-9": SAME})


def test_rescale_keeps_an_unreported_cache_counter_unreported() -> None:
    assert rescale(Usage(input_tokens=100, output_tokens=10), HAIKU) == Usage(
        input_tokens=80, output_tokens=8
    )


def test_a_ratio_needs_a_source_count() -> None:
    assert TokenRatio.of([130, 70], [100, 100]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        TokenRatio.of([1], [0])


def test_remainder_is_the_total_less_the_part_on_every_counter() -> None:
    total = Usage(
        input_tokens=1_000, output_tokens=100, cache_read_tokens=600, cache_write_tokens=200
    )
    part = Usage(input_tokens=300, output_tokens=40, cache_read_tokens=0, cache_write_tokens=150)
    assert remainder(total, part) == Usage(
        input_tokens=700, output_tokens=60, cache_read_tokens=600, cache_write_tokens=50
    )
    # A counter the total never reported stays unreported.
    assert remainder(Usage(input_tokens=10, output_tokens=1), Usage(input_tokens=4)) == Usage(
        input_tokens=6, output_tokens=1
    )


def test_a_part_larger_than_the_whole_is_refused() -> None:
    # A rebuilt coordinator call that counts more than the request paid for was rebuilt wrong.
    with pytest.raises(ValueError, match="cache_write_tokens: the part"):
        remainder(
            Usage(input_tokens=10, output_tokens=1, cache_write_tokens=2),
            Usage(input_tokens=5, cache_write_tokens=3),
        )
    with pytest.raises(ValueError, match="never reported"):
        remainder(Usage(input_tokens=10), Usage(input_tokens=5, cache_read_tokens=1))


def test_a_subagent_only_workload_prices_each_subagent_model_once() -> None:
    w = _workload(usage={"subagent": Usage(input_tokens=1_000, output_tokens=100)})
    costs = project_all(w, RATIOS)
    assert [c.route.subagent for c in costs] == [
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
    ]
    text = render(w, costs, RATIOS)
    assert "| subagents on haiku-4-5 | - | haiku-4-5 |" in text
    assert "Opus coordinator" not in text  # no route named for a role that was not priced


def test_models_needed_lists_each_model_once_in_order() -> None:
    models = models_needed(ROUTES)
    assert models[0] == SOURCE and len(models) == len(set(models))
    assert set(models) == {m for r in ROUTES for m in (r.coordinator, r.subagent)}


def test_the_plans_two_routes_are_projected() -> None:
    names = {(r.coordinator, r.subagent) for r in ROUTES}
    assert ("claude-opus-5", "claude-sonnet-5") in names
    assert ("claude-opus-5", "claude-haiku-4-5") in names
    assert (ROUTES[0].coordinator, ROUTES[0].subagent) == (SOURCE, SOURCE)


def test_render_and_record_carry_every_route() -> None:
    w = _workload(notes=("a note",))
    costs = project_all(w, RATIOS)
    text = render(w, costs, RATIOS)
    for c in costs:
        assert f"| {c.route.name} |" in text
    assert "- a note" in text and "| no |" in text and "| yes |" in text
    data = json.loads(json.dumps(record(w, costs)))
    assert [r["name"] for r in data["routes"]] == [r.name for r in ROUTES]
    assert data["routes"][0]["usd"] == pytest.approx(costs[0].usd, abs=1e-6)


# ---------------------------------------------------------------- the script, offline

QUERY = "Why is checkout-api slow, and what do our runbooks say about it?"
SITUATION = "checkout-api p99 rose from 120ms to 2100ms at 14:00 UTC."
RUN_USAGE = {
    "input_tokens": 2_000_000,
    "output_tokens": 50_000,
    "cache_read_tokens": 900_000,
    "cache_write_tokens": 400_000,
}


def _fixture_run(tmp: Path, *, synthesis_source: str = "deterministic_fallback") -> Path:
    """A four-agent-run record of the real shape: the response is what the executor builds
    from a two-agent plan, and the contexts are what it recorded."""
    from aioc.contracts import AgentName
    from aioc.coordinator import Executor
    from tests.test_executor import _invocation, _plan, _RecordingRunner

    plan = _plan([_invocation("incident"), _invocation("docs")])
    response = Executor(
        {AgentName.INCIDENT: _RecordingRunner(), AgentName.DOCS: _RecordingRunner()}
    ).execute(plan, QUERY)
    run = tmp / "20260930T184259Z__llm__day15-integration"
    run.mkdir(parents=True)
    event = {
        "data": {
            "query": QUERY,
            "usage": RUN_USAGE,
            "wall_seconds": 400.0,
            "synthesis_source": synthesis_source,
        }
    }
    (run / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (run / "situation.txt").write_text(SITUATION, encoding="utf-8")
    (run / "response.json").write_text(response.model_dump_json(), encoding="utf-8")
    (run / "contexts.json").write_text(
        json.dumps([{"context_passed": i.context_passed} for i in response.selected_agents]),
        encoding="utf-8",
    )
    return run


def _fixture_baseline(tmp: Path) -> Path:
    path = tmp / "baseline.json"
    run = {
        "label": script.BASELINE_RUN,
        "items": 38,
        "seconds": 800.0,
        "tokens": {"in": 400_000, "out": 100_000, "cache_read": 280_000, "cache_write": 20_000},
        "rates": {
            "failure_mode": {"hits": 16, "total": 18},
            "recall_cited": {"hits": 18, "total": 18},
        },
    }
    path.write_text(json.dumps({"model": SOURCE, "runs": [run]}), encoding="utf-8")
    return path


@pytest.fixture
def scripted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    out = tmp_path / "routing"
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(script, "OUT", out)
    monkeypatch.setattr(script, "COUNTS", out / "token-counts.json")
    monkeypatch.setattr(script, "BASELINE", _fixture_baseline(tmp_path))
    calls: list[tuple[str, dict[str, Any]]] = []

    def counter(model: str, request: dict[str, Any]) -> int:
        """Three characters a token, Haiku counting 0.8 of that - a stand-in tokenizer."""
        calls.append((model, request))
        assert "tool_choice" not in request  # Opus 5.5 / Sonnet 5.5 would refuse it
        scale = 0.8 if model == "claude-haiku-4-5" else 1.0
        return max(1, round(len(json.dumps(request)) / 3 * scale))

    return {"run": _fixture_run(tmp_path), "out": out, "counter": counter, "calls": calls}


def test_plan_is_free_and_says_what_count_would_ask(
    scripted: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["--plan", "--run", str(scripted["run"])], counter=scripted["counter"]) == 0
    out = capsys.readouterr().out
    n = len(models_needed(ROUTES))
    assert f"12 requests x {n} models = {12 * n} calls, free" in out
    assert scripted["calls"] == []  # --plan counts nothing


def test_the_projection_refuses_to_run_before_anything_is_counted(
    scripted: dict[str, Any],
) -> None:
    with pytest.raises(SystemExit, match="token-counts.json is missing"):
        script.main(["--run", str(scripted["run"])], counter=scripted["counter"])


def test_the_coordinator_calls_are_rebuilt_with_the_shipped_code(scripted: dict[str, Any]) -> None:
    from aioc.coordinator import planner, synthesis
    from aioc.coordinator.handoff import ROSTER_HEADER

    requests = script.coordinator_requests(scripted["run"])
    plan = requests["input:plan"]
    assert plan["system"] == planner.SELECTION_SYSTEM_PROMPT
    assert plan["messages"][0]["content"] == planner.plan_prompt(QUERY, SITUATION)
    assert [t["name"] for t in plan["tools"]] == [planner.SELECT_TOOL_NAME]
    synth = requests["input:synthesis"]
    assert synth["system"] == synthesis.SYNTHESIS_SYSTEM_PROMPT
    prompt = synth["messages"][0]["content"]
    assert prompt.startswith(f"<query>\n{QUERY}\n</query>")
    assert 'invocation_id="inv_incident"' in prompt and 'invocation_id="inv_docs"' in prompt
    # The plan as the model wrote it: the planner's block, without what the executor added.
    payload = json.loads(requests["output:plan"]["messages"][0]["content"])
    assert [i["agent"] for i in payload["selected_agents"]] == ["incident", "docs"]
    assert all(ROSTER_HEADER not in i["context_passed"] for i in payload["selected_agents"])
    assert {s["agent"] for s in payload["skipped_agents"]} == {"github", "deployment"}


def test_count_and_write_end_to_end(
    scripted: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    run, out, counter = scripted["run"], scripted["out"], scripted["counter"]
    assert script.main(["--count", "--write", "--run", str(run)], counter=counter) == 0
    models = models_needed(ROUTES)
    assert len(scripted["calls"]) == 12 * len(models)
    saved = json.loads((out / "token-counts.json").read_text(encoding="utf-8"))
    assert set(saved["counts"]) == set(models)
    assert saved["four_agent_run"]["usage"] == RUN_USAGE
    assert saved["four_agent_run"]["agent_invocations"] == 2
    source = saved["counts"][SOURCE]

    data = json.loads((out / "projection.json").read_text(encoding="utf-8"))
    four, evals = data["workloads"]
    coordinator, subagent = four["measured"]["coordinator"], four["measured"]["subagent"]
    # The coordinator is its two rebuilt calls as counted; it writes its prefixes, reads none.
    assert coordinator["input_tokens"] == source["input:plan"] + source["input:synthesis"]
    assert coordinator["output_tokens"] == source["output:plan"] + source["output:synthesis"]
    assert (
        coordinator["cache_write_tokens"] == source["prefix:planner"] + source["prefix:synthesis"]
    )
    assert coordinator["cache_read_tokens"] == 0
    # The agents are what is left, on every counter.
    for key, total in RUN_USAGE.items():
        assert coordinator[key] + subagent[key] == total
    assert [r["name"] for r in four["routes"]] == [r.name for r in ROUTES]
    assert evals["measured"] == {
        "subagent": {
            "invocations": 38,
            "input_tokens": 400_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 280_000,
            "cache_write_tokens": 20_000,
        }
    }
    assert data["ratios"]["claude-haiku-4-5"]["input"] == pytest.approx(0.8, abs=0.01)
    text = (out / "projection.md").read_text(encoding="utf-8")
    assert "A projection, not a measurement" in text and "failure mode 16/18" in text
    assert "the deterministic synthesis that replaced it" in text
    # The saved counts make the next run offline: nothing is counted again.
    scripted["calls"].clear()
    assert script.main(["--run", str(run)], counter=counter) == 0
    assert scripted["calls"] == []
    assert "Four agents on one query" in capsys.readouterr().out


def test_a_kept_model_synthesis_needs_no_stand_in_note(
    tmp_path: Path, scripted: dict[str, Any]
) -> None:
    run = _fixture_run(tmp_path / "other", synthesis_source="model")
    script.main(["--count", "--write", "--run", str(run)], counter=scripted["counter"])
    text = (scripted["out"] / "projection.md").read_text(encoding="utf-8")
    assert "stands in" not in text
