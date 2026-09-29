"""Day 19 cost levers, the batch half: the wire, and running unchanged code through it.

Offline: a scripted fake stands in for the Message Batches API. Two things are under test.

`MessageBatcher` is the wire - what is submitted, how long it waits, and that a result is
read by ``custom_id`` rather than by position, since a batch returns them in any order.

`DeferredClient` is the claim that the Batch API "changes nothing in the harness but the
entry point". The tests hold it to that literally: the shipped `IncidentAgent.diagnose`
is run against it, raises on the call that has no answer yet, and returns a validated
response once the batch has been flushed - including when the first report is refused
and the validation-retry loop asks again, which is simply one more pass.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import ValidationError

from aioc.agents import EMIT_TOOL_NAME, IncidentAgent, RetryLog
from aioc.contracts import IncidentAgentResponse
from aioc.llm import (
    BatchError,
    DeferredClient,
    LLMClient,
    LLMSettings,
    MessageBatcher,
    PendingRequest,
    ToolSpec,
    Usage,
    request_key,
)
from tests.test_incident_agent import _STRUCTURED_PAYLOAD

# ------------------------------------------------------------------------------- fakes


def _emit(payload: dict[str, Any], *, tool_use_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="tool_use",
        model="claude-sonnet-5",
        content=[ToolUseBlock(type="tool_use", id=tool_use_id, name=EMIT_TOOL_NAME, input=payload)],
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=240,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=4_000,
        ),
    )


def _succeeded(custom_id: str, message: Any) -> SimpleNamespace:
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


def _errored(custom_id: str, kind: str, message: str) -> SimpleNamespace:
    error = SimpleNamespace(error=SimpleNamespace(type=kind, message=message))
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored", error=error))


class _FakeBatches:
    """The three calls the batcher makes. ``answer`` turns one request's params into its
    result entry; ``statuses`` is what successive `retrieve` calls report."""

    def __init__(
        self,
        answer: Callable[[str, dict[str, Any]], Any] | None = None,
        *,
        statuses: tuple[str, ...] = ("in_progress", "ended"),
    ) -> None:
        self._answer = answer or (lambda cid, _params: _succeeded(cid, _emit(_STRUCTURED_PAYLOAD)))
        self._statuses = list(statuses)
        self.created: list[list[dict[str, Any]]] = []
        self.retrieved = 0

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self.retrieved += 1
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return SimpleNamespace(id=batch_id, processing_status=status)

    def results(self, batch_id: str) -> list[Any]:
        number = int(batch_id.removeprefix("msgbatch_"))
        entries = [self._answer(r["custom_id"], r["params"]) for r in self.created[number - 1]]
        # Any order: a consumer that reads by position is wrong, so give it the wrong order.
        return [entry for entry in reversed(entries) if entry is not None]


class _FakeAnthropic:
    def __init__(self, batches: _FakeBatches) -> None:
        self.messages = SimpleNamespace(batches=batches)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _batcher(batches: _FakeBatches, **kwargs: Any) -> tuple[MessageBatcher, _Clock]:
    clock = _Clock()
    batcher = MessageBatcher(
        _FakeAnthropic(batches),  # type: ignore[arg-type]
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )
    return batcher, clock


_SETTINGS = LLMSettings(model="claude-sonnet-5", max_tokens=1024, prompt_caching=True)
_MESSAGES: Any = [{"role": "user", "content": "hello"}]
_CONTEXT = "payments-api 5xx ratio 0.1% at 14:00 -> 7.4% at 14:10."


# ------------------------------------------------------------------------ request keys


def test_a_request_key_is_a_valid_custom_id():
    key = request_key({"model": "m", "max_tokens": 1, "messages": _MESSAGES})
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key)


def test_the_same_request_has_the_same_key_whatever_order_it_was_built_in():
    one = {"model": "m", "max_tokens": 1, "messages": _MESSAGES, "system": "s"}
    other = {"system": "s", "messages": _MESSAGES, "max_tokens": 1, "model": "m"}
    assert request_key(one) == request_key(other)


def test_any_difference_is_a_different_request():
    base = {"model": "m", "max_tokens": 1, "messages": _MESSAGES}
    keys = {
        request_key(base),
        request_key({**base, "max_tokens": 2}),
        request_key({**base, "model": "n"}),
        request_key({**base, "messages": [{"role": "user", "content": "hello."}]}),
        request_key({**base, "system": "s"}),
    }
    assert len(keys) == 5


def test_a_replayed_assistant_turn_is_part_of_the_key():
    # The retry's request carries the SDK's own content blocks; two different refused
    # payloads must not collide.
    def convo(payload: dict[str, Any]) -> dict[str, Any]:
        block = ToolUseBlock(type="tool_use", id="toolu_1", name="emit", input=payload)
        return {
            "model": "m",
            "max_tokens": 1,
            "messages": [*_MESSAGES, {"role": "assistant", "content": [block]}],
        }

    assert request_key(convo({"a": 1})) == request_key(convo({"a": 1}))
    assert request_key(convo({"a": 1})) != request_key(convo({"a": 2}))


# ---------------------------------------------------------------------------- the wire


def test_submit_sends_each_request_under_its_custom_id():
    batches = _FakeBatches()
    batcher, _ = _batcher(batches)
    batch_id = batcher.submit({"req_a": {"model": "m"}, "req_b": {"model": "n"}})
    assert batch_id == "msgbatch_1"
    assert batches.created == [
        [
            {"custom_id": "req_a", "params": {"model": "m"}},
            {"custom_id": "req_b", "params": {"model": "n"}},
        ]
    ]


def test_an_empty_batch_is_refused_before_it_is_sent():
    batches = _FakeBatches()
    batcher, _ = _batcher(batches)
    with pytest.raises(ValueError, match="at least one request"):
        batcher.submit({})
    assert batches.created == []


def test_wait_polls_until_the_batch_has_ended():
    batches = _FakeBatches(statuses=("in_progress", "in_progress", "ended"))
    seen: list[tuple[str, str, float]] = []
    batcher, clock = _batcher(batches, poll_seconds=20.0, on_poll=lambda *a: seen.append(a))
    batcher.wait("msgbatch_1")
    assert batches.retrieved == 3
    assert clock.slept == [20.0, 20.0]
    assert [status for _, status, _ in seen] == ["in_progress", "in_progress", "ended"]
    assert seen[-1] == ("msgbatch_1", "ended", 40.0)


def test_a_batch_that_never_ends_is_an_error_that_names_it():
    batches = _FakeBatches(statuses=("in_progress",))
    batcher, _ = _batcher(batches, poll_seconds=30.0, timeout_seconds=90.0)
    with pytest.raises(BatchError, match="msgbatch_7 had not ended after 90s"):
        batcher.wait("msgbatch_7")


def test_results_are_read_by_custom_id_not_by_position():
    def answer(cid: str, params: dict[str, Any]) -> Any:
        return _succeeded(cid, SimpleNamespace(marker=params["model"]))

    batches = _FakeBatches(answer)
    batcher, _ = _batcher(batches)
    _, results = batcher.run({"req_a": {"model": "first"}, "req_b": {"model": "second"}})
    assert results["req_a"].message.marker == "first"  # type: ignore[union-attr]
    assert results["req_b"].message.marker == "second"  # type: ignore[union-attr]


def test_a_request_that_did_not_succeed_says_why():
    def answer(cid: str, _params: dict[str, Any]) -> Any:
        if cid == "req_bad":
            return _errored(cid, "invalid_request_error", "max_tokens: too large")
        if cid == "req_late":
            return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="expired"))
        if cid == "req_lost":
            return None  # the batch ended and never mentioned it
        return _succeeded(cid, _emit(_STRUCTURED_PAYLOAD))

    batches = _FakeBatches(answer)
    batcher, _ = _batcher(batches)
    run, results = batcher.run(
        {"req_ok": {}, "req_bad": {}, "req_late": {}, "req_lost": {}},
    )
    assert results["req_ok"].error is None and results["req_ok"].message is not None
    assert results["req_bad"].error == "errored: invalid_request_error: max_tokens: too large"
    assert results["req_late"].error == "expired"
    assert results["req_lost"].error == "the batch ended with no result for it"
    assert (run.requests, run.succeeded, run.failed) == (4, 1, 3)


# ------------------------------------------------------------------ the deferred client


def test_a_request_with_no_answer_is_queued_and_raises():
    client = DeferredClient(_SETTINGS)
    with pytest.raises(PendingRequest) as caught:
        client.complete(messages=_MESSAGES, system="s")
    assert list(client.pending) == [caught.value.key]


def test_the_queued_request_is_the_one_the_realtime_client_would_send():
    deferred = DeferredClient(_SETTINGS)
    with pytest.raises(PendingRequest) as caught:
        deferred.complete(messages=_MESSAGES, system="s", tool_choice={"type": "auto"})

    sent: list[dict[str, Any]] = []
    fake = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or _emit(_STRUCTURED_PAYLOAD))
    )
    LLMClient(_SETTINGS, client=fake).complete(  # type: ignore[arg-type]
        messages=_MESSAGES, system="s", tool_choice={"type": "auto"}
    )
    assert deferred.pending[caught.value.key] == sent[0]
    assert sent[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_same_request_twice_is_one_entry_in_the_batch():
    client = DeferredClient(_SETTINGS)
    for _ in range(2):
        with pytest.raises(PendingRequest):
            client.complete(messages=_MESSAGES, system="s")
    assert len(client.pending) == 1


def test_after_a_flush_the_request_is_answered_from_the_batch():
    batches = _FakeBatches()
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS)
    with pytest.raises(PendingRequest):
        client.complete(messages=_MESSAGES, system="s")

    run = client.flush(batcher)
    assert run is not None and (run.requests, run.succeeded) == (1, 1)
    assert client.pending == {}
    message = client.complete(messages=_MESSAGES, system="s")
    assert message.stop_reason == "tool_use"
    assert len(batches.created) == 1  # answered from the store: nothing was sent again


def test_flushing_an_empty_queue_submits_nothing():
    batches = _FakeBatches()
    batcher, _ = _batcher(batches)
    assert DeferredClient(_SETTINGS).flush(batcher) is None
    assert batches.created == []


def test_a_request_the_batch_failed_raises_its_reason_and_is_not_requeued():
    batches = _FakeBatches(lambda cid, _p: _errored(cid, "invalid_request_error", "bad schema"))
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS)
    with pytest.raises(PendingRequest):
        client.complete(messages=_MESSAGES)
    client.flush(batcher)
    with pytest.raises(BatchError, match="invalid_request_error: bad schema"):
        client.complete(messages=_MESSAGES)
    assert client.pending == {}


def test_what_a_batch_cannot_carry_is_refused_not_approximated():
    client = DeferredClient(_SETTINGS)
    echo = ToolSpec("echo", "Echo.", {"type": "object", "properties": {}}, lambda _a: "ok")
    with pytest.raises(BatchError, match="tool loop cannot be batched"):
        client.run_tool_loop(messages=_MESSAGES, tools=[echo])
    with pytest.raises(BatchError, match="streaming cannot be batched"):
        client.stream_text(messages=_MESSAGES)


def test_a_deferred_client_needs_no_api_key():
    client = DeferredClient(LLMSettings(anthropic_api_key=None, model="claude-sonnet-5"))
    with pytest.raises(PendingRequest):
        client.complete(messages=_MESSAGES)


# ------------------------------------------------- the shipped agent, through a batch


def _diagnose(client: LLMClient, usage: Usage, log: RetryLog) -> IncidentAgentResponse:
    return IncidentAgent(client, retry_log=log).diagnose(
        "What broke?",
        context=_CONTEXT,
        request_id="req_case",
        invocation_id="inv_case",
        usage=usage,
    )


def test_the_agent_runs_unchanged_and_answers_on_the_second_pass():
    batches = _FakeBatches()
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS)

    with pytest.raises(PendingRequest):
        _diagnose(client, Usage(), RetryLog())
    client.flush(batcher)

    usage, log = Usage(), RetryLog()
    response = _diagnose(client, usage, log)
    assert isinstance(response, IncidentAgentResponse)
    assert response.findings.failure_mode.value is not None
    # One model call was made, so one call is counted - the first pass spent nothing.
    assert (usage.input_tokens, usage.output_tokens) == (4_120, 240)
    assert usage.cache_read_tokens == 4_000
    assert [r.outcome for r in log.records] == ["accepted"]
    # What went out is the agent's own forced emit.
    (request,) = batches.created[0]
    assert request["params"]["tool_choice"] == {"type": "tool", "name": EMIT_TOOL_NAME}
    assert request["custom_id"] == request_key(request["params"])


def _refused_then_fixed(cid: str, params: dict[str, Any]) -> Any:
    """The first report breaks the contract (a detail on a non-`other` enum); the
    re-request, which carries the error, gets the valid one."""
    if len(params["messages"]) == 1:
        broken = copy.deepcopy(_STRUCTURED_PAYLOAD)
        broken["findings"]["severity"]["detail"] = "not allowed here"
        return _succeeded(cid, _emit(broken))
    return _succeeded(cid, _emit(_STRUCTURED_PAYLOAD, tool_use_id="toolu_2"))


def test_a_refused_report_is_retried_by_the_next_pass():
    batches = _FakeBatches(_refused_then_fixed)
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS.model_copy(update={"max_validation_retries": 2}))

    passes = 0
    response = None
    usage, log = Usage(), RetryLog()
    while response is None:
        passes += 1
        usage, log = Usage(), RetryLog()
        try:
            response = _diagnose(client, usage, log)
        except PendingRequest:
            client.flush(batcher)

    assert passes == 3  # queued, refused-and-requeued, accepted
    assert len(batches.created) == 2  # two batches: the first attempts, then the retries
    (record,) = log.records
    assert (record.attempts, record.outcome) == (2, "recovered")
    assert [k.value for k in record.kinds] == ["format"]
    # Both calls of the conversation are counted once each, on the pass that finished.
    assert usage.output_tokens == 480

    (retry,) = batches.created[1]
    feedback = retry["params"]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result" and feedback["is_error"] is True
    assert feedback["tool_use_id"] == "toolu_1"
    assert "findings.severity" in feedback["content"]


def test_a_report_that_stays_refused_raises_the_agents_own_error():
    def always_broken(cid: str, _params: dict[str, Any]) -> Any:
        broken = copy.deepcopy(_STRUCTURED_PAYLOAD)
        broken["findings"]["severity"]["detail"] = "not allowed here"
        return _succeeded(cid, _emit(broken))

    batches = _FakeBatches(always_broken)
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS.model_copy(update={"max_validation_retries": 2}))
    for _ in range(4):
        try:
            _diagnose(client, Usage(), RetryLog())
        except PendingRequest:
            client.flush(batcher)
        except ValidationError as exc:
            assert any("identical" in note for note in exc.__notes__)
            break
    else:
        pytest.fail("the retry loop never stopped")
    assert len(batches.created) == 2  # the identical-rejection rule stopped the third


def test_a_prose_reply_is_the_agents_error_not_a_pending_request():
    prose = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-sonnet-5",
        content=[TextBlock(type="text", text="I think it is a memory leak.")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    batches = _FakeBatches(lambda cid, _p: _succeeded(cid, prose))
    batcher, _ = _batcher(batches)
    client = DeferredClient(_SETTINGS)
    with pytest.raises(PendingRequest):
        _diagnose(client, Usage(), RetryLog())
    client.flush(batcher)
    with pytest.raises(Exception, match="did not call emit_incident_report"):
        _diagnose(client, Usage(), RetryLog())
