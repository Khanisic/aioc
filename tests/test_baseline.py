"""Day 20 baseline, offline: the cache verdict, runs side by side, and the checkpoint script.

The live half is `scripts/check_day20_baseline.py` itself. What is tested here is what the
baseline's numbers depend on being right:

- the cache verdict says `NOT WORKING` for the one case that costs money and breaks
  nothing, and declines to judge the cases it cannot;
- runs that are not comparable are refused, each for its own stated reason;
- the two cost deltas are the two different things the report says they are;
- the checkpoint stops after the smoke test when the smoke test fails, and writes nothing
  under `evaluations/` for a run on part of the set.

The scripted model here bills like the real one: with the cache marker on the request, the
first request for each agent writes the prefix and the later ones read it; without the
marker nothing is cached.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import check_day20_baseline
import eval_baseline
import pytest
import run_evals
from pydantic import SecretStr
from runlog import RunRecorder

from aioc.evals import (
    REFERENCE,
    BaselineError,
    EvalRun,
    build_baseline,
    cache_health,
    compare,
    render_baseline,
    render_comparison,
    render_markdown,
    run_batch,
    run_label,
    run_realtime,
    to_record,
)
from aioc.llm import DeferredClient, LLMClient, LLMSettings
from tests.test_batch import _batcher, _FakeBatches, _succeeded
from tests.test_evals import CASES, ITEMS, _answer, _Corpus, _item_of, _refused

PREFIX = 4_000  # tokens in the shared prefix: system prompt plus emit schema
REST = 200  # tokens after it: the context and the question

_BASE = LLMSettings(
    anthropic_api_key=SecretStr("sk-ant-test-not-a-key"),
    model="claude-sonnet-5",
    max_tokens=2048,
    prompt_caching=True,
    max_validation_retries=2,
)


def _settings(*, caching: bool, **changes: Any) -> LLMSettings:
    return _BASE.model_copy(update={"prompt_caching": caching, **changes})


# ------------------------------------------------------------------------------- fakes


class _Billing:
    """Bills a request the way the API does. One instance per run: what has been written
    is remembered, so the first request for an agent writes and the rest read."""

    def __init__(
        self, *, reads: bool = True, counters: bool = True, wrong: set[str] | None = None
    ) -> None:
        self._reads = reads
        self._counters = counters
        self._wrong = wrong or set()
        self._written: set[str] = set()

    def answer(self, params: dict[str, Any]) -> SimpleNamespace:
        item = _item_of(params)
        message = copy.copy(_answer(params))
        if item.key in self._wrong:
            message = copy.copy(_answer_wrong(params))
        marked = isinstance(params.get("system"), list)
        usage: dict[str, int] = {"output_tokens": 900}
        if not marked:
            usage.update(input_tokens=PREFIX + REST)
            if self._counters:
                usage.update(cache_creation_input_tokens=0, cache_read_input_tokens=0)
        elif item.agent in self._written and self._reads:
            usage.update(
                input_tokens=REST, cache_creation_input_tokens=0, cache_read_input_tokens=PREFIX
            )
        else:
            self._written.add(item.agent)
            usage.update(
                input_tokens=REST, cache_creation_input_tokens=PREFIX, cache_read_input_tokens=0
            )
        if not self._counters:
            usage = {"input_tokens": PREFIX + REST, "output_tokens": 900}
        message.usage = SimpleNamespace(**usage)
        return message


def _answer_wrong(params: dict[str, Any]) -> SimpleNamespace:
    """The scripted model's answer with the failure mode swapped for a wrong one."""
    from aioc.agents import EMIT_TOOL_NAME
    from tests.test_evals import _diagnosis, _message

    item = _item_of(params)
    assert item.incident is not None
    wrong = "other" if item.incident.true_failure_mode != "other" else "code_regression"
    return _message(EMIT_TOOL_NAME, _diagnosis(item, mode=wrong))


class _Wire:
    """An Anthropic client over a `_Billing`."""

    def __init__(self, billing: _Billing) -> None:
        self.calls: list[dict[str, Any]] = []
        self._billing = billing
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._billing.answer(kwargs)


def _realtime(items: list[Any], *, caching: bool, **billing: Any) -> EvalRun:
    client = LLMClient(_settings(caching=caching), client=_Wire(_Billing(**billing)))  # type: ignore[arg-type]
    return run_realtime(items, client, _Corpus())


