"""Day 17 - the validation-retry loop.

A refused report is re-requested with the error attached, in the same conversation, with
the emit tool forced again; the outcome is recorded. Under test is the loop's plumbing
against scripted fake clients: what the model is shown on a rejection, the two stopping
rules, the two rejection kinds told apart, what is never retried, the tokens every attempt
costs, the record, and the executor's gap carrying the loop's note when it gives up.
Every test here is offline and key-free.
"""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import ToolUseBlock
from pydantic import ValidationError

from aioc.agents import (
    EMIT_TOOL_NAME,
    GITHUB_EMIT_TOOL_NAME,
    DocsAgent,
    DocsAgentError,
    GitHubAgentError,
    IncidentAgent,
    IncidentAgentError,
    Rejection,
    RejectionKind,
    ReportRejected,
    RetryLog,
    RetryRecord,
    default_retry_log,
)
from aioc.agents._retry import (
    classify_rejection,
    emit_with_retry,
    render_feedback,
    render_rejection,
)
from aioc.contracts import AgentName, IncidentAgentResponse, ResponseStatus
from aioc.coordinator import Executor
from aioc.llm import LLMClient, LLMSettings, Usage
from tests import test_deployment_agent as dep
from tests import test_docs_agent as docs
from tests import test_github_agent as gh
from tests import test_incident_agent as inc
from tests.test_executor import _invocation, _plan

# ---------------------------------------------------------------------------- helpers


def _client(responses: list[Any]) -> tuple[LLMClient, inc._FakeMessages]:
    fake = inc._FakeAnthropic(responses)
    settings = LLMSettings(model="claude-sonnet-5", max_tokens=4096)
    return LLMClient(settings, client=fake), fake.messages  # type: ignore[arg-type]


def _incident(
    responses: list[Any], *, retries: int = 2, log: RetryLog | None = None
) -> tuple[IncidentAgent, inc._FakeMessages]:
    client, messages = _client(responses)
    return IncidentAgent(client, max_validation_retries=retries, retry_log=log), messages


def _broken_incident() -> dict[str, Any]:
    """A null root_cause value with its gap removed - the contract refuses it (format)."""
    return {**inc._STRUCTURED_PAYLOAD, "gaps": []}


def _tool_result_block(call: dict[str, Any]) -> dict[str, Any]:
    """The last user turn's single tool_result block of a scripted call."""
    last = call["messages"][-1]
    assert last["role"] == "user"
    (block,) = last["content"]
    assert block["type"] == "tool_result"
    return block


# ---------------------------------------------------------------- the re-request itself


def test_a_format_rejection_is_re_requested_with_the_error_attached_and_recovers():
    log = RetryLog()
    agent, messages = _incident(
        [inc._tool_use_message(_broken_incident()), inc._tool_use_message(inc._STRUCTURED_PAYLOAD)],
        log=log,
    )
    resp = agent.diagnose("q", context=inc._CONTEXT, request_id="req_1", invocation_id="inv_1")

    assert isinstance(resp, IncidentAgentResponse)
    assert len(messages.calls) == 2
    retry = messages.calls[1]
    # The same conversation: the prompt, the refused tool_use turn, then the error as
    # that call's tool_result - and the emit tool forced again.
    assert retry["messages"][0] == messages.calls[0]["messages"][0]
    assert retry["messages"][1]["role"] == "assistant"
    assert isinstance(retry["messages"][1]["content"][0], ToolUseBlock)
    block = _tool_result_block(retry)
    assert block["tool_use_id"] == "toolu_1"
    assert block["is_error"] is True
    assert "validation error" in block["content"]
    assert "findings.root_cause" in block["content"]  # the field the contract named
    assert "attempt(s) remain" in block["content"]
    assert "shape problem" in block["content"]  # format advice, not grounding advice
    assert retry["tool_choice"] == {"type": "tool", "name": EMIT_TOOL_NAME}
    assert retry["system"] == messages.calls[0]["system"]

    (record,) = log.records
    assert record.outcome == "recovered"
    assert record.attempts == 2
    assert record.kinds == (RejectionKind.FORMAT,)
    assert record.rejections[0].error_type == "ValidationError"
    assert record.request_id == "req_1" and record.invocation_id == "inv_1"
    assert record.accepted and record.stopped_by is None


