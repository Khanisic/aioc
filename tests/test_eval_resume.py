"""Day 20, after its first live run: stopping for the right reason, and keeping what was paid for.

Two live runs of the eval harness ended badly in ways that cost money. One recorded a
revoked key as five failed items. The next was five items in when the account ran out of
credit, and went on to record the other thirty-three as the agents' failures - and both
threw away answers they had already paid for. The rules these tests hold:

- a failure that is the environment's stops the run; a failure that is the agent's is a
  score. The two are told apart by what raised, never by guessing;
- one failed call is weather and the run goes on; three in a row is the climate;
- every item is on disk the moment it is scored, and a continued run asks only for what
  was not finished - which includes anything the environment failed, and excludes what
  the agent gave up on;
- a token is counted once, however many runs an answer passes through.

Also here: the evidence check learned, from the same run, that an excerpt joined from
lines that are each verbatim is not an invented one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import check_day20_baseline
import httpx
import pytest
import run_evals
from cost_review import review

from aioc.agents import EMIT_TOOL_NAME
from aioc.evals import (
    MAX_ENVIRONMENT_FAILURES,
    PROGRESS,
    EvalAborted,
    ProgressFile,
    RunConfig,
    StoreError,
    failure_kind,
    is_environment_error,
    quoted,
    read_run,
    run_batch,
    run_realtime,
    score_failure,
    score_item,
    stops_the_run,
    summarise,
    to_record,
)
from aioc.llm import BatchError, DeferredClient, LLMClient
from tests.test_baseline import _BASE, _Billing, _settings, _Wire
from tests.test_baseline import wired as wired  # noqa: F401 - the fixture, used by name
from tests.test_batch import _batcher, _FakeBatches, _succeeded
from tests.test_evals import (
    CASES,
    ITEMS,
    _Corpus,
    _diagnosed,
    _diagnosis,
    _message,
    _refused,
    _Scripted,
)

_SUBSET = list(CASES.select(cases={"case_01", "case_02", "case_07", "case_19"}))  # 7 items
_CONFIG = RunConfig(
    set_sha256=CASES.sha256,
    model="claude-sonnet-5",
    mode="realtime",
    prompt_caching=True,
    cache_ttl="5m",
)


# ------------------------------------------------------------------------------- fakes


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _no_credit() -> anthropic.BadRequestError:
    return anthropic.BadRequestError(
        "Error code: 400 - Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits.",
        response=httpx.Response(400, request=_request()),
        body=None,
    )


def _overloaded() -> anthropic.InternalServerError:
    return anthropic.InternalServerError(
        "Error code: 529 - Overloaded",
        response=httpx.Response(529, request=_request()),
        body=None,
    )


class _Flaky(_Wire):
    """Answers like the scripted API, except on the calls it is told to fail."""

    def __init__(self, failures: dict[int, Any]) -> None:
        super().__init__(_Billing())
        self._failures = failures  # 1-based call number -> a function making the error

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        make = self._failures.get(len(self.calls))
        if make is not None:
            raise make()
        return self._billing.answer(kwargs)


def _client(wire: Any) -> LLMClient:
    return LLMClient(_settings(caching=True), client=wire)  # type: ignore[arg-type]


def _from(number: int, make: Any) -> dict[int, Any]:
    """Fail every call from ``number`` on."""
    return {n: make for n in range(number, number + 60)}


# ===================================================== whose failure it was, and what stops


def test_a_failure_is_the_environments_when_the_call_itself_failed():
    for error in (_no_credit(), _overloaded(), _refused(), BatchError("expired")):
        assert is_environment_error(error)
        assert score_failure(ITEMS["case_01:diagnose"], error).error_kind == "environment"
    assert is_environment_error(anthropic.APIConnectionError(request=_request()))


def test_a_failure_is_the_agents_when_it_gave_up_on_its_report():
    item = ITEMS["case_01:diagnose"]
    broken = _diagnosis(item)
    broken["findings"]["severity"]["detail"] = "not allowed here"
    fake = _Scripted([_message(EMIT_TOOL_NAME, broken), _message(EMIT_TOOL_NAME, broken)])
    (score,) = run_realtime([item], _client(fake)).scores
    assert (score.answered, score.error_kind) == (False, "agent")
    assert not is_environment_error(ValueError("x"))


def test_an_answered_item_has_no_failure_to_attribute():
    item = ITEMS["case_01:diagnose"]
    assert score_item(item, _diagnosed(item)).error_kind is None


def test_what_stops_a_run_on_its_own():
    assert "refused the credential" in (stops_the_run(_refused()) or "")
    assert "no credit left" in (stops_the_run(_no_credit()) or "")
    assert "can then be resumed" in (stops_the_run(_no_credit()) or "")
    # One overloaded response says nothing about the next.
    assert stops_the_run(_overloaded()) is None
    assert stops_the_run(ValueError("credit balance")) is None  # not the API's word


def test_an_empty_account_stops_the_run_and_keeps_what_was_answered():
    # The second live run: five items answered, then the balance ran out.
    wire = _Flaky(_from(6, _no_credit))
    with pytest.raises(EvalAborted) as caught:
        run_realtime(_SUBSET, _client(wire), _Corpus())
    stopped = caught.value
    assert stopped.completed == 5
    assert [result.item.key for result in stopped.results] == [i.key for i in _SUBSET[:5]]
    assert all(result.score.answered for result in stopped.results)
    assert "stopped at case_07:recall: the account has no credit left" in str(stopped)
    assert "5 item(s) had been scored and are kept" in str(stopped)
    assert len(wire.calls) == 6  # the refusal, and then nothing


def test_one_failed_call_is_recorded_and_the_run_goes_on():
    wire = _Flaky({3: _overloaded})
    run = run_realtime(_SUBSET, _client(wire), _Corpus())
    assert [score.answered for score in run.scores] == [True, True, False, True, True, True, True]
    failed = run.scores[2]
    assert (failed.key, failed.error_kind) == ("case_02:diagnose", "environment")
    assert "InternalServerError" in (failed.error or "")
    assert len(wire.calls) == 7


def test_failures_in_a_row_stop_the_run_and_are_not_kept():
    assert MAX_ENVIRONMENT_FAILURES == 3
    wire = _Flaky(_from(3, _overloaded))
    with pytest.raises(EvalAborted) as caught:
        run_realtime(_SUBSET, _client(wire), _Corpus())
    stopped = caught.value
    # Two answered; the three that failed are dropped, so a continued run asks again.
    assert [result.item.key for result in stopped.results] == ["case_01:diagnose", "case_01:recall"]
    assert "3 items in a row failed on the API itself" in str(stopped)
    assert len(wire.calls) == 5


def test_two_failures_then_an_answer_is_weather():
    wire = _Flaky({2: _overloaded, 3: _overloaded})
    run = run_realtime(_SUBSET, _client(wire), _Corpus())
    assert [score.answered for score in run.scores] == [True, False, False, True, True, True, True]
    assert summarise(run.scores).answered.render() == "5/7 = 71%"


def test_failures_on_the_last_items_are_recorded_when_the_run_ends():
    wire = _Flaky({6: _overloaded, 7: _overloaded})
    run = run_realtime(_SUBSET, _client(wire), _Corpus())
    assert [score.answered for score in run.scores[-2:]] == [False, False]
    assert len(run.scores) == 7


def test_a_batch_whose_requests_were_refused_for_credit_stops_the_run():
    def answer(cid: str, params: dict[str, Any]) -> Any:
        error = SimpleNamespace(
            error=SimpleNamespace(
                type="invalid_request_error", message="Your credit balance is too low"
            )
        )
        return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="errored", error=error))

    batcher, _ = _batcher(_FakeBatches(answer))
    with pytest.raises(EvalAborted, match="the account has no credit left") as caught:
        run_batch(_SUBSET, DeferredClient(_settings(caching=True)), batcher, _Corpus())
    assert caught.value.completed == 0


def test_a_batch_that_never_ends_stops_the_run():
    batches = _FakeBatches(statuses=("in_progress",))
    batcher, _ = _batcher(batches, poll_seconds=30.0, timeout_seconds=60.0)
    with pytest.raises(EvalAborted, match="batch submission 1: the batch could not be run"):
        run_batch(_SUBSET, DeferredClient(_settings(caching=True)), batcher, _Corpus())


# ================================================================= continuing a run


def test_a_continued_run_asks_only_for_what_was_not_done():
    wire = _Flaky(_from(4, _no_credit))
    with pytest.raises(EvalAborted) as caught:
        run_realtime(_SUBSET, _client(wire), _Corpus())
    done = {result.item.key: result for result in caught.value.results}
    assert len(done) == 3

    again = _Wire(_Billing())
    seen: list[str] = []
    said: list[str] = []
    run = run_realtime(
        _SUBSET,
        _client(again),
        _Corpus(),
        done=done,
        on_result=lambda result: seen.append(result.item.key),
        progress=said.append,
    )
    assert len(again.calls) == 4
    assert seen == [item.key for item in _SUBSET[3:]]  # only what it ran
    assert len(said) == 4
    assert [score.key for score in run.scores] == [item.key for item in _SUBSET]  # in set order
    assert all(score.answered for score in run.scores)
    # What was kept is what was measured then: the same object, tokens and all.
    assert run.results[0] is done["case_01:diagnose"]


def test_a_batch_is_continued_the_same_way():
    first = run_realtime(_SUBSET[:3], _client(_Wire(_Billing())), _Corpus())
    done = {result.item.key: result for result in first.results}
    batches = _FakeBatches(lambda cid, params: _succeeded(cid, _Billing().answer(params)))
    batcher, _ = _batcher(batches)
    run = run_batch(_SUBSET, DeferredClient(_settings(caching=True)), batcher, _Corpus(), done=done)
    assert [len(requests) for requests in batches.created] == [4]
    assert [score.key for score in run.scores] == [item.key for item in _SUBSET]


def test_a_run_with_nothing_left_makes_no_call():
    first = run_realtime(_SUBSET, _client(_Wire(_Billing())), _Corpus())
    done = {result.item.key: result for result in first.results}
    wire = _Wire(_Billing())
    run = run_realtime(_SUBSET, _client(wire), _Corpus(), done=done)
    assert wire.calls == [] and len(run.scores) == 7


# ======================================================================== the store


def _finished(tmp_path: Path) -> tuple[Path, Any]:
    """A run directory with a progress file, as a run leaves it, and the run that wrote it."""
    directory = tmp_path / "a-run"
    directory.mkdir()
    progress = ProgressFile(directory, _CONFIG)
    run = run_realtime(_SUBSET, _client(_Wire(_Billing())), _Corpus(), on_result=progress.write)
    return directory, run


def test_every_item_is_on_disk_as_it_finishes(tmp_path: Path):
    directory = tmp_path / "a-run"
    directory.mkdir()
    progress = ProgressFile(directory, _CONFIG)
    lines_seen: list[int] = []

    def write(result: Any) -> None:
        progress.write(result)
        lines_seen.append(len((directory / PROGRESS).read_text(encoding="utf-8").splitlines()))

    with pytest.raises(EvalAborted):
        run_realtime(_SUBSET, _client(_Flaky(_from(4, _no_credit))), _Corpus(), on_result=write)
    assert lines_seen == [2, 3, 4]  # the header, then one line an item, each as it landed
    header = json.loads((directory / PROGRESS).read_text(encoding="utf-8").splitlines()[0])
    assert header == {"header": _CONFIG.to_dict()}


def test_what_is_read_back_is_what_was_measured(tmp_path: Path):
    directory, written = _finished(tmp_path)
    stored = read_run(directory, CASES)
    assert stored.config == _CONFIG and stored.complete is False
    assert list(stored.results) == [item.key for item in _SUBSET]
    for result, again in zip(written.results, stored.results.values(), strict=True):
        was, now = result.score.to_dict(), again.score.to_dict()
        was.pop("seconds"), now.pop("seconds")
        assert now == was
        assert again.retrieved == result.retrieved
        assert again.response is not None
        assert again.response.model_dump(mode="json") == result.response.model_dump(mode="json")  # type: ignore[union-attr]


def test_what_the_environment_failed_is_asked_again_and_what_the_agent_failed_is_kept(
    tmp_path: Path,
):
    directory = tmp_path / "a-run"
    directory.mkdir()
    progress = ProgressFile(directory, _CONFIG)
    item, other, third = _SUBSET[0], _SUBSET[2], _SUBSET[4]
    broken = _diagnosis(item)
    broken["findings"]["severity"]["detail"] = "not allowed here"
    gave_up = run_realtime(
        [item],
        _client(_Scripted([_message(EMIT_TOOL_NAME, broken), _message(EMIT_TOOL_NAME, broken)])),
    )
    weather = run_realtime([other, third], _client(_Flaky({1: _overloaded})), _Corpus())
    for result in (*gave_up.results, *weather.results):
        progress.write(result)

    stored = read_run(directory, CASES)
    kinds = {key: result.score.error_kind for key, result in stored.results.items()}
    assert kinds == {item.key: "agent", other.key: "environment", third.key: None}
    assert sorted(stored.reusable()) == sorted([item.key, third.key])
    assert "identical" in (stored.results[item.key].score.error or "")


def test_a_run_that_ended_before_progress_files_is_read_from_its_record(tmp_path: Path):
    # Today's interrupted run: answered items, then credit refusals recorded as failures,
    # with no `error_kind` on any of them.
    run = run_realtime(_SUBSET[:3], _client(_Wire(_Billing())), _Corpus())
    record = to_record(run, CASES, run_id="legacy")
    responses = {
        result.item.key: {
            "response": result.response.model_dump(mode="json"),  # type: ignore[union-attr]
            "retrieved": None if result.retrieved is None else list(result.retrieved),
        }
        for result in run.results
    }
    refused = copy.deepcopy(record["items"][0])
    refused.update(
        key="case_02:recall",
        answered=False,
        correct=None,
        error="BadRequestError: Error code: 400 - Your credit balance is too low",
    )
    refused.pop("error_kind")
    gave_up = copy.deepcopy(refused)
    gave_up.update(key="case_07:diagnose", error="ValidationError: 1 validation error")
    record["items"] += [refused, gave_up]
    directory = tmp_path / "legacy"
    directory.mkdir()
    (directory / "eval.json").write_text(json.dumps(record), encoding="utf-8")
    (directory / "responses.json").write_text(json.dumps(responses), encoding="utf-8")

    stored = read_run(directory, CASES)
    assert stored.complete is True
    assert stored.results["case_02:recall"].score.error_kind == "environment"
    assert stored.results["case_07:diagnose"].score.error_kind == "agent"
    assert sorted(stored.reusable()) == sorted(
        [item.key for item in _SUBSET[:3]] + ["case_07:diagnose"]
    )


def test_the_kind_of_a_stored_failure():
    assert failure_kind({"answered": True}) is None
    assert failure_kind({"answered": False, "error_kind": "agent", "error": "APIError: x"}) == (
        "agent"
    )
    assert failure_kind({"answered": False, "error": "RateLimitError: slow down"}) == "environment"
    assert failure_kind({"answered": False, "error": "DocsAgentError: paraphrased"}) == "agent"
    assert failure_kind({"answered": False, "error": None}) == "agent"


def test_a_line_cut_off_by_a_kill_costs_that_item_and_no_other(tmp_path: Path):
    directory, _run = _finished(tmp_path)
    path = directory / PROGRESS
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) - 40], encoding="utf-8")
    stored = read_run(directory, CASES)
    assert list(stored.results) == [item.key for item in _SUBSET[:-1]]


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        (None, "holds neither progress.jsonl nor eval.json"),
        ("", "is empty"),
        ('{"key": "case_01:diagnose"}\n', "does not begin with a run header"),
        ('{"header": {"model": "m"}}\n', "does not begin with a run header"),
    ],
    ids=["nothing-there", "an-empty-file", "no-header", "half-a-header"],
)
def test_a_directory_that_cannot_be_read_says_why(
    tmp_path: Path, content: str | None, complaint: str
):
    if content is not None:
        (tmp_path / PROGRESS).write_text(content, encoding="utf-8")
    with pytest.raises(StoreError, match=complaint):
        read_run(tmp_path, CASES)


def test_two_runs_are_the_same_run_only_if_the_set_the_model_and_the_levers_are():
    from dataclasses import replace

    assert _CONFIG.differs_from(_CONFIG) == []
    assert _CONFIG.differs_from(replace(_CONFIG, mode="batch")) == ["mode: 'batch', not 'realtime'"]
    assert len(_CONFIG.differs_from(replace(_CONFIG, model="m", set_sha256="0"))) == 2
    assert _CONFIG.differs_from(replace(_CONFIG, cache_ttl="1h")) == ["cache_ttl: '1h', not '5m'"]
    # With caching off the lifetime changes nothing, so it cannot make two runs differ.
    off = replace(_CONFIG, prompt_caching=False)
    assert off.differs_from(replace(off, cache_ttl="1h")) == []


# ======================================================= an excerpt joined from its lines


_CONTEXT = ITEMS["case_02:diagnose"].context


def test_an_excerpt_is_verbatim_across_list_items():
    # The first live run: two adjacent metric lines quoted as one, without the marker.
    assert quoted("p50 latency: 38 ms -> 1850 ms\np99 latency: 95 ms -> 8400 ms", _CONTEXT) == (
        "verbatim"
    )
    assert quoted("- p50 latency: 38 ms -> 1850 ms", _CONTEXT) == "verbatim"
    assert quoted("Nightly  catalogue sync\nstarted", _CONTEXT) == "verbatim"


def test_an_excerpt_joined_from_verbatim_lines_is_stitched():
    assert quoted("error rate: 0.002 -> 0.002; p99 latency: 95 ms -> 8400 ms", _CONTEXT) == (
        "stitched"
    )
    assert quoted("requests affected: 3100 | p50 latency: 38 ms -> 1850 ms.", _CONTEXT) == (
        "stitched"
    )


def test_an_excerpt_with_any_part_made_up_is_neither():
    assert quoted("p50 latency: 38 ms -> 1850 ms; p99 latency: 95 ms -> 9000 ms", _CONTEXT) is None
    assert quoted("Latency rose roughly fifty-fold.", _CONTEXT) is None
    assert quoted("p50 latency rose from 38 ms to 1850 ms", _CONTEXT) is None


def test_a_stitched_excerpt_is_counted_on_its_own_line_and_not_as_invented():
    item = ITEMS["case_02:diagnose"]
    stitched = "error rate: 0.002 -> 0.002; p99 latency: 95 ms -> 8400 ms"
    score = score_item(item, _diagnosed(item, excerpt=stitched))
    assert score.grounding.ungrounded == ()
    assert score.grounding.stitched == ("evidence ev_1: joined from lines that are each verbatim",)
    summary = summarise([score])
    assert (summary.ungrounded.hits, summary.stitched.hits) == (0, 1)
    assert summary.stitched.total == score.grounding.checked
    assert score.to_dict()["grounding"]["stitched"] == list(score.grounding.stitched)


# =================================================================== the scripts, live path


def _budget(state: dict[str, Any], calls: int) -> None:
    """Let the scripted account pay for ``calls`` more calls, then run out of credit."""
    spent = {"n": 0}

    def answer(params: dict[str, Any]) -> Any:
        spent["n"] += 1
        if spent["n"] > calls:
            raise _no_credit()
        return state["bill"].answer(params)  # one bill a client, so the cache is read

    state["answer"] = answer


@pytest.fixture
def account(wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: F811
    """`wired`, with an account whose credit can run out: ``state['answer']`` overrides
    how a realtime call is answered."""
    state = wired

    def client(settings: Any) -> LLMClient:
        calls: list[dict[str, Any]] = []

        def create(**kwargs: Any) -> Any:
            calls.append(kwargs)
            if state.get("answer") is not None:
                return state["answer"](kwargs)
            return state["bill"].answer(kwargs)

        state["bill"] = _Billing()
        state["calls"].append(calls)
        return LLMClient(settings, client=SimpleNamespace(messages=SimpleNamespace(create=create)))  # type: ignore[arg-type]

    monkeypatch.setattr(run_evals, "LLMClient", client)
    return state


def _calls(state: dict[str, Any]) -> int:
    return sum(len(calls) for calls in state["calls"])


def _runs(results: Path) -> list[Path]:
    return sorted(results.glob("runs/*/*__llm__evals-*"))


_FOUR = ["--cases", "case_01,case_02,case_07,case_19"]


def test_a_run_that_runs_out_of_credit_stops_and_says_how_to_continue(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    _budget(account, 3)
    assert run_evals.main(_FOUR) == 2
    err = capsys.readouterr().err
    assert "ABORTED: stopped at case_02:recall: the account has no credit left" in err
    assert "3 item(s) had been scored and are kept" in err
    (run,) = _runs(account["results"])
    assert f"to continue it: run_evals.py --resume {run}" in err
    assert _calls(account) == 4

    # What it finished is on disk twice over: to be continued from, and on the cost record.
    assert len((run / PROGRESS).read_text(encoding="utf-8").splitlines()) == 1 + 3
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "error"
    assert not (run / "eval.json").exists()
    assert review(account["results"], since=None)["total"].output_tokens == 3 * 900


def test_resume_finishes_the_run_and_pays_only_for_the_rest(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    _budget(account, 3)
    assert run_evals.main(_FOUR) == 2
    (stopped,) = _runs(account["results"])
    account["answer"] = None
    capsys.readouterr()

    assert run_evals.main([*_FOUR, "--resume", str(stopped)]) == 0
    out = capsys.readouterr().out
    assert f"continuing {stopped.name}: 3 item(s) kept, 4 to run" in out
    assert "- Answered: 7/7 = 100%." in out
    assert _calls(account) == 4 + 4  # the stopped run's four, and the four it had left

    finished = next(run for run in _runs(account["results"]) if run != stopped)
    record = json.loads((finished / "eval.json").read_text(encoding="utf-8"))
    assert [item["key"] for item in record["items"]] == [
        "case_01:diagnose",
        "case_01:recall",
        "case_02:diagnose",
        "case_02:recall",
        "case_07:diagnose",
        "case_07:recall",
        "case_19:recall",
    ]
    assert record["summary"]["usage"]["out"] == 7 * 900  # the whole run, as if made at once
    meta = json.loads((finished / "run.json").read_text(encoding="utf-8"))["metadata"]
    assert (meta["resumed_from"], meta["items_kept"]) == (stopped.name, 3)
    # The finished run's directory can itself be continued from: it holds all seven.
    assert len(read_run(finished, CASES).reusable()) == 7


def test_a_token_is_counted_once_across_the_stopped_run_and_the_one_that_finished_it(
    account: dict[str, Any],
):
    _budget(account, 3)
    assert run_evals.main(_FOUR) == 2
    (stopped,) = _runs(account["results"])
    account["answer"] = None
    assert run_evals.main([*_FOUR, "--resume", str(stopped)]) == 0

    result = review(account["results"], since=None)
    assert result["total"].output_tokens == 7 * 900
    assert result["total"].runs == 2 and result["total"].unmeasured_runs == 0
    finished = next(run for run in _runs(account["results"]) if run != stopped)
    events = [
        json.loads(line)
        for line in (finished / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reused = [event["name"] for event in events if event["data"].get("reused_from")]
    assert reused == ["case_01:diagnose", "case_01:recall", "case_02:diagnose"]
    assert {event["data"]["reused_from"] for event in events[:3]} == {stopped.name}


def test_an_answer_whose_run_never_recorded_its_cost_is_counted_by_the_run_that_kept_it(
    account: dict[str, Any], tmp_path: Path
):
    # A run that was killed, not stopped: a progress file and nothing else.
    killed = tmp_path / "killed"
    killed.mkdir()
    progress = ProgressFile(killed, _CONFIG)
    for result in run_realtime(_SUBSET[:3], _client(_Wire(_Billing())), _Corpus()).results:
        progress.write(result)

    assert run_evals.main([*_FOUR, "--resume", str(killed)]) == 0
    assert _calls(account) == 4
    assert review(account["results"], since=None)["total"].output_tokens == 7 * 900


@pytest.mark.parametrize(
    ("flags", "complaint"),
    [
        (["--no-cache"], "prompt_caching: True, not False"),
        (["--mode", "batch"], "mode: 'realtime', not 'batch'"),
        (["--model", "claude-haiku-4-5"], "model: 'claude-sonnet-5', not 'claude-haiku-4-5'"),
    ],
    ids=["another-cache-setting", "another-mode", "another-model"],
)
def test_a_run_of_another_kind_cannot_be_continued(
    account: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    complaint: str,
):
    assert run_evals.main(_FOUR) == 0
    (first,) = _runs(account["results"])
    before = _calls(account)
    capsys.readouterr()
    assert run_evals.main([*_FOUR, *flags, "--resume", str(first)]) == 2
    err = capsys.readouterr().err
    assert "cannot resume" in err and "is a different run" in err and complaint in err
    assert _calls(account) == before


def test_resuming_from_nothing_says_so(account: dict[str, Any], tmp_path: Path, capsys: Any):
    assert run_evals.main([*_FOUR, "--resume", str(tmp_path / "missing")]) == 2
    assert "cannot resume" in capsys.readouterr().err


def test_a_rescore_can_be_kept_beside_the_run_it_rescored(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(_FOUR) == 0
    (run,) = _runs(account["results"])
    original = (run / "eval.json").read_text(encoding="utf-8")
    assert run_evals.main(["--rescore", str(run), "--save"]) == 0
    assert "saved:" in capsys.readouterr().out
    assert (run / "eval.json").read_text(encoding="utf-8") == original  # left alone
    rescored = json.loads((run / "eval.rescored.json").read_text(encoding="utf-8"))
    assert rescored["run"]["run_id"] == run.name
    assert rescored["summary"] == json.loads(original)["summary"]
    assert (
        (run / "report.rescored.md")
        .read_text(encoding="utf-8")
        .startswith("# Eval run, rescored: ")
    )


# ---------------------------------------------------------------- the checkpoint, resumed


def _checkpoint(state: dict[str, Any], *flags: str) -> int:
    return check_day20_baseline.main(
        ["--out", str(state["out"]), "--results", str(state["results"]), *flags]
    )


def test_the_checkpoint_stops_on_an_empty_account_and_says_how_to_go_on(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    _budget(account, 4 + 5)  # the smoke test, then five items of the first run
    assert _checkpoint(account, "--limit", "6") == 2
    err = capsys.readouterr().err
    assert "ABORTED: stopped at case_03:recall: the account has no credit left" in err
    assert "check_day20_baseline.py --resume --skip-smoke" in err
    assert _calls(account) == 4 + 6
    assert not account["out"].exists()


def test_the_checkpoint_resumed_pays_for_what_was_left_and_nothing_else(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    _budget(account, 4 + 5)
    assert _checkpoint(account, "--limit", "6") == 2
    account["answer"] = None
    before = _calls(account)
    capsys.readouterr()

    assert _checkpoint(account, "--limit", "6", "--resume", "--skip-smoke") == 0
    out = capsys.readouterr().out
    assert "continuing from what is recorded" in out
    assert "realtime, uncached  5 kept from" in out and "; 1 to run" in out
    # The smoke test's diagnoses are the cached run's first items: three of its six.
    assert "realtime, cached    3 kept from" in out and "; 3 to run" in out
    assert "batch, cached       nothing recorded; 6 to run" in out
    assert "--- PASS" in out
    assert _calls(account) - before == 1 + 3  # and the batch, which went to the batcher


def test_a_configuration_already_run_to_the_end_costs_nothing_the_second_time(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    flags = ("--limit", "6", "--skip-smoke", "--configs", "realtime-uncached,realtime-cached")
    assert _checkpoint(account, *flags) == 0
    before = _calls(account)
    capsys.readouterr()
    assert _checkpoint(account, *flags, "--resume") == 0
    out = capsys.readouterr().out
    assert out.count("6 kept from") == 2 and out.count("; 0 to run") == 2
    assert _calls(account) == before


def test_the_plan_shows_what_a_resume_would_keep_and_is_still_free(
    account: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    assert run_evals.main(["--limit", "6", "--no-cache"]) == 0
    before = _calls(account)
    capsys.readouterr()
    assert _checkpoint(account, "--plan", "--resume", "--limit", "6") == 0
    out = capsys.readouterr().out
    assert "realtime, uncached  6 kept from" in out
    assert "realtime, cached    nothing recorded; 6 to run" in out
    assert _calls(account) == before


def test_the_plan_prices_a_diagnosis_and_a_recall_differently():
    items = [ITEMS["case_01:diagnose"], ITEMS["case_01:recall"]]
    projection = run_evals.project(items[:1], _BASE)
    ((key, tokens, output),) = projection.sized
    assert (key, output) == ("case_01:diagnose", 3_000)
    # Measured: 7,914 input tokens for this request. The estimate has to be in reach.
    assert 7_000 < tokens < 9_000