def _batched(items: list[Any], *, caching: bool, **billing: Any) -> EvalRun:
    bill = _Billing(**billing)
    batches = _FakeBatches(lambda cid, params: _succeeded(cid, bill.answer(params)))
    batcher, _ = _batcher(batches)
    settings = _settings(caching=caching, prompt_cache_ttl="1h" if caching else "5m")
    return run_batch(items, DeferredClient(settings), batcher, _Corpus())


_SUBSET = list(CASES.select(cases={"case_01", "case_02", "case_07", "case_19"}))
_DIAGNOSES = [item for item in _SUBSET if item.agent == "incident"]


def _records(**overrides: Any) -> list[dict[str, Any]]:
    """The three Day 20 runs over the subset, as their records."""
    return [
        to_record(_realtime(_SUBSET, caching=False), CASES, run_id="run-uncached"),
        to_record(_realtime(_SUBSET, caching=True, **overrides), CASES, run_id="run-cached"),
        to_record(_batched(_SUBSET, caching=True), CASES, run_id="run-batch"),
    ]


# ======================================================================= the cache verdict


def test_a_realtime_run_whose_later_requests_read_the_prefix_is_healthy():
    run = _realtime(_SUBSET, caching=True)
    health = cache_health(run)
    assert health.healthy is True
    # Two agents, so two prefixes written; the other five requests read theirs.
    assert (health.writes, health.reads) == (2 * PREFIX, 5 * PREFIX)
    assert "read the prefix the first one wrote" in health.note


def test_a_cache_that_is_written_and_never_read_is_not_working():
    # The failure that fails nothing: every request succeeds and costs 25% more.
    health = cache_health(_realtime(_SUBSET, caching=True, reads=False))
    assert health.healthy is False
    assert (health.writes, health.reads) == (7 * PREFIX, 0)
    assert "no docs or incident request after the first read from the cache" in health.note


def test_one_agent_reading_does_not_excuse_the_other():
    run = _realtime(_SUBSET, caching=True)
    silent = [
        result
        if result.score.agent != "docs"
        else _with_usage(result, cache_read_tokens=0, cache_write_tokens=PREFIX)
        for result in run.results
    ]
    run.results = silent
    health = cache_health(run)
    assert health.healthy is False and "no docs request" in health.note


def _with_usage(result: Any, **changes: Any) -> Any:
    from dataclasses import replace

    usage = replace(result.score.usage, **changes)
    return replace(result, score=replace(result.score, usage=usage))


def test_no_cache_counters_at_all_is_not_working_and_says_so():
    health = cache_health(_realtime(_SUBSET, caching=True, counters=False))
    assert health.healthy is False
    assert health.note == "the API reported no cache counters on any response"


def test_a_run_with_caching_off_is_not_judged():
    health = cache_health(_realtime(_SUBSET, caching=False))
    assert health.healthy is None and health.note == "prompt caching was off for this run"


def test_a_run_with_nothing_to_share_is_not_judged():
    one_each = [ITEMS["case_01:diagnose"], ITEMS["case_01:recall"]]
    health = cache_health(_realtime(one_each, caching=True))
    assert health.healthy is None
    assert "no two answered items shared a prefix" in health.note


def test_a_batch_is_reported_and_not_judged():
    # Its requests run concurrently: all of them may be written before any can be read.
    health = cache_health(_batched(_SUBSET, caching=True, reads=False))
    assert health.healthy is None
    assert "reads are best-effort" in health.note
    assert "0 tokens were read and 28,000 written" in health.note


def test_an_item_that_got_no_response_is_not_counted_as_a_missed_read():
    run = _realtime(_DIAGNOSES, caching=True)
    from dataclasses import replace

    from aioc.evals import score_failure

    failed = replace(run.results[1], score=score_failure(run.results[1].item, RuntimeError("x")))
    run.results = [run.results[0], failed, run.results[2]]
    assert cache_health(run).healthy is True  # the third read; the second never ran