def test_a_grounding_rejection_tells_the_model_an_honest_gap_is_the_answer():
    payload = docs._payload()
    payload["findings"]["claims"][0]["sources"][0]["quote"] = "They limited the cache."
    log = RetryLog()
    client, messages = _client([docs._tool_message(payload), docs._tool_message(docs._payload())])
    agent = DocsAgent(client, docs._FakeRetriever(docs._retrieval()), retry_log=log)

    resp = agent.answer(docs._QUERY, context=docs._CONTEXT)

    assert resp.agent is AgentName.DOCS
    block = _tool_result_block(messages.calls[1])
    assert "verbatim" in block["content"]  # the agent's own words for the refusal
    assert "honest gap is the correct answer" in block["content"]
    assert "blocks_field" in block["content"]
    assert "shape problem" not in block["content"]
    (record,) = log.records
    assert record.kinds == (RejectionKind.GROUNDING,)
    assert record.rejections[0].error_type == "DocsAgentError"
    assert record.outcome == "recovered"


def test_every_attempt_is_charged_to_the_usage_accumulator():
    agent, _ = _incident(
        [inc._tool_use_message(_broken_incident()), inc._tool_use_message(inc._STRUCTURED_PAYLOAD)]
    )
    usage = Usage()
    agent.diagnose("q", context=inc._CONTEXT, usage=usage)
    assert usage.input_tokens == 240 and usage.output_tokens == 480  # two calls of 120/240


def test_an_accepted_first_try_is_recorded_too():
    log = RetryLog()
    agent, _ = _incident([inc._tool_use_message(inc._STRUCTURED_PAYLOAD)], log=log)
    agent.diagnose("q", context=inc._CONTEXT)
    (record,) = log.records
    assert record.outcome == "accepted"
    assert record.attempts == 1 and record.rejections == ()


# -------------------------------------------------------------------- stopping rules


def test_the_cap_stops_the_loop_and_the_last_error_is_raised_with_a_note():
    log = RetryLog()
    agent, messages = _incident(
        [inc._tool_use_message(_broken_incident()) for _ in range(3)], retries=1, log=log
    )
    with pytest.raises(ValidationError) as excinfo:
        agent.diagnose("q", context=inc._CONTEXT)
    assert len(messages.calls) == 2  # the first attempt plus one retry
    (note,) = excinfo.value.__notes__
    assert "2 attempt(s)" in note and "cap of 1" in note
    (record,) = log.records
    assert record.outcome == "exhausted"
    assert record.stopped_by == "max_retries"
    assert record.attempts == 2


def test_an_identical_rejection_stops_the_loop_before_the_cap():
    """Two identical refusals in a row mean a third attempt would fail the same way - the
    refinement loop's identical-gap rule, applied here."""
    log = RetryLog()
    agent, messages = _incident(
        [inc._tool_use_message(_broken_incident()) for _ in range(4)], retries=3, log=log
    )
    with pytest.raises(ValidationError) as excinfo:
        agent.diagnose("q", context=inc._CONTEXT)
    assert len(messages.calls) == 2
    (record,) = log.records
    assert record.stopped_by == "identical_rejection"
    assert "identical" in excinfo.value.__notes__[0]


def test_a_different_rejection_keeps_the_loop_going():
    broken_status = {**inc._STRUCTURED_PAYLOAD, "status": "complete"}  # null value, complete
    log = RetryLog()
    agent, messages = _incident(
        [
            inc._tool_use_message(_broken_incident()),
            inc._tool_use_message({**_broken_incident(), "overall_confidence": 7}),
            inc._tool_use_message(broken_status),
        ],
        retries=2,
        log=log,
    )
    # The third payload is `complete` with a null value; the runtime settles that to
    # `partial` (Day 13), so it is accepted - three attempts, two different rejections.
    resp = agent.diagnose("q", context=inc._CONTEXT)
    assert resp.status is ResponseStatus.PARTIAL
    assert len(messages.calls) == 3
    (record,) = log.records
    assert record.outcome == "recovered" and record.attempts == 3
    assert [r.attempt for r in record.rejections] == [1, 2]


