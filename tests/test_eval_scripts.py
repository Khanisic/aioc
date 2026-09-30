"""Day 19 dev tooling, offline: `scripts/run_evals.py` and the cost review's two levers.

The live path of `run_evals.py` is run here end to end with the model, the corpus, and the
recorder's directory swapped for scripted ones - so what is checked is the script itself:
what it records, that a recorded run can be scored again from disk, and that the cost
review prices that record the way the report did.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import pytest
import run_evals
from cost_review import review
from pydantic import SecretStr
from runlog import RunRecorder

from aioc.llm import LLMClient, LLMSettings, Usage, price
from tests.test_batch import _batcher
from tests.test_contract import _worked_example
from tests.test_day15_scripts import _record
from tests.test_evals import CASES, _batch_api, _Corpus, _Model, _Revoked

_KEYED = LLMSettings(
    anthropic_api_key=SecretStr("sk-ant-test-not-a-key"),
    model="claude-sonnet-5",
    max_tokens=2048,
    prompt_caching=True,
    max_validation_retries=2,
)


@pytest.fixture
def scripted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`run_evals` with nothing live behind it: a scripted model, a scripted corpus, and
    records written under ``tmp_path``."""
    monkeypatch.setattr(run_evals, "LLMSettings", lambda: _KEYED)
    monkeypatch.setattr(
        run_evals, "LLMClient", lambda settings: LLMClient(settings, client=_Model())
    )
    monkeypatch.setattr(run_evals, "CorpusSearcher", lambda _embedder: _Corpus())
    monkeypatch.setattr(run_evals, "default_embedder", lambda: None)
    monkeypatch.setattr(
        run_evals, "RunRecorder", functools.partial(RunRecorder, results_root=tmp_path)
    )
    batcher, _ = _batcher(_batch_api())
    monkeypatch.setattr(
        run_evals.MessageBatcher, "from_settings", classmethod(lambda cls, *_a, **_k: batcher)
    )
    return tmp_path


def _run_dir(results: Path) -> Path:
    (path,) = list(results.glob("runs/*/*"))
    return path


_FOUR = ["--cases", "case_01,case_02,case_07,case_19"]


# ------------------------------------------------------------------------ the free paths


