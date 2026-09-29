"""Day 19 cost levers, the caching half: request shaping, the cache counters, and the price.

Offline like every harness test - a scripted fake stands in for the Anthropic client, so
what is asserted is what *would go on the wire* and how what comes back is counted. That
a real request is actually served from the cache is a live fact (`scripts/run_evals.py`
reports the counters); what these tests hold is everything that fact depends on:

- the marker sits on the system block, where it covers the tool schemas as well;
- the prefix is byte-identical between two requests that differ in everything a caller
  can vary - the one property caching cannot work without, and the one that breaks
  silently when somebody interpolates a timestamp into a prompt;
- the three-part prompt count is put back together, so a token total recorded with
  caching on means what one recorded before it meant.
"""

from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from aioc.agents import DocsAgent, IncidentAgent
from aioc.agents.deployment import DEPLOYMENT_SYSTEM_PROMPT
from aioc.agents.docs import DOCS_STRUCTURED_SYSTEM_PROMPT
from aioc.agents.github import GITHUB_SYSTEM_PROMPT
from aioc.agents.incident import INCIDENT_STRUCTURED_SYSTEM_PROMPT, INCIDENT_SYSTEM_PROMPT
from aioc.contracts import AgentName, IncidentAgentResponse
from aioc.coordinator import Executor
from aioc.coordinator.planner import SELECTION_SYSTEM_PROMPT
from aioc.coordinator.synthesis import SYNTHESIS_SYSTEM_PROMPT
from aioc.llm import (
    BATCH_DISCOUNT,
    CACHE_WRITE_MULTIPLIER,
    PRICES,
    LLMClient,
    LLMSettings,
    ToolSpec,
    Usage,
    price,
    price_uncached,
    system_text,
)
from aioc.llm.pricing import price_key
from tests.test_docs_agent import _FakeRetriever, _retrieval
from tests.test_docs_agent import _payload as _docs_payload
from tests.test_docs_agent import _tool_message as _docs_message
from tests.test_executor import _incident_response, _invocation, _plan
from tests.test_incident_agent import _STRUCTURED_PAYLOAD, _tool_use_message

# ------------------------------------------------------------------------------- fakes


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


def _client(responses: list[Any], **settings: Any) -> tuple[LLMClient, _FakeMessages]:
    fake = _FakeAnthropic(responses)
    config = LLMSettings(model="claude-sonnet-5", max_tokens=1024, **settings)
    return LLMClient(config, client=fake), fake.messages  # type: ignore[arg-type]


def _text(text: str = "ok", **usage: int) -> SimpleNamespace:
    counts = {"input_tokens": 10, "output_tokens": 5, **usage}
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[TextBlock(type="text", text=text)],
        model="claude-sonnet-5",
        usage=SimpleNamespace(**counts),
    )


def _tool_call(name: str, arguments: dict[str, Any], **usage: int) -> SimpleNamespace:
    counts = {"input_tokens": 10, "output_tokens": 5, **usage}
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=arguments)],
        model="claude-sonnet-5",
        usage=SimpleNamespace(**counts),
    )


_ECHO = ToolSpec(
    name="echo",
    description="Echo the input back.",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    handler=lambda args: str(args.get("text", "")),
)