def test_zero_retries_disables_the_loop_but_still_records():
    log = RetryLog()
    agent, messages = _incident([inc._tool_use_message(_broken_incident())], retries=0, log=log)
    with pytest.raises(ValidationError):
        agent.diagnose("q", context=inc._CONTEXT)
    assert len(messages.calls) == 1
    (record,) = log.records
    assert record.outcome == "exhausted" and record.stopped_by == "max_retries"


def test_the_cap_comes_from_the_harness_settings_by_default():
    fake = inc._FakeAnthropic([inc._tool_use_message(_broken_incident()) for _ in range(3)])
    settings = LLMSettings(model="claude-sonnet-5", max_tokens=4096, max_validation_retries=1)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        IncidentAgent(client, retry_log=RetryLog()).diagnose("q", context=inc._CONTEXT)
    assert len(fake.messages.calls) == 2


def test_a_negative_cap_is_refused():
    with pytest.raises(ValueError, match=">= 0"):
        emit_with_retry(
            _client([])[0],
            agent="x",
            messages=[],
            system="s",
            tools=[],
            emit_tool="emit",
            build=lambda p: p,
            error_type=ReportRejected,
            usage=None,
            max_retries=-1,
            log=RetryLog(),
            request_id=None,
            invocation_id=None,
        )


# ------------------------------------------------------------------- never retried


def test_truncation_is_raised_at_once_and_not_retried():
    log = RetryLog()
    agent, messages = _incident(
        [inc._tool_use_message(inc._STRUCTURED_PAYLOAD, stop_reason="max_tokens")], log=log
    )
    with pytest.raises(IncidentAgentError, match="truncated at the max_tokens limit"):
        agent.diagnose("q", context=inc._CONTEXT)
    assert len(messages.calls) == 1
    assert log.records == []


def test_a_model_that_skips_the_forced_tool_is_not_retried():
    agent, messages = _incident([inc._message("prose instead")], log=RetryLog())
    with pytest.raises(IncidentAgentError, match="did not call"):
        agent.diagnose("q", context=inc._CONTEXT)
    assert len(messages.calls) == 1


def test_an_error_that_is_not_the_models_doing_is_not_retried():
    """A commit the tool returned without a timestamp is a tool fault: a retry cannot change
    it, so the loop raises at once and records nothing."""
    envelope = gh._pr_envelope()
    envelope["data"]["commits"][0]["authored_at"] = None
    log = RetryLog()
    agent, messages, _ = gh._agent([*gh._SCRIPT, gh._emit_message(gh._report())], envelope)
    agent._max_retries = 2
    agent._retry_log = log
    with pytest.raises(GitHubAgentError, match="carries no authored_at") as excinfo:
        agent.analyze(gh._QUERY, context=gh._CONTEXT)
    assert excinfo.value.kind is None
    assert len(messages.calls) == len(gh._SCRIPT) + 1
    assert log.records == []


def test_classification_of_the_three_error_families():
    assert classify_rejection(ValueError("x")) is None
    assert classify_rejection(ReportRejected("x")) is RejectionKind.GROUNDING
    assert classify_rejection(ReportRejected("x", kind=None)) is None
    assert (
        classify_rejection(GitHubAgentError("x", kind=RejectionKind.FORMAT)) is RejectionKind.FORMAT
    )
    with pytest.raises(ValidationError) as excinfo:
        IncidentAgentResponse.model_validate({})
    assert classify_rejection(excinfo.value) is RejectionKind.FORMAT


# ----------------------------------------------------- the tool-driven agents' two paths


