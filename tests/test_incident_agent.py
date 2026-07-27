"""Unit tests for the Incident agent skeleton (Day 3).

Same pattern as the harness tests: the Anthropic client is a scripted fake injected through
`LLMClient`, so the tests are deterministic, offline, and key-free. What is under test is the
agent's plumbing - explicit context passing, the system prompt, prose extraction, accounting -
not the model's judgment (that is the Day 19 eval harness's job).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock

from aioc.agents import INCIDENT_SYSTEM_PROMPT, IncidentAgent
from aioc.llm import LLMClient, LLMSettings

# --------------------------------------------------------------------------- fakes


def _message(text: str, *, in_tokens: int = 40, out_tokens: int = 25) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[TextBlock(type="text", text=text)],
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


def _agent(responses: list[Any]) -> tuple[IncidentAgent, _FakeMessages]:
    fake = _FakeAnthropic(responses)
    settings = LLMSettings(model="claude-opus-5", max_tokens=1024)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    return IncidentAgent(client), fake.messages


_CONTEXT = (
    "Service: payments-api. Since 14:02 UTC error_rate rose from 0.1% to 7.4%; "
    "p99 latency 2100ms. No deploys in the window."
)


# --------------------------------------------------------------------------- behaviour


def test_returns_prose_and_accounting():
    agent, _ = _agent([_message("**Summary** - payments-api is degraded.")])
    result = agent.investigate("Why is payments-api slow?", context=_CONTEXT)
    assert result.text == "**Summary** - payments-api is degraded."
    assert result.stop_reason == "end_turn"
    assert result.model == "claude-opus-5"
    assert (result.input_tokens, result.output_tokens) == (40, 25)


def test_context_is_passed_explicitly_in_the_prompt():
    agent, messages = _agent([_message("ok")])
    agent.investigate("What broke?", context=_CONTEXT)
    call = messages.calls[0]
    prompt = call["messages"][0]["content"]
    # The literal context block appears in the prompt - nothing is assumed inherited.
    assert _CONTEXT in prompt
    assert "<context>" in prompt and "</context>" in prompt
    assert "What broke?" in prompt


def test_system_prompt_carries_the_graded_behaviours():
    agent, messages = _agent([_message("ok")])
    agent.investigate("What broke?", context=_CONTEXT)
    system = messages.calls[0]["system"]
    assert system == INCIDENT_SYSTEM_PROMPT
    # The three behaviours Day 3 is graded on: SRE persona, evidence citation, confidence.
    assert "Site" in system and "Reliability" in system
    assert "evidence" in system.lower()
    assert "0.90-1.00" in system  # the CONTRACTS.md sec 2.1 band table, verbatim
    assert "below 0.25" in system.lower()


def test_empty_context_is_rejected():
    agent, _ = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.investigate("What broke?", context="   ")


def test_empty_query_is_rejected():
    agent, _ = _agent([])
    with pytest.raises(ValueError, match="query must be non-empty"):
        agent.investigate("", context=_CONTEXT)


def test_multiple_text_blocks_are_concatenated():
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            TextBlock(type="text", text="part one. "),
            TextBlock(type="text", text="part two."),
        ],
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    agent, _ = _agent([resp])
    result = agent.investigate("q", context=_CONTEXT)
    assert result.text == "part one. part two."