_MESSAGES: Any = [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------- request shaping


def test_the_system_prompt_goes_as_one_block_carrying_the_cache_marker():
    client, messages = _client([_text()], prompt_caching=True)
    client.complete(messages=_MESSAGES, system="You are an SRE.")
    assert messages.calls[0]["system"] == [
        {"type": "text", "text": "You are an SRE.", "cache_control": {"type": "ephemeral"}}
    ]


def test_with_caching_off_the_request_is_the_one_sent_before_day_19():
    client, messages = _client([_text()], prompt_caching=False)
    client.complete(messages=_MESSAGES, system="You are an SRE.", tools=[_ECHO])
    call = messages.calls[0]
    assert call["system"] == "You are an SRE."
    assert "cache_control" not in call
    assert "cache_control" not in json.dumps(call["tools"])


def test_the_marker_is_on_the_system_block_and_never_on_a_tool():
    # Render order is tools -> system -> messages: one marker on the system block caches
    # the tool schemas with it. A second marker on a tool would spend a breakpoint on a
    # prefix the first already covers.
    client, messages = _client([_text()], prompt_caching=True)
    client.complete(messages=_MESSAGES, system="s", tools=[_ECHO])
    call = messages.calls[0]
    assert "cache_control" in call["system"][-1]
    assert "cache_control" not in json.dumps(call["tools"])


def test_the_longer_ttl_is_stated_and_the_default_is_left_implicit():
    default, d_calls = _client([_text()], prompt_caching=True)
    default.complete(messages=_MESSAGES, system="s")
    assert d_calls.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}

    hour, h_calls = _client([_text()], prompt_caching=True, prompt_cache_ttl="1h")
    hour.complete(messages=_MESSAGES, system="s")
    assert h_calls.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_a_single_call_does_not_cache_its_tail():
    # The message is the part that differs every time: a write nobody reads back is a
    # 25% surcharge on it.
    client, messages = _client([_text()], prompt_caching=True)
    client.complete(messages=_MESSAGES, system="s")
    assert "cache_control" not in messages.calls[0]