def test_the_report_and_the_record_carry_the_verdict():
    good = _realtime(_SUBSET, caching=True)
    assert "Cache: healthy - every agent's later requests read" in render_markdown(good, CASES)
    bad = _realtime(_SUBSET, caching=True, reads=False)
    assert "Cache: NOT WORKING - no docs or incident request" in render_markdown(bad, CASES)
    off = _realtime(_SUBSET, caching=False)
    assert "Cache: not assessed - prompt caching was off" in render_markdown(off, CASES)
    record = to_record(bad, CASES, run_id="a-run")
    assert record["cache"]["healthy"] is False and record["cache"]["writes"] == 7 * PREFIX
    assert record["run"]["run_id"] == "a-run"
    assert to_record(bad, CASES)["run"]["run_id"] is None


# ============================================================================ the baseline


def test_the_runs_are_put_side_by_side_with_the_reference_first():
    records = _records()
    baseline = build_baseline(list(reversed(records)))  # order given is not order shown
    assert [run["label"] for run in baseline["runs"]] == [
        "realtime, uncached",
        "realtime, cached",
        "batch, cached",
    ]
    assert baseline["reference"] == REFERENCE
    assert baseline["model"] == "claude-sonnet-5"
    assert baseline["set"]["sha256"] == CASES.sha256
    assert [run["run_id"] for run in baseline["runs"]] == [
        "run-uncached",
        "run-cached",
        "run-batch",
    ]
    assert [run_label(record) for record in records] == [
        "realtime, uncached",
        "realtime, cached",
        "batch, cached",
    ]


def test_every_run_carries_its_scores_and_its_tokens():
    uncached, cached, batch = build_baseline(_records())["runs"]
    for run in (uncached, cached, batch):
        assert run["items"] == 7
        assert run["rates"]["failure_mode"] == {"hits": 2, "total": 3, "rate": 2 / 3}
        assert run["rates"]["recall_cited"]["hits"] == 2
        assert run["rates"]["tool_success"] == {"hits": 4, "total": 4, "rate": 1.0}
        assert run["tokens"]["in"] == 7 * (PREFIX + REST)
        assert run["tokens"]["out"] == 7 * 900
    assert (uncached["tokens"]["cache_read"], uncached["tokens"]["cache_write"]) == (0, 0)
    assert (cached["tokens"]["cache_read"], cached["tokens"]["cache_write"]) == (
        5 * PREFIX,
        2 * PREFIX,
    )
    assert (batch["batches"], uncached["batches"]) == (1, 0)
    assert (batch["cache_ttl"], cached["cache_ttl"]) == ("1h", "5m")


def test_the_same_tokens_delta_is_the_lever_and_nothing_else():
    uncached, cached, batch = build_baseline(_records())["runs"]
    flat = (7 * (PREFIX + REST) * 2.00 + 7 * 900 * 10.00) / 1e6
    assert uncached["usd"] == pytest.approx(flat, abs=1e-4)
    assert uncached["same_tokens"]["change"] == pytest.approx(0.0)

    paid = (
        7 * REST * 2.00  # the part after the prefix, at the input rate
        + 2 * PREFIX * 2.00 * 1.25  # two prefixes written
        + 5 * PREFIX * 0.20  # five read
        + 7 * 900 * 10.00
    ) / 1e6
    assert cached["usd"] == pytest.approx(paid, abs=1e-4)
    assert cached["same_tokens"]["realtime_uncached_usd"] == pytest.approx(flat, abs=1e-4)
    assert cached["same_tokens"]["change"] == pytest.approx((paid - flat) / flat, abs=1e-3)
    assert cached["same_tokens"]["change"] < 0

    assert batch["same_tokens"]["change"] < cached["same_tokens"]["change"]  # half, and cached


def test_the_measured_delta_is_against_the_reference_run():
    uncached, cached, _batch = build_baseline(_records())["runs"]
    assert cached["measured"] == {
        "against": REFERENCE,
        "reference_usd": uncached["usd"],
        "change": pytest.approx((cached["usd"] - uncached["usd"]) / uncached["usd"]),
    }
    assert uncached["measured"]["change"] == pytest.approx(0.0)


def test_a_baseline_with_no_reference_run_says_so_and_measures_nothing_against_it():
    _uncached, cached, batch = _records()
    baseline = build_baseline([cached, batch])
    assert baseline["reference"] is None
    assert all(run["measured"] is None for run in baseline["runs"])
    assert all(run["same_tokens"]["change"] is not None for run in baseline["runs"])
    report = render_baseline(baseline)
    assert "There is no `realtime, uncached` run" in report
    assert "| Paid, against the reference run | n/a | n/a |" in report