def test_list_names_every_item_and_its_truth(capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "seeded-incidents v1" in out
    assert "items  38 (18 diagnose, 20 recall)" in out
    assert "bad_config_deploy x4" in out and "other x2" in out
    assert "case_07:diagnose     incident  code_regression, sev3" in out
    assert "case_07:recall       docs      cites doc_0012" in out
    assert "case_19:recall       docs      no precedent in the corpus" in out


def test_list_respects_the_selection(capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(["--list", "--tasks", "diagnose", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "items  3 (3 diagnose)" in out and "case_04" not in out


def test_show_prints_exactly_what_the_agent_is_handed(capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(["--show", "case_04"]) == 0
    out = capsys.readouterr().out
    assert "=== case_04:diagnose -> the incident agent" in out
    assert "Disk usage crossed 90% on the container volume (evt_0010_1)" in out
    assert "=== case_04:recall -> the docs agent" in out
    # What the case withholds stays withheld on the way to the terminal too.
    assert "debug log level" not in out.lower()


def test_an_unknown_case_is_an_error_not_an_empty_run():
    with pytest.raises(SystemExit, match="no such case"):
        run_evals.main(["--list", "--cases", "case_01,case_99"])
    with pytest.raises(SystemExit, match="no such case: case_99"):
        run_evals.main(["--show", "case_99"])


def test_the_free_paths_cannot_be_combined():
    with pytest.raises(SystemExit):
        run_evals.main(["--list", "--dry-run"])


def test_a_dry_run_sizes_every_request_and_sends_none(
    scripted: Path, capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(["--dry-run", *_FOUR]) == 0
    out = capsys.readouterr().out
    assert "7 request(s), model claude-sonnet-5, mode realtime" in out
    assert out.count("input tokens") == 8  # seven requests and the total
    assert "projected, realtime and uncached: $" in out
    assert "projected, batch and uncached:" in out
    assert list(scripted.glob("runs/*/*")) == []  # nothing ran, so nothing is recorded


def test_tool_success_is_read_off_the_recorded_responses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    run = tmp_path / "runs" / "2026-09-18" / "a-run"
    run.mkdir(parents=True)
    (run / "response.json").write_text(json.dumps(_worked_example()), encoding="utf-8")
    other = tmp_path / "runs" / "2026-09-18" / "not-a-response"
    other.mkdir()
    (other / "response.json").write_text('{"something": "else"}', encoding="utf-8")

    assert run_evals.main(["--recorded-tools", "--results", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "recorded agent report(s) in 2 run(s)" in out
    assert "all" in out and "%" in out


def test_no_recorded_responses_is_said_plainly(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(["--recorded-tools", "--results", str(tmp_path)]) == 0
    assert "no recorded responses" in capsys.readouterr().out


# -------------------------------------------------------------------------- the live path


def test_without_a_key_nothing_is_attempted(
    scripted: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        run_evals, "LLMSettings", lambda: _KEYED.model_copy(update={"anthropic_api_key": None})
    )
    assert run_evals.main(_FOUR) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err
    assert list(scripted.glob("runs/*/*")) == []


def test_a_revoked_key_aborts_the_run_and_says_what_to_do(
    scripted: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        run_evals, "LLMClient", lambda settings: LLMClient(settings, client=_Revoked(good=0))
    )
    assert run_evals.main(_FOUR) == 2
    err = capsys.readouterr().err
    assert "ABORTED: stopped at case_01:diagnose: the API refused the credential" in err
    assert "HANDOFF.md sec 7 item 10" in err
    # The attempt is on record as an error, with no item blamed for it.
    summary = json.loads((_run_dir(scripted) / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "error"
    assert summary["totals"] == {"events": 1, "error": 1}


def test_a_run_records_its_report_its_scores_and_its_responses(
    scripted: Path, capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(_FOUR) == 0
    out = capsys.readouterr().out
    assert "7 item(s) from seeded-incidents v1" in out
    assert "mode realtime, prompt caching on, 5m" in out
    assert "| Failure mode matches the recorded truth | 2/3 = 67% |" in out

    run = _run_dir(scripted)
    assert run.name.endswith("__llm__evals-realtime")
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "passed"  # the run completed; a wrong answer is a score
    assert summary["metadata"]["set_sha256"] == CASES.sha256
    assert summary["metadata"]["items"] == 7
    assert summary["totals"] == {"events": 7, "passed": 5, "failed": 2}

    record = json.loads((run / "eval.json").read_text(encoding="utf-8"))
    assert record["summary"]["accuracy"]["failure_mode"]["hits"] == 2
    kept = json.loads((run / "responses.json").read_text(encoding="utf-8"))
    assert set(kept) == {item["key"] for item in record["items"]}
    assert kept["case_01:diagnose"]["response"]["agent"] == "incident"
    assert kept["case_01:diagnose"]["retrieved"] is None
    assert kept["case_02:recall"]["retrieved"] == ["doc_0001", "doc_0003"]
    assert (run / "report.md").read_text(encoding="utf-8").startswith("# Eval run: ")


def test_the_report_can_be_written_where_it_can_be_committed(scripted: Path, tmp_path: Path):
    target = tmp_path / "evaluations" / "results" / "first.md"
    assert run_evals.main([*_FOUR, "--write", str(target)]) == 0
    assert "## Accuracy" in target.read_text(encoding="utf-8")


def test_the_flags_change_the_settings_and_nothing_else(
    scripted: Path, capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main([*_FOUR, "--no-cache", "--model", "claude-haiku-4-5"]) == 0
    out = capsys.readouterr().out
    assert "model claude-haiku-4-5, mode realtime, prompt caching off" in out
    record = json.loads((_run_dir(scripted) / "eval.json").read_text(encoding="utf-8"))
    assert record["run"]["prompt_caching"] is False
    assert record["run"]["model"] == "claude-haiku-4-5"


def test_a_batch_run_is_recorded_as_one(scripted: Path, capsys: pytest.CaptureFixture[str]):
    assert run_evals.main([*_FOUR, "--mode", "batch", "--cache-ttl", "1h"]) == 0
    out = capsys.readouterr().out
    assert "mode batch, prompt caching on, 1h" in out
    assert "7 request(s) submitted as one batch" in out
    run = _run_dir(scripted)
    assert run.name.endswith("__llm__evals-batch")
    record = json.loads((run / "eval.json").read_text(encoding="utf-8"))
    assert [b["requests"] for b in record["run"]["batches"]] == [7]
    events = [
        json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(event["data"]["batch"] is True for event in events)
    assert all(event["data"]["cache_ttl"] == "1h" for event in events)
    assert all(event["duration_ms"] is None for event in events)


def test_a_recorded_run_scores_the_same_from_disk(
    scripted: Path, capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(_FOUR) == 0
    run = _run_dir(scripted)
    first = (run / "report.md").read_text(encoding="utf-8")
    capsys.readouterr()

    assert run_evals.main(["--rescore", str(run)]) == 0
    again = capsys.readouterr().out
    # Everything under the heading is the same report: same scores, same tokens, same price.
    assert again.split("\n", 1)[1].strip() == first.split("\n", 1)[1].strip()
    assert again.startswith(f"# Eval run, rescored: {run.name}")


def test_rescoring_keeps_a_failed_item_failed(scripted: Path, capsys: pytest.CaptureFixture[str]):
    assert run_evals.main(_FOUR) == 0
    run = _run_dir(scripted)
    record = json.loads((run / "eval.json").read_text(encoding="utf-8"))
    kept = json.loads((run / "responses.json").read_text(encoding="utf-8"))
    record["items"][0].update(answered=False, error="ValidationError: refused", correct=None)
    kept["case_01:diagnose"]["response"] = None
    (run / "eval.json").write_text(json.dumps(record), encoding="utf-8")
    (run / "responses.json").write_text(json.dumps(kept), encoding="utf-8")
    capsys.readouterr()

    assert run_evals.main(["--rescore", str(run)]) == 0
    out = capsys.readouterr().out
    assert "- Answered: 6/7 = 86%." in out
    assert "- `case_01:diagnose` produced no response: ValidationError: refused" in out


# --------------------------------------------------------- the cost review's two levers


def test_the_cost_review_prices_an_eval_run_the_way_its_report_did(scripted: Path):
    assert run_evals.main(_FOUR) == 0
    record = json.loads((_run_dir(scripted) / "eval.json").read_text(encoding="utf-8"))
    total = review(scripted, since=None)["total"]
    assert total.usd == pytest.approx(record["cost"]["as_run_usd"], abs=1e-4)
    assert total.usd_flat == pytest.approx(record["cost"]["realtime_uncached_usd"], abs=1e-4)
    assert (total.input_tokens, total.cache_read_tokens) == (7 * 4_200, 7 * 4_000)
    assert total.saved > 0


def test_a_batched_run_is_priced_at_half(scripted: Path):
    assert run_evals.main([*_FOUR, "--mode", "batch"]) == 0
    record = json.loads((_run_dir(scripted) / "eval.json").read_text(encoding="utf-8"))
    total = review(scripted, since=None)["total"]
    assert total.usd == pytest.approx(record["cost"]["as_run_usd"], abs=1e-4)
    usage = Usage(
        input_tokens=7 * 4_200,
        output_tokens=7 * 900,
        cache_read_tokens=7 * 4_000,
        cache_write_tokens=0,
    )
    assert total.usd == pytest.approx(price("claude-sonnet-5", usage, batch=True))


def test_cache_counters_in_either_recorded_shape_are_priced(tmp_path: Path):
    script = {"usage": {"in": 1_000_000, "out": 0, "cache_read": 900_000, "cache_write": 0}}
    contract = {
        "cost": {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 900_000,
            "cache_write_tokens": 0,
            "usd": None,
        }
    }
    _record(tmp_path, "a-check", "2026-09-29", [{"data": script}])
    _record(tmp_path, "a-respond", "2026-09-29", [{"data": contract}])
    by_check = review(tmp_path, since=None)["by_check"]
    # 100k at the input rate and 900k at the read rate: $0.20 + $0.18, against $2.00 flat.
    for name in ("a-check", "a-respond"):
        assert by_check[name].usd == pytest.approx(0.38)
        assert by_check[name].usd_flat == pytest.approx(2.00)
        assert by_check[name].saved == pytest.approx(1.62)
        assert by_check[name].cache_read_tokens == 900_000


def test_a_cache_write_nobody_read_back_shows_as_a_loss(tmp_path: Path):
    written = {"usage": {"in": 1_000_000, "out": 0, "cache_read": 0, "cache_write": 1_000_000}}
    _record(tmp_path, "one-off", "2026-09-29", [{"data": written}])
    _record(tmp_path, "one-off-1h", "2026-09-29", [{"data": {**written, "cache_ttl": "1h"}}])
    by_check = review(tmp_path, since=None)["by_check"]
    assert by_check["one-off"].usd == pytest.approx(2.50)
    assert by_check["one-off"].saved == pytest.approx(-0.50)
    assert by_check["one-off-1h"].usd == pytest.approx(4.00)


def test_a_record_from_before_day_19_is_priced_as_it_always_was(tmp_path: Path):
    _record(tmp_path, "old", "2026-09-17", [{"data": {"usage": {"in": 1_000_000, "out": 100_000}}}])
    old = review(tmp_path, since=None)["by_check"]["old"]
    assert old.usd == pytest.approx(3.00) and old.saved == pytest.approx(0.0)
    assert old.cache_read_tokens == 0


def test_null_cache_counters_are_not_counted(tmp_path: Path):
    block: dict[str, Any] = {"in": 100, "out": 10, "cache_read": None, "cache_write": None}
    _record(tmp_path, "nulls", "2026-09-29", [{"data": {"usage": block}}])
    tally = review(tmp_path, since=None)["by_check"]["nulls"]
    assert (tally.input_tokens, tally.cache_read_tokens, tally.cache_write_tokens) == (100, 0, 0)


def test_two_runs_in_the_same_second_keep_their_own_records(tmp_path: Path):
    # Found by the Day 20 checkpoint: its smoke test and its first full run are both
    # `evals-realtime`, and against a scripted model they start inside one second. The
    # second run was writing into the first one's directory.
    first = RunRecorder(kind="llm", name="evals-realtime", results_root=tmp_path)
    second = RunRecorder(kind="llm", name="evals-realtime", results_root=tmp_path)
    third = RunRecorder(kind="llm", name="evals-realtime", results_root=tmp_path)
    if first.run_id.split("__")[0] != third.run_id.split("__")[0]:
        pytest.skip("the clock ticked over between the three; nothing to collide")
    assert second.run_id == f"{first.run_id}-2" and third.run_id == f"{first.run_id}-3"
    assert len({first.dir, second.dir, third.dir}) == 3

    first.event("a", outcome="passed")
    second.event("b", outcome="failed")
    first.finish()
    second.finish()
    assert json.loads((first.dir / "run.json").read_text(encoding="utf-8"))["totals"] == {
        "events": 1,
        "passed": 1,
    }
    assert json.loads((second.dir / "run.json").read_text(encoding="utf-8"))["run_id"] == (
        second.run_id
    )
    index = (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in index] == [first.run_id, second.run_id]