def test_the_tool_loop_caches_its_growing_tail_on_every_round():
    client, messages = _client(
        [_tool_call("echo", {"text": "a"}), _tool_call("echo", {"text": "b"}), _text("done")],
        prompt_caching=True,
    )
    client.run_tool_loop(messages=_MESSAGES, tools=[_ECHO], system="s")
    assert len(messages.calls) == 3
    for call in messages.calls:
        assert call["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" in call["system"][-1]


def test_the_tool_loop_with_caching_off_sends_no_marker():
    client, messages = _client(
        [_tool_call("echo", {"text": "a"}), _text("done")], prompt_caching=False
    )
    client.run_tool_loop(messages=_MESSAGES, tools=[_ECHO], system="s")
    assert all("cache_control" not in call for call in messages.calls)


def test_an_omitted_optional_is_left_out_of_the_request():
    client, messages = _client([_text()], prompt_caching=True)
    client.complete(messages=_MESSAGES)
    assert set(messages.calls[0]) == {"model", "max_tokens", "messages"}


def test_request_params_are_what_complete_sends():
    # The batch path submits `request_params`; it must be the realtime request exactly.
    client, messages = _client([_text()], prompt_caching=True)
    kwargs: dict[str, Any] = {
        "messages": _MESSAGES,
        "system": "s",
        "tools": [_ECHO],
        "tool_choice": {"type": "tool", "name": "echo"},
    }
    params = client.request_params(**kwargs)
    client.complete(**kwargs)
    assert messages.calls[0] == params


def test_system_text_reads_either_wire_shape():
    assert system_text(None) is None
    assert system_text("plain") == "plain"
    assert system_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


# ------------------------------------------------------------- the prefix does not move


def _prefix(call: dict[str, Any]) -> str:
    """Everything the cache key is built from before the first message."""
    return json.dumps({"tools": call.get("tools"), "system": call["system"]}, sort_keys=True)


def test_the_incident_agents_prefix_is_identical_across_different_requests():
    client, messages = _client(
        [_tool_use_message(_STRUCTURED_PAYLOAD), _tool_use_message(_STRUCTURED_PAYLOAD)],
        prompt_caching=True,
    )
    agent = IncidentAgent(client, max_validation_retries=0)
    agent.diagnose("Why is checkout slow?", context="checkout-api p99 rose at 14:02.")
    agent.diagnose(
        "What broke payments?",
        context="payments-api 5xx rose at 09:15.",
        request_id="req_other",
        invocation_id="inv_other",
    )
    first, second = messages.calls
    assert _prefix(first) == _prefix(second)
    assert first["messages"] != second["messages"]


def test_the_docs_agents_prefix_is_identical_across_different_requests():
    client, messages = _client(
        [_docs_message(_docs_payload()), _docs_message(_docs_payload())], prompt_caching=True
    )
    agent = DocsAgent(client, _FakeRetriever(_retrieval()), max_validation_retries=0)
    agent.answer("How was the memory leak fixed?", context="Repeat memory pressure.")
    agent.answer("Is there an alert threshold?", context="A different request entirely.")
    first, second = messages.calls
    assert _prefix(first) == _prefix(second)


def test_no_system_prompt_carries_anything_that_varies_by_request():
    # The silent invalidators, looked for where they would do the damage. A date or an id
    # in a system prompt does not fail a request; it makes every request a cache write.
    prompts = {
        "incident": INCIDENT_SYSTEM_PROMPT,
        "incident structured": INCIDENT_STRUCTURED_SYSTEM_PROMPT,
        "docs": DOCS_STRUCTURED_SYSTEM_PROMPT,
        "github": GITHUB_SYSTEM_PROMPT,
        "deployment": DEPLOYMENT_SYSTEM_PROMPT,
        "planner": SELECTION_SYSTEM_PROMPT,
        "synthesis": SYNTHESIS_SYSTEM_PROMPT,
    }
    for name, prompt in prompts.items():
        assert isinstance(prompt, str) and prompt, name
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", prompt), f"{name}: a date in the prompt"
        assert not re.search(r"\b(req|inv|tc)_[0-9a-f]{6,}\b", prompt), f"{name}: an id"


# ------------------------------------------------------------------------ the counters


def test_the_three_part_prompt_count_is_put_back_together():
    usage = Usage()
    usage.record(
        SimpleNamespace(
            input_tokens=120,
            output_tokens=300,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=4_000,
        )
    )
    assert usage.input_tokens == 4_120
    assert usage.output_tokens == 300
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (4_000, 0)
    assert usage.uncached_input_tokens == 120


def test_a_response_that_reports_no_cache_counters_leaves_them_unmeasured():
    # The scripted fakes, and any run recorded before Day 19: null is not zero.
    usage = Usage()
    usage.record(SimpleNamespace(input_tokens=40, output_tokens=25))
    assert (usage.input_tokens, usage.output_tokens) == (40, 25)
    assert usage.cache_read_tokens is None and usage.cache_write_tokens is None
    assert usage.uncached_input_tokens == 40


def test_a_null_counter_from_the_api_is_not_counted():
    usage = Usage()
    usage.record(
        SimpleNamespace(
            input_tokens=40,
            output_tokens=25,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        )
    )
    assert usage.cache_read_tokens is None and usage.cache_write_tokens is None


def test_folding_accumulators_keeps_the_counters_and_their_nulls():
    total = Usage()
    total.add(Usage(input_tokens=10, output_tokens=1))
    assert total.cache_read_tokens is None
    total.add(Usage(input_tokens=100, output_tokens=2, cache_read_tokens=80, cache_write_tokens=0))
    total.add(Usage(input_tokens=50, output_tokens=3, cache_read_tokens=5, cache_write_tokens=40))
    assert (total.input_tokens, total.output_tokens) == (160, 6)
    assert (total.cache_read_tokens, total.cache_write_tokens) == (85, 40)
    assert total.as_record() == {"in": 160, "out": 6, "cache_read": 85, "cache_write": 40}


def test_the_tool_loop_counts_a_cache_write_then_a_cache_read():
    client, _ = _client(
        [
            _tool_call(
                "echo",
                {"text": "a"},
                input_tokens=30,
                cache_creation_input_tokens=5_000,
                cache_read_input_tokens=0,
            ),
            _text(
                "done",
                input_tokens=60,
                cache_creation_input_tokens=90,
                cache_read_input_tokens=5_000,
            ),
        ],
        prompt_caching=True,
    )
    usage = client.run_tool_loop(messages=_MESSAGES, tools=[_ECHO], system="s").usage
    assert usage.input_tokens == 30 + 5_000 + 60 + 90 + 5_000
    assert usage.cache_write_tokens == 5_090
    assert usage.cache_read_tokens == 5_000


def test_an_agents_accumulator_receives_the_cache_counters():
    message = copy.copy(_tool_use_message(_STRUCTURED_PAYLOAD))
    message.usage = SimpleNamespace(
        input_tokens=200,
        output_tokens=900,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=4_500,
    )
    client, _ = _client([message], prompt_caching=True)
    usage = Usage()
    IncidentAgent(client, max_validation_retries=0).diagnose("q", context="ctx", usage=usage)
    assert (usage.input_tokens, usage.cache_read_tokens) == (4_700, 4_500)


class _CachingRunner:
    """An agent whose one model call read most of its prompt from the cache."""

    def __init__(self, reported: SimpleNamespace) -> None:
        self._reported = reported

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> IncidentAgentResponse:
        usage.record(self._reported)
        return _incident_response(request_id, invocation_id)


def test_the_response_cost_carries_the_cache_counters():
    runner = _CachingRunner(
        SimpleNamespace(
            input_tokens=150,
            output_tokens=700,
            cache_creation_input_tokens=50,
            cache_read_input_tokens=4_800,
        )
    )
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")
    assert resp.cost.input_tokens == 5_000
    assert resp.cost.cache_read_tokens == 4_800
    assert resp.cost.cache_write_tokens == 50
    assert resp.cost.usd is None  # a response knows its tokens, not the price table


def test_the_response_cost_is_null_on_a_counter_nothing_reported():
    # The contract's null: not measured. A run before Day 19, or a scripted fake.
    runner = _CachingRunner(SimpleNamespace(input_tokens=120, output_tokens=240))
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")
    assert resp.cost.input_tokens == 120
    assert resp.cost.cache_read_tokens is None and resp.cost.cache_write_tokens is None


# --------------------------------------------------------------------------- the price


def test_cache_reads_and_writes_are_priced_at_their_own_rates():
    sonnet = PRICES["claude-sonnet-5"]
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=800_000,
        cache_write_tokens=100_000,
    )
    expected = (
        100_000 * sonnet.input
        + 100_000 * sonnet.input * CACHE_WRITE_MULTIPLIER["5m"]
        + 800_000 * sonnet.cache_read
        + 100_000 * sonnet.output
    ) / 1e6
    assert price("claude-sonnet-5", usage) == pytest.approx(expected)
    assert price_uncached("claude-sonnet-5", usage) == pytest.approx(
        (1_000_000 * sonnet.input + 100_000 * sonnet.output) / 1e6
    )