def test_runs_that_answer_the_same_agree_on_every_item():
    agreement = build_baseline(_records())["agreement"]
    assert agreement == {"items": 7, "same": 7, "differing": {}}


def test_an_item_the_runs_scored_differently_is_named():
    baseline = build_baseline(_records(wrong={"case_01:diagnose"}))
    assert baseline["agreement"]["same"] == 6
    assert baseline["agreement"]["differing"] == {
        "case_01:diagnose": {
            "realtime, uncached": True,
            "realtime, cached": False,
            "batch, cached": True,
        }
    }
    assert baseline["items"]["case_01:diagnose"]["realtime, cached"] == {
        "correct": False,
        "answered": True,
        "input": PREFIX + REST,
        "output": 900,
    }


def _changed(record: dict[str, Any], **changes: Any) -> dict[str, Any]:
    copied = copy.deepcopy(record)
    for path, value in changes.items():
        node = copied
        *parents, leaf = path.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return copied


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"set__sha256": "0" * 64}, "different eval sets"),
        ({"run__model": "claude-haiku-4-5"}, "different models"),
        ({"run__mode": "realtime", "run__prompt_caching": False}, "same configuration"),
    ],
    ids=["another-set", "another-model", "a-repeated-configuration"],
)
def test_runs_that_are_not_comparable_are_refused(change: dict[str, Any], complaint: str):
    uncached, cached, _batch = _records()
    with pytest.raises(BaselineError, match=complaint):
        build_baseline([uncached, _changed(cached, **change)])


def test_runs_over_different_items_are_refused():
    uncached, cached, _batch = _records()
    shorter = copy.deepcopy(cached)
    shorter["items"] = shorter["items"][:-1]
    with pytest.raises(BaselineError, match="different items.*has 7 and .* has 6"):
        build_baseline([uncached, shorter])


def test_a_baseline_of_nothing_is_refused():
    with pytest.raises(BaselineError, match="at least one run"):
        build_baseline([])


def test_the_baseline_is_json_and_reads_as_a_table():
    baseline = json.loads(json.dumps(build_baseline(_records(wrong={"case_01:diagnose"}))))
    report = render_baseline(baseline, heading="Eval baseline (test)")
    assert report.startswith("# Eval baseline (test)\n")
    assert "| | realtime, uncached | realtime, cached | batch, cached |" in report
    assert "| Failure mode correct | 2/3 = 67% | 1/3 = 33% | 2/3 = 67% |" in report
    assert "| of which read from the cache | 0 | 20,000 | 20,000 |" in report
    assert "| What the levers changed, same tokens | +0% |" in report
    assert "  - realtime, cached: `run-cached`" in report
    assert "6 of 7 items were scored the same in every run." in report
    assert "| `case_01:diagnose` | yes | no | yes |" in report
    assert "| `single_source` |" in report
    assert "Cache, realtime, cached: every agent's later requests read" in report
    assert chr(0x2014) not in report


# =================================================================== a later run, compared


def _later(**billing: Any) -> dict[str, Any]:
    """The cached realtime run again, after the context has been trimmed by a quarter."""
    record = to_record(_realtime(_SUBSET, caching=True, **billing), CASES, run_id="run-later")
    for item in record["items"]:
        item["usage"]["in"] = int(item["usage"]["in"] * 0.75)
    record["summary"]["usage"]["in"] = int(record["summary"]["usage"]["in"] * 0.75)
    record["cost"]["as_run_usd"] = round(record["cost"]["as_run_usd"] * 0.9, 4)
    return record


def test_a_later_run_is_read_against_the_run_of_its_own_configuration():
    baseline = build_baseline(_records())
    comparison = compare(baseline, _later())
    assert comparison["configuration"] == "realtime, cached"
    assert (comparison["baseline_run"], comparison["run"]) == ("run-cached", "run-later")
    assert comparison["tokens"]["in"] == {
        "before": 7 * (PREFIX + REST),
        "after": int(7 * (PREFIX + REST) * 0.75),
        "change": pytest.approx(-0.25),
    }
    assert comparison["tokens"]["out"]["change"] == pytest.approx(0.0)
    assert comparison["usd"]["change"] == pytest.approx(-0.1, abs=1e-3)
    assert comparison["rates"]["failure_mode"]["change"] == pytest.approx(0.0)
    assert comparison["flipped"] == {}