def test_a_forced_emit_retry_answers_the_refused_tool_use_and_keeps_the_data_tools():
    report = gh._report()
    report["evidence"][0]["excerpt"] = "a paraphrase that appears nowhere"
    log = RetryLog()
    agent, messages, _ = gh._agent(
        [*gh._SCRIPT, gh._emit_message(report), gh._emit_message(gh._report())]
    )
    agent._max_retries = 2
    agent._retry_log = log
    resp = agent.analyze(gh._QUERY, context=gh._CONTEXT, request_id="req_9", invocation_id="inv_9")

    assert resp.evidence[0].tool_call_id == resp.tool_calls[0].id
    retry = messages.calls[-1]
    block = _tool_result_block(retry)
    assert block["tool_use_id"] == "toolu_emit" and block["is_error"] is True
    assert "does not appear verbatim" in block["content"]
    assert retry["tool_choice"] == {"type": "tool", "name": GITHUB_EMIT_TOOL_NAME}
    # The data tools stay defined so the earlier tool_use turns in the history stay valid,
    # and no data tool ran again: the wire calls are the loop's, not the retry's.
    assert {t["name"] for t in retry["tools"]} >= {"get_pull_request", GITHUB_EMIT_TOOL_NAME}
    assert len(resp.tool_calls) == 1
    (record,) = log.records
    assert record.agent == "github" and record.invocation_id == "inv_9"
    assert record.kinds == (RejectionKind.GROUNDING,) and record.outcome == "recovered"


def test_a_captured_emit_is_retried_with_a_plain_message_because_its_result_was_sent():
    """When the model emitted on its own during the tool loop, that tool_use already has
    its result in the history; the rejection is a new user turn, and the emit is forced."""
    report = dep._report()
    report["findings"]["service"] = "nobody-api"  # never asked of a tool
    log = RetryLog()
    agent, messages, _ = dep._agent(
        [
            *dep._SCRIPT[:-1],
            dep._emit_message(report),  # emitted during the loop, captured
            dep._done_message(),
            dep._emit_message(dep._report()),  # the forced retry
        ]
    )
    agent._max_retries = 2
    agent._retry_log = log
    resp = agent.assess(dep._QUERY, context=dep._CONTEXT)

    assert resp.findings.service == dep._report()["findings"]["service"]
    retry = messages.calls[-1]
    last = retry["messages"][-1]
    assert last["role"] == "user" and isinstance(last["content"], str)
    assert "never asked of a tool" in last["content"]
    assert retry["tool_choice"]["name"] == dep.DEPLOYMENT_EMIT_TOOL_NAME
    (record,) = log.records
    assert record.agent == "deployment" and record.outcome == "recovered"
    assert record.rejections[0].error_type == "DeploymentAgentError"


def test_the_grounding_error_still_carries_the_rejected_report():
    report = gh._report()
    report["evidence"][0]["excerpt"] = "a paraphrase that appears nowhere"
    agent, _, _ = gh._agent([*gh._SCRIPT, gh._emit_message(report), gh._emit_message(report)])
    agent._max_retries = 1
    agent._retry_log = RetryLog()
    with pytest.raises(GitHubAgentError) as excinfo:
        agent.analyze(gh._QUERY, context=gh._CONTEXT)
    assert excinfo.value.report is not None
    assert excinfo.value.kind is RejectionKind.GROUNDING


# ---------------------------------------------------------------- rendering and the log


def test_pydantic_errors_are_rendered_one_per_line_without_urls_or_inputs():
    with pytest.raises(ValidationError) as excinfo:
        IncidentAgentResponse.model_validate({**_broken_incident(), "overall_confidence": 7})
    text = render_rejection(excinfo.value)
    assert text.splitlines()[0].endswith("validation error(s) against the contract:")
    assert "- overall_confidence:" in text
    assert "https://" not in text and "input_value" not in text
    assert render_rejection(DocsAgentError("plain words")) == "plain words"


def test_the_feedback_names_the_tool_the_remaining_attempts_and_the_kind():
    rejection = Rejection(attempt=1, kind=RejectionKind.GROUNDING, error_type="E", message="why")
    text = render_feedback("emit_x", rejection, remaining=2)
    assert "`emit_x`" in text and "2 attempt(s) remain" in text and "why" in text
    assert "honest gap" in text
    formatted = Rejection(attempt=1, kind=RejectionKind.FORMAT, error_type="E", message="why")
    assert "shape problem" in render_feedback("emit_x", formatted, remaining=1)