def test_the_hour_long_cache_costs_twice_the_input_rate_to_write():
    usage = Usage(input_tokens=1_000_000, cache_read_tokens=0, cache_write_tokens=1_000_000)
    short = price("claude-sonnet-5", usage, cache_ttl="5m")
    long = price("claude-sonnet-5", usage, cache_ttl="1h")
    assert short == pytest.approx(2.00 * 1.25)
    assert long == pytest.approx(2.00 * 2.0)


def test_a_write_that_is_never_read_costs_more_than_no_cache_at_all():
    # The reason a single call does not cache its tail, as arithmetic.
    written = Usage(input_tokens=10_000, cache_read_tokens=0, cache_write_tokens=10_000)
    assert price("claude-sonnet-5", written) > price_uncached("claude-sonnet-5", written)  # type: ignore[operator]


def test_a_batch_is_half_price_on_every_token():
    usage = Usage(
        input_tokens=500_000, output_tokens=50_000, cache_read_tokens=300_000, cache_write_tokens=0
    )
    realtime = price("claude-sonnet-5", usage)
    assert realtime is not None
    assert price("claude-sonnet-5", usage, batch=True) == pytest.approx(realtime * BATCH_DISCOUNT)


def test_a_dated_model_id_is_priced_and_the_longest_match_wins():
    assert price_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert price_key("claude-sonnet-5") == "claude-sonnet-5"
    # `claude-opus-5-5` starts with `claude-opus-5` and is a different price.
    assert price_key("claude-opus-5-5") == "claude-opus-5-5"
    assert PRICES["claude-opus-5-5"] != PRICES["claude-opus-5"]


def test_an_unlisted_model_is_unpriced_rather_than_guessed():
    usage = Usage(input_tokens=10, output_tokens=5)
    assert price_key("some-other-model") is None
    assert price("some-other-model", usage) is None
    assert price_uncached("some-other-model", usage) is None
