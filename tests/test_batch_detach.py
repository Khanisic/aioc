"""Day 24: a batch run that nobody waits on.

The Day 20 baseline's three batches sat in the queue for 2 h 40 min, 1 h 42 min, and
45 min, and the process polling the first died after seventeen minutes. Day 24's re-run
is submitted and left: `--detach` sends the batch, records where the run got to, and
exits; the same command with `--resume` checks on it, for free while it is still running,
and carries on when it has ended. The rules these tests hold:

- detaching submits exactly what a waiting run would have submitted, and polls nothing;
- a batch still running is reported, never waited on, and nothing new is sent;
- a refused report's retry is the next batch, submitted by the resume that read the
  first one - so a run with retries detaches once a pass;
- a resumed run lists every batch it was given as well as its own, because replaying an
  item asks for its first call again and only the earliest batch holds that answer;
- the run that finally finishes scores what a run that waited would have scored, and a
  token is counted once across every run in the chain.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import run_evals
from cost_review import review

from aioc.agents import EMIT_TOOL_NAME
from aioc.evals import BATCHES, BatchDetached, read_run, run_batch, submitted_batches
from aioc.llm import BatchStatus, DeferredClient, MessageBatcher, PendingRequest
from tests.test_baseline import _Billing, _settings
from tests.test_batch import _batcher, _FakeBatches, _succeeded
from tests.test_eval_resume import _FOUR, _runs
from tests.test_eval_resume import account as account  # noqa: F401 - fixtures, used by name
from tests.test_eval_resume import queued as queued  # noqa: F401
from tests.test_eval_resume import wired as wired  # noqa: F401
from tests.test_evals import CASES, _answer, _Corpus, _item_of, _message

_SUBSET = list(CASES.select(cases={"case_01", "case_02", "case_07", "case_19"}))  # 7 items
_RETRIED = "case_01:diagnose"


def _refuse_first(params: dict[str, Any]) -> Any:
    """Every item answered rightly, except that ``_RETRIED``'s first report breaks the
    contract (a detail on a non-`other` enum), so its retry needs a second batch."""
    if _item_of(params).key == _RETRIED and len(params["messages"]) == 1:
        broken = copy.deepcopy(_answer(params).content[0].input)
        broken["findings"]["severity"]["detail"] = "not allowed here"
        return _message(EMIT_TOOL_NAME, broken)
    return _answer(params)


def _counting(batches: _FakeBatches) -> MessageBatcher:
    batcher, _ = _batcher(batches)
    return batcher


# ================================================================= the wire, one poll


def test_status_is_one_poll_that_reads_the_counts_and_never_waits():
    batches = _FakeBatches(statuses=("in_progress",))
    batcher, clock = _batcher(batches)
    batch_id = batcher.submit({"req_a": {"messages": []}, "req_b": {"messages": []}})

    status = batcher.status(batch_id)
    assert isinstance(status, BatchStatus)
    assert (status.processing_status, status.ended) == ("in_progress", False)
    assert (status.processing, status.succeeded) == (2, 0)
    assert batches.retrieved == 1 and clock.slept == []
    assert status.render() == (
        f"batch {batch_id}: in_progress "
        "(2 processing, 0 succeeded, 0 errored, 0 canceled, 0 expired)"
    )


def test_an_ended_batch_says_so_with_its_counts():
    batches = _FakeBatches(statuses=("ended",))
    batcher, _ = _batcher(batches)
    batch_id = batcher.submit({"req_a": {"messages": []}})
    status = batcher.status(batch_id)
    assert status.ended and (status.processing, status.succeeded) == (0, 1)


def test_submit_sends_the_queue_and_returns_without_polling():
    batches = _FakeBatches()
    batcher, clock = _batcher(batches)
    client = DeferredClient(_settings(caching=True))
    with pytest.raises(PendingRequest):
        client.complete(messages=[{"role": "user", "content": "hello"}])
    told: list[tuple[str, int]] = []

    batch_id = client.submit(batcher, on_submit=lambda b, n: told.append((b, n)))
    assert batch_id == "msgbatch_1" and told == [("msgbatch_1", 1)]
    assert client.pending == {} and batches.retrieved == 0 and clock.slept == []
    # The answers are read later, by whoever has the id.
    client.attach(batcher, batch_id)
    assert client.complete(messages=[{"role": "user", "content": "hello"}]) is not None


def test_submitting_an_empty_queue_sends_nothing():
    batches = _FakeBatches()
    assert DeferredClient(_settings(caching=True)).submit(_counting(batches)) is None
    assert batches.created == []


# ============================================================== the runner, detached


def _detach(
    batches: _FakeBatches, submitted: list[str], told: list[tuple[str, int]] | None = None
) -> BatchDetached | Any:
    """One detached step of the run: the `BatchDetached` it stopped on, or the finished run."""
    try:
        return run_batch(
            _SUBSET,
            DeferredClient(_settings(caching=True)),
            _counting(batches),
            _Corpus(),
            submitted=submitted,
            on_submit=None if told is None else (lambda b, n: told.append((b, n))),
            detach=True,
        )
    except BatchDetached as stop:
        return stop


def test_detaching_submits_what_a_waiting_run_would_and_polls_nothing():
    waited = _FakeBatches(lambda cid, params: _succeeded(cid, _answer(params)))
    run_batch(_SUBSET, DeferredClient(_settings(caching=True)), _counting(waited), _Corpus())

    detached = _FakeBatches(lambda cid, params: _succeeded(cid, _answer(params)))
    told: list[tuple[str, int]] = []
    stop = _detach(detached, [], told)
    assert isinstance(stop, BatchDetached)
    assert (stop.batch_id, stop.submitted, stop.status, stop.results) == (
        "msgbatch_1",
        True,
        None,
        [],
    )
    assert told == [("msgbatch_1", 7)]
    assert detached.retrieved == 0
    assert detached.created == waited.created[:1]  # the same seven requests, byte for byte


def test_a_batch_still_running_is_reported_and_nothing_is_sent():
    batches = _FakeBatches(
        lambda cid, params: _succeeded(cid, _answer(params)), statuses=("in_progress",)
    )
    first = _detach(batches, [])
    stop = _detach(batches, [first.batch_id])
    assert isinstance(stop, BatchDetached) and not stop.submitted
    assert stop.status is not None and stop.status.processing_status == "in_progress"
    assert stop.status.processing == 7
    assert len(batches.created) == 1 and batches.retrieved == 1  # one poll, one batch


def test_a_retry_is_the_next_batch_and_the_chain_scores_what_waiting_would():
    waited = _FakeBatches(
        lambda cid, params: _succeeded(cid, _refuse_first(params)), statuses=("ended",)
    )
    reference = run_batch(
        _SUBSET, DeferredClient(_settings(caching=True)), _counting(waited), _Corpus()
    )

    batches = _FakeBatches(
        lambda cid, params: _succeeded(cid, _refuse_first(params)), statuses=("ended",)
    )
    first = _detach(batches, [])
    assert isinstance(first, BatchDetached) and first.submitted
    second = _detach(batches, [first.batch_id])
    # The first batch ended: six items are finished, and the refused report's retry went out.
    assert isinstance(second, BatchDetached) and second.submitted
    assert second.batch_id == "msgbatch_2" and len(batches.created[1]) == 1
    assert sorted(r.item.key for r in second.results) == sorted(
        item.key for item in _SUBSET if item.key != _RETRIED
    )
    run = _detach(batches, [first.batch_id, second.batch_id])
    assert not isinstance(run, BatchDetached)
    assert len(batches.created) == 2  # nothing after the retry

    assert [s.to_dict() | {"seconds": None} for s in run.scores] == [
        s.to_dict() | {"seconds": None} for s in reference.scores
    ]
    retried = next(s for s in run.scores if s.key == _RETRIED)
    assert (retried.retry.attempts, retried.retry.outcome) == (2, "recovered")
    assert retried.usage.output_tokens == 2 * 900  # both calls, counted on the last pass


def test_a_resumed_run_that_cannot_poll_stops_and_keeps_what_it_had():
    class _Unreachable(_FakeBatches):
        def retrieve(self, batch_id: str) -> SimpleNamespace:
            import anthropic
            import httpx

            raise anthropic.APIConnectionError(
                request=httpx.Request("GET", "https://api.anthropic.com")
            )

    from aioc.evals import EvalAborted

    with pytest.raises(EvalAborted, match="batch msgbatch_9: an earlier batch could not be polled"):
        _detach(_Unreachable(), ["msgbatch_9"])


# ======================================================================== the script


def _detached_runs(state: dict[str, Any]) -> list[Path]:
    # Runs started in the same second are told apart by a suffix: `evals-batch-2`.
    return [path for path in _runs(state["results"]) if "__llm__evals-batch" in path.name]


def test_detach_needs_a_batch():
    with pytest.raises(SystemExit, match="--detach needs --mode batch"):
        run_evals.main([*_FOUR, "--detach"])


def test_the_script_submits_records_and_says_how_to_go_on(
    queued: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    queued["batch_statuses"] = ("in_progress",)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach"]) == run_evals.DETACHED == 3
    out = capsys.readouterr().out
    (run,) = _detached_runs(queued)
    assert "SUBMITTED: batch msgbatch_1. Not waiting for it." in out
    assert "0 item(s) scored so far" in out
    assert (
        "uv run python scripts/run_evals.py --mode batch --detach "
        f"--cases case_01,case_02,case_07,case_19 --resume {run}"
    ) in out
    assert submitted_batches(run) == ("msgbatch_1",)
    assert not (run / "eval.json").exists()
    summary = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "skipped"
    assert queued["batches"].retrieved == 0


def test_checking_a_running_batch_sends_nothing_and_finishing_reads_it_back(
    queued: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    queued["batch_statuses"] = ("in_progress",)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach"]) == 3
    (first,) = _detached_runs(queued)
    capsys.readouterr()

    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach", "--resume", str(first)]) == 3
    out = capsys.readouterr().out
    assert "STILL RUNNING: batch msgbatch_1: in_progress (7 processing" in out
    assert len(queued["batches"].created) == 1
    second = next(p for p in _detached_runs(queued) if p != first)
    # The check is itself a run to resume from, and it still knows the batch.
    assert submitted_batches(second) == ("msgbatch_1",)
    carried = json.loads((second / BATCHES).read_text(encoding="utf-8").splitlines()[0])
    assert carried == {"batch_id": "msgbatch_1", "requests": None, "carried_from": first.name}

    queued["batch_statuses"] = ("ended",)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach", "--resume", str(second)]) == 0
    out = capsys.readouterr().out
    assert "- Answered: 7/7 = 100%." in out
    assert len(queued["batches"].created) == 1  # read back, never paid for twice
    finished = next(p for p in _detached_runs(queued) if p not in (first, second))
    assert (finished / "eval.json").is_file()
    assert read_run(finished, CASES).complete


def test_a_token_is_counted_once_across_a_detached_chain_with_a_retry(
    queued: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bill = _Billing()

    def answer(cid: str, params: dict[str, Any]) -> Any:
        message = bill.answer(params)
        if _item_of(params).key == _RETRIED and len(params["messages"]) == 1:
            payload = copy.deepcopy(message.content[0].input)
            payload["findings"]["severity"]["detail"] = "not allowed here"
            message = copy.copy(message)
            message.content = _message(EMIT_TOOL_NAME, payload).content
        return _succeeded(cid, message)

    monkeypatch.setattr(queued["batches"], "_answer", answer)
    resume: list[str] = []
    codes = []
    for _ in range(4):
        codes.append(run_evals.main([*_FOUR, "--mode", "batch", "--detach", *resume]))
        if codes[-1] == 0:
            break
        # Exactly what a person does: resume the directory the last run printed.
        printed = capsys.readouterr().out.rsplit("--resume ", 1)[1].strip()
        resume = ["--resume", printed]
    assert codes == [3, 3, 0]  # the first attempts, the retry, then done
    assert len(queued["batches"].created) == 2

    result = review(queued["results"], since=None)
    assert result["total"].output_tokens == 8 * 900  # seven items, one of them asked twice
    assert result["total"].unmeasured_runs == 0


def test_resuming_an_older_run_in_a_chain_continues_the_newest(
    queued: dict[str, Any], capsys: pytest.CaptureFixture[str]
):
    queued["batch_statuses"] = ("in_progress",)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach"]) == 3
    (first,) = _detached_runs(queued)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach", "--resume", str(first)]) == 3
    second = next(p for p in _detached_runs(queued) if p != first)
    assert run_evals.continued_by(first) == second and run_evals.continued_by(second) is None
    capsys.readouterr()

    # The first command printed, given again, is still the right one.
    queued["batch_statuses"] = ("ended",)
    assert run_evals.main([*_FOUR, "--mode", "batch", "--detach", "--resume", str(first)]) == 0
    out = capsys.readouterr().out
    assert f"{first.name} was continued by {second.name}; continuing that one" in out
    assert "- Answered: 7/7 = 100%." in out
    assert len(queued["batches"].created) == 1
    third = next(p for p in _detached_runs(queued) if p not in (first, second))
    assert run_evals.continued_by(first) == third