def test_an_item_that_changed_its_answer_is_named():
    baseline = build_baseline(_records())
    comparison = compare(baseline, _later(wrong={"case_02:diagnose"}))
    assert comparison["flipped"] == {"case_02:diagnose": {"before": True, "after": False}}
    assert comparison["rates"]["failure_mode"]["change"] == pytest.approx(-1 / 3)
    report = render_comparison(comparison)
    assert "| Input tokens | 29,400 | 22,050 | -25% |" in report
    assert "| Failure mode correct | 2/3 = 67% | 1/3 = 33% | -33 pts |" in report
    assert "| `case_02:diagnose` | yes | no |" in report


def test_an_unchanged_run_says_nothing_changed():
    baseline = build_baseline(_records())
    same = to_record(_realtime(_SUBSET, caching=True), CASES, run_id="again")
    report = render_comparison(compare(baseline, same))
    assert "None: every item was scored as it was in the baseline." in report
    assert "| Input tokens | 29,400 | 29,400 | +0% |" in report


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"set__sha256": "0" * 64}, "a changed set needs a new baseline"),
        ({"run__model": "claude-haiku-4-5"}, "used claude-haiku-4-5 and the baseline"),
        ({"run__mode": "batch", "run__prompt_caching": False}, "no `batch, uncached` run"),
    ],
    ids=["another-set", "another-model", "a-configuration-the-baseline-lacks"],
)
def test_a_run_that_cannot_be_compared_is_refused(change: dict[str, Any], complaint: str):
    baseline = build_baseline(_records())
    with pytest.raises(BaselineError, match=complaint):
        compare(baseline, _changed(_later(), **change))