def test_identical_rejections_ignore_whitespace_only_differences():
    a = Rejection(attempt=1, kind=RejectionKind.FORMAT, error_type="E", message="x  y\nz")
    b = Rejection(attempt=2, kind=RejectionKind.FORMAT, error_type="E", message="x y z")
    c = Rejection(attempt=2, kind=RejectionKind.GROUNDING, error_type="E", message="x y z")
    assert a.same_as(b) and not a.same_as(c)


def test_the_log_summary_measures_recovery_by_kind():
    log = RetryLog()

    def rec(*, kinds: tuple[RejectionKind, ...], accepted: bool, stopped: str | None) -> None:
        log.record(
            RetryRecord(
                agent="incident",
                request_id="req",
                invocation_id="inv",
                attempts=len(kinds) + (1 if accepted else 0),
                rejections=tuple(
                    Rejection(attempt=i + 1, kind=k, error_type="E", message="m")
                    for i, k in enumerate(kinds)
                ),
                accepted=accepted,
                stopped_by=stopped,
            )
        )

    rec(kinds=(), accepted=True, stopped=None)
    rec(kinds=(RejectionKind.FORMAT,), accepted=True, stopped=None)
    rec(
        kinds=(RejectionKind.GROUNDING, RejectionKind.GROUNDING),
        accepted=False,
        stopped="identical_rejection",
    )
    rec(kinds=(RejectionKind.FORMAT, RejectionKind.GROUNDING), accepted=True, stopped=None)

    s = log.summary()
    assert s["emits"] == 4
    assert s["accepted_first_try"] == 1 and s["recovered"] == 2 and s["exhausted"] == 1
    assert s["retries"] == 0 + 1 + 1 + 2
    assert s["by_kind"]["format"] == {"rejected": 2, "recovered": 2, "exhausted": 0}
    assert s["by_kind"]["grounding"] == {"rejected": 2, "recovered": 1, "exhausted": 1}
    assert s["stopped_by"] == {"identical_rejection": 1}
    rendered = log.render_summary()
    assert "4 report(s) emitted" in rendered and "grounding: 2 report(s) rejected" in rendered
    assert log.for_invocation("inv") == log.records
    assert log.records[1].to_dict()["outcome"] == "recovered"
    log.clear()
    assert log.render_summary().endswith("no reports emitted")


def test_agents_share_the_process_default_log_unless_given_one():
    shared = default_retry_log()
    before = len(shared.records)
    client, _ = _client([inc._tool_use_message(inc._STRUCTURED_PAYLOAD)])
    IncidentAgent(client).diagnose("q", context=inc._CONTEXT)
    assert len(shared.records) == before + 1
    assert default_retry_log() is shared


# ------------------------------------------------------------- the executor's gap text


class _GivesUp:
    """A runner whose agent's loop gave up: the last error carries the loop's note."""

    def run(self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage):
        exc = ValidationError.from_exception_data("IncidentAgentResponse", [])
        exc.add_note("validation-retry loop: 3 attempt(s), rejected 3 time(s) [format]; the cap")
        raise exc


def test_the_executors_gap_keeps_the_loops_note_when_an_agent_gives_up():
    plan = _plan([_invocation("incident")])
    resp = Executor(runners={AgentName.INCIDENT: _GivesUp()}, max_refinement_rounds=0).execute(
        plan, "q"
    )
    (gap,) = resp.unresolved_gaps
    assert "ValidationError" in gap.description
    assert "validation-retry loop: 3 attempt(s)" in gap.description


def test_a_long_message_gives_the_note_the_room():
    from aioc.coordinator.executor import _error_summary

    exc = RuntimeError("x" * 700)
    exc.add_note("the note")
    text = _error_summary(exc)
    assert text.endswith("... | the note") and len(text) <= 600