# ============================================================================= the scripts


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Both scripts with nothing live behind them. ``state`` lets a test change how the
    scripted API bills, or make it refuse the credential."""
    state: dict[str, Any] = {"billing": {}, "calls": [], "refuse": False}
    results = tmp_path / "test-results"
    out = tmp_path / "evaluations"

    def client(settings: LLMSettings) -> LLMClient:
        wire = _Wire(_Billing(**state["billing"]))
        if state["refuse"]:

            def refuse(**_kwargs: Any) -> Any:
                raise _refused()

            wire.messages = SimpleNamespace(create=refuse)
        state["calls"].append(wire.calls)
        return LLMClient(settings, client=wire)  # type: ignore[arg-type]

    def batcher(_cls: Any, *_args: Any, **_kwargs: Any) -> Any:
        bill = _Billing(**state["billing"])
        made, _ = _batcher(_FakeBatches(lambda cid, p: _succeeded(cid, bill.answer(p))))
        return made

    for module in (run_evals, check_day20_baseline):
        monkeypatch.setattr(module, "LLMSettings", lambda: _BASE)
        monkeypatch.setattr(
            module, "RunRecorder", functools.partial(RunRecorder, results_root=results)
        )
    monkeypatch.setattr(run_evals, "LLMClient", client)
    monkeypatch.setattr(run_evals, "CorpusSearcher", lambda _embedder: _Corpus())
    monkeypatch.setattr(run_evals, "default_embedder", lambda: None)
    monkeypatch.setattr(run_evals.MessageBatcher, "from_settings", classmethod(batcher))
    state.update(results=results, out=out)
    return state


def _calls(state: dict[str, Any]) -> int:
    return sum(len(calls) for calls in state["calls"])


def _eval_runs(results: Path) -> list[Path]:
    return sorted(path.parent for path in results.glob("runs/*/*/eval.json"))


def test_the_plan_is_free_and_adds_up(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]):
    assert check_day20_baseline.main(["--plan", "--out", str(wired["out"])]) == 0
    out = capsys.readouterr().out
    assert "model claude-sonnet-5, 38 item(s) a run" in out
    assert "smoke test             4 calls" in out
    assert "realtime, uncached    38 calls" in out
    assert "batch, cached         38 calls" in out
    assert "total                118 calls" in out
    assert "rehearsal" not in out
    assert _calls(wired) == 0
    assert not wired["results"].exists() and not wired["out"].exists()


def test_a_batch_is_planned_at_half_the_realtime_price(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    args = ["--plan", "--skip-smoke", "--configs", "realtime-uncached,batch-uncached"]
    assert check_day20_baseline.main(args) == 0
    lines = capsys.readouterr().out.splitlines()
    price = {line.split()[0]: float(line.split("~$")[1]) for line in lines if "~$" in line}
    assert price["batch,"] == pytest.approx(price["realtime,"] / 2, abs=0.01)
    assert price["total"] == pytest.approx(price["realtime,"] + price["batch,"], abs=0.01)
    assert not any("smoke" in line for line in lines)


def test_an_unknown_configuration_is_an_error():
    with pytest.raises(SystemExit, match="unknown configuration"):
        check_day20_baseline.main(["--plan", "--configs", "realtime-cached,streaming"])


def test_without_a_key_the_plan_is_shown_and_nothing_is_attempted(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    keyless = _BASE.model_copy(update={"anthropic_api_key": None})
    monkeypatch.setattr(check_day20_baseline, "LLMSettings", lambda: keyless)
    assert check_day20_baseline.main(["--out", str(wired["out"])]) == 2
    captured = capsys.readouterr()
    assert "total                118 calls" in captured.out
    assert "ANTHROPIC_API_KEY is not set" in captured.err
    assert _calls(wired) == 0


def test_the_whole_checkpoint_writes_the_baseline_it_measured(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert check_day20_baseline.main(["--out", str(wired["out"])]) == 0
    out = capsys.readouterr().out
    assert "SMOKE PASS: every agent's later requests read the prefix" in out
    assert "--- PASS: the runs are comparable" in out
    # 4 smoke calls and two realtime runs of 38; the batch run went through the batcher.
    assert _calls(wired) == 4 + 38 + 38

    written = sorted(
        path.relative_to(wired["out"]).as_posix() for path in wired["out"].rglob("*.*")
    )
    assert written == [
        "baseline.json",
        "baseline.md",
        "results/batch-cached.md",
        "results/realtime-cached.md",
        "results/realtime-uncached.md",
    ]
    baseline = json.loads((wired["out"] / "baseline.json").read_text(encoding="utf-8"))
    assert [run["label"] for run in baseline["runs"]] == [
        "realtime, uncached",
        "realtime, cached",
        "batch, cached",
    ]
    assert baseline["agreement"]["items"] == 38
    assert all(run["rates"]["answered"]["hits"] == 38 for run in baseline["runs"])
    assert all(run["run_id"] for run in baseline["runs"])
    report = (wired["out"] / "baseline.md").read_text(encoding="utf-8")
    assert report.startswith("# Eval baseline (Day 20)\n")
    assert "Mode: batch, prompt caching on (1h TTL)." in (
        wired["out"] / "results" / "batch-cached.md"
    ).read_text(encoding="utf-8")

    # Four eval runs recorded themselves; the checkpoint's own record carries no usage.
    assert len(_eval_runs(wired["results"])) == 4
    (checkpoint,) = wired["results"].glob("runs/*/*__checkpoint__day20-baseline")
    summary = json.loads((checkpoint / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "passed"
    assert "usage" not in (checkpoint / "events.jsonl").read_text(encoding="utf-8")
    assert (checkpoint / "baseline.json").is_file()


def test_the_cost_review_counts_each_token_once(wired: dict[str, Any]):
    from cost_review import review

    assert check_day20_baseline.main(["--out", str(wired["out"]), "--limit", "6"]) == 0
    total = review(wired["results"], since=None)["total"]
    # The smoke test and three runs of six: the checkpoint's record is not an `llm` run.
    assert total.runs == 4
    assert total.output_tokens == (4 + 6 * 3) * 900


def test_a_rehearsal_writes_nothing_under_evaluations(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert check_day20_baseline.main(["--out", str(wired["out"]), "--limit", "6"]) == 0
    out = capsys.readouterr().out
    assert "a rehearsal: 6 of 38 items, so nothing is written under" in out
    assert "rehearsal - nothing written under" in out
    assert "--- PASS" in out
    assert not wired["out"].exists()
    assert len(_eval_runs(wired["results"])) == 4


def test_a_failed_smoke_test_stops_before_the_full_runs(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    wired["billing"] = {"reads": False}
    assert check_day20_baseline.main(["--out", str(wired["out"])]) == 1
    err = capsys.readouterr().err
    assert "SMOKE FAIL: prompt caching is on and not working" in err
    assert "Stopped after 4 calls; the full runs were not started." in err
    assert _calls(wired) == 4
    assert not wired["out"].exists()
    (checkpoint,) = wired["results"].glob("runs/*/*__checkpoint__day20-baseline")
    summary = json.loads((checkpoint / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "failed"


def test_a_refused_credential_stops_the_checkpoint_at_the_first_call(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    wired["refuse"] = True
    assert check_day20_baseline.main(["--out", str(wired["out"])]) == 2
    assert "ABORTED: the API refused the credential at case_01:diagnose" in capsys.readouterr().err
    assert not wired["out"].exists()


def test_skipping_the_smoke_test_goes_straight_to_the_runs(wired: dict[str, Any]):
    args = ["--out", str(wired["out"]), "--limit", "6", "--skip-smoke"]
    assert check_day20_baseline.main([*args, "--configs", "realtime-cached"]) == 0
    assert _calls(wired) == 6
    assert len(_eval_runs(wired["results"])) == 1


def test_the_checkpoint_fails_on_a_cache_the_smoke_test_could_not_have_seen(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    # Skipped smoke test, broken cache: the full run's own verdict is what catches it.
    wired["billing"] = {"reads": False}
    args = ["--out", str(wired["out"]), "--limit", "6", "--skip-smoke"]
    assert check_day20_baseline.main([*args, "--configs", "realtime-uncached,realtime-cached"]) == 1
    out = capsys.readouterr().out
    assert "--- FAIL" in out
    assert "realtime, cached: cost +" in out and "the lever saved nothing" in out
    assert "realtime, cached: no docs or incident request after the first read" in out


def _entry(label: str, *, answered: tuple[int, int] = (10, 10), change: float | None = -0.2) -> Any:
    hits, total = answered
    return {
        "label": label,
        "rates": {"answered": {"hits": hits, "total": total, "rate": hits / total}},
        "same_tokens": {"change": change},
    }


def _of(*runs: Any, differing: int = 0, items: int = 10) -> dict[str, Any]:
    return {
        "runs": list(runs),
        "agreement": {"items": items, "differing": {f"k{i}": {} for i in range(differing)}},
    }


def test_what_the_checkpoint_holds_a_baseline_to():
    evaluate = check_day20_baseline.evaluate
    assert evaluate(_of(_entry(REFERENCE, change=0.0), _entry("batch, cached")), {}) == []
    # The reference run is not a lever, so saving nothing is what it should do.
    assert evaluate(_of(_entry(REFERENCE, change=0.0)), {}) == []
    assert evaluate(_of(_entry("realtime, cached", answered=(0, 10))), {}) == [
        "realtime, cached: no item was answered"
    ]
    assert evaluate(_of(_entry("realtime, cached", answered=(8, 10))), {}) == [
        "realtime, cached: only 8/10 items were answered"
    ]
    # One agent that gave up is a result, not a broken run.
    assert evaluate(_of(_entry("realtime, cached", answered=(9, 10))), {}) == []
    (complaint,) = evaluate(_of(_entry("realtime, cached", change=0.03)), {})
    assert "cost +3% against its own tokens" in complaint
    assert evaluate(_of(_entry("batch, cached"), differing=2), {}) == []
    (noise,) = evaluate(_of(_entry("batch, cached"), differing=3), {})
    assert "scored 3 of 10 items differently" in noise


# ------------------------------------------------------------------------ eval_baseline


def _recorded(wired: dict[str, Any], *args: str) -> Path:
    before = set(_eval_runs(wired["results"]))
    assert run_evals.main(list(args)) == 0
    (made,) = set(_eval_runs(wired["results"])) - before
    return made


def test_named_runs_are_put_side_by_side(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]):
    four = ["--cases", "case_01,case_02,case_07,case_19"]
    uncached = _recorded(wired, *four, "--no-cache")
    cached = _recorded(wired, *four)
    capsys.readouterr()

    assert eval_baseline.main([str(cached), str(uncached)]) == 0
    out = capsys.readouterr().out
    assert "| | realtime, uncached | realtime, cached |" in out
    assert f"  - realtime, cached: `{cached.name}`" in out
    assert "7 of 7 items were scored the same in every run." in out


def test_the_baseline_is_written_as_markdown_and_as_json(
    wired: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    run = _recorded(wired, "--cases", "case_01,case_02")
    target = tmp_path / "out" / "baseline.md"
    assert eval_baseline.main([str(run), "--write", str(target), "--heading", "A baseline"]) == 0
    assert target.read_text(encoding="utf-8").startswith("# A baseline\n")
    data = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    assert [entry["label"] for entry in data["runs"]] == ["realtime, cached"]
    assert "written:" in capsys.readouterr().out


def test_latest_takes_the_newest_complete_run_of_each_configuration(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    results = ["--results", str(wired["results"])]
    assert eval_baseline.main(["--latest", *results]) == 1
    assert "no complete eval run on the current set" in capsys.readouterr().err

    partial = _recorded(wired, "--limit", "6")  # not the whole set: never a candidate
    older = _recorded(wired)
    uncached = _recorded(wired, "--no-cache")
    newer = _recorded(wired)
    capsys.readouterr()

    assert eval_baseline.main(["--latest", *results]) == 0
    out = capsys.readouterr().out
    assert f"  - realtime, cached: `{newer.name}`" in out
    assert f"  - realtime, uncached: `{uncached.name}`" in out
    # Names in the same second differ only by a suffix, so match the whole name.
    assert f"`{older.name}`" not in out and f"`{partial.name}`" not in out
    assert "38 items" in out


def test_latest_ignores_a_run_made_before_the_set_changed(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    run = _recorded(wired)
    record = json.loads((run / "eval.json").read_text(encoding="utf-8"))
    record["set"]["sha256"] = "0" * 64
    (run / "eval.json").write_text(json.dumps(record), encoding="utf-8")
    assert eval_baseline.main(["--latest", "--results", str(wired["results"])]) == 1


def test_a_later_run_is_compared_from_the_command_line(
    wired: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    four = ["--cases", "case_01,case_02,case_07,case_19"]
    first = _recorded(wired, *four)
    target = tmp_path / "baseline.md"
    assert eval_baseline.main([str(first), "--write", str(target)]) == 0

    wired["billing"] = {"wrong": {"case_02:diagnose"}}
    later = _recorded(wired, *four)
    capsys.readouterr()
    against = ["--against", str(target.with_suffix(".json"))]
    assert eval_baseline.main([*against, str(later)]) == 0
    out = capsys.readouterr().out
    assert f"- Baseline run: `{first.name}`." in out
    assert "| `case_02:diagnose` | yes | no |" in out

    assert eval_baseline.main([*against, str(_recorded(wired, *four, "--no-cache"))]) == 1
    assert "the baseline has no `realtime, uncached` run" in capsys.readouterr().err


def test_runs_that_cannot_be_compared_exit_with_the_reason(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    one = _recorded(wired, "--cases", "case_01")
    two = _recorded(wired, "--cases", "case_01,case_02", "--no-cache")
    capsys.readouterr()
    assert eval_baseline.main([str(one), str(two)]) == 1
    assert "cannot compare these runs: runs over different items" in capsys.readouterr().err


def test_the_command_line_says_what_it_needs(tmp_path: Path):
    with pytest.raises(SystemExit, match="name at least one run directory"):
        eval_baseline.main([])
    with pytest.raises(SystemExit, match="does not exist"):
        eval_baseline.main([str(tmp_path / "missing")])
    with pytest.raises(SystemExit, match="exactly one run directory"):
        eval_baseline.main(["--against", str(tmp_path / "b.json")])
    with pytest.raises(SystemExit, match="do not name any as well"):
        eval_baseline.main(["--latest", str(tmp_path)])


# --------------------------------------------------------------------- run_evals --smoke


def test_smoke_runs_the_first_four_diagnoses_and_passes(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(["--smoke"]) == 0
    out = capsys.readouterr().out
    assert "4 item(s) from seeded-incidents v1" in out
    assert "SMOKE PASS: 4 item(s) answered; every agent's later requests read" in out
    assert _calls(wired) == 4


def test_smoke_fails_on_a_cache_that_did_not_read(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    wired["billing"] = {"reads": False}
    assert run_evals.main(["--smoke"]) == 1
    err = capsys.readouterr().err
    assert "SMOKE FAIL: prompt caching is on and not working: no incident request" in err


def test_smoke_with_caching_off_checks_the_wire_and_not_the_cache(
    wired: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(["--smoke", "--no-cache"]) == 0
    assert "prompt caching was off for this run" in capsys.readouterr().out


def test_smoke_respects_a_selection(wired: dict[str, Any], capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(["--smoke", "--cases", "case_01,case_02"]) == 0
    assert "4 item(s)" in capsys.readouterr().out  # two cases, both tasks
    assert _calls(wired) == 4
