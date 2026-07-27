"""Unit tests for the Claude API harness (Day 2).

The underlying Anthropic client is injected, so these drive the ``tool_use`` loop with a
scripted fake - deterministic, no network, no API key. Real `TextBlock` / `ToolUseBlock`
instances are used so the loop's ``isinstance`` dispatch runs the same path it would live.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from aioc.llm import (
    LLMClient,
    LLMSettings,
    ToolLoopLimitError,
    ToolResult,
    ToolSpec,
)

# --------------------------------------------------------------------------- fakes


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def _tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=tool_input)


def _message(
    stop_reason: str, content: list[Any], *, in_tokens: int = 10, out_tokens: int = 5
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


class _FakeStream:
    def __init__(self, chunks: list[str]) -> None:
        self.text_stream = iter(chunks)

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeMessages:
    def __init__(self, responses: list[Any], stream_chunks: list[str] | None = None) -> None:
        self._responses = list(responses)
        self._stream_chunks = stream_chunks or []
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        # Snapshot the messages list: the loop passes its live convo by reference and keeps
        # mutating it, so record a shallow copy to capture the call-time state.
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = list(snapshot["messages"])
        self.calls.append(snapshot)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self._stream_chunks)


class _FakeAnthropic:
    def __init__(self, responses: list[Any], stream_chunks: list[str] | None = None) -> None:
        self.messages = _FakeMessages(responses, stream_chunks)


def _client(responses: list[Any], stream_chunks: list[str] | None = None) -> LLMClient:
    fake = _FakeAnthropic(responses, stream_chunks)
    # effort stays None so no output_config is sent; a fixed model avoids env dependence.
    settings = LLMSettings(model="claude-opus-5", max_tokens=1024)
    return LLMClient(settings, client=fake)  # type: ignore[arg-type]


def _health_tool(calls: list[dict[str, Any]]) -> ToolSpec:
    def handler(args: dict[str, Any]) -> str:
        calls.append(args)
        return f"{args['service']}: healthy, error_rate=0.1%"

    return ToolSpec(
        name="get_service_health",
        description="Return the current health of a named service.",
        input_schema={
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
        handler=handler,
    )


# --------------------------------------------------------------------------- tool loop


def test_single_tool_round_trip():
    handler_calls: list[dict[str, Any]] = []
    tool = _health_tool(handler_calls)
    client = _client(
        [
            _message(
                "tool_use",
                [_tool_use("toolu_1", "get_service_health", {"service": "checkout-api"})],
            ),
            _message("end_turn", [_text("checkout-api is healthy (0.1% errors).")]),
        ]
    )

    result = client.run_tool_loop(
        messages=[{"role": "user", "content": "Is checkout-api healthy?"}],
        tools=[tool],
    )

    # The handler actually ran with the parsed input.
    assert handler_calls == [{"service": "checkout-api"}]
    # Two model calls: one asking for the tool, one answering.
    assert result.rounds == 2
    assert result.stop_reason == "end_turn"
    assert "healthy" in result.text
    # One recorded tool call, marked ok, with the tool output captured.
    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.id == "toolu_1"
    assert record.name == "get_service_health"
    assert record.arguments == {"service": "checkout-api"}
    assert record.ok is True
    assert "error_rate" in record.output
    # Usage summed across both model calls.
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 10


def test_tool_result_is_fed_back_to_the_model():
    tool = _health_tool([])
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "get_service_health", {"service": "api"})]),
            _message("end_turn", [_text("ok")]),
        ]
    )
    client.run_tool_loop(messages=[{"role": "user", "content": "check"}], tools=[tool])

    # The second create() call must carry the tool_result back as a user turn.
    second_call = client._client.messages.calls[1]  # type: ignore[attr-defined]
    last_msg = second_call["messages"][-1]
    assert last_msg["role"] == "user"
    tool_result = last_msg["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False


def test_parallel_tool_calls_in_one_turn():
    tool = _health_tool([])
    client = _client(
        [
            _message(
                "tool_use",
                [
                    _tool_use("toolu_1", "get_service_health", {"service": "a"}),
                    _tool_use("toolu_2", "get_service_health", {"service": "b"}),
                ],
            ),
            _message("end_turn", [_text("both fine")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "a and b?"}], tools=[tool])

    assert [r.id for r in result.tool_calls] == ["toolu_1", "toolu_2"]
    # Both results come back in a single user message (splitting them trains Claude to stop
    # making parallel calls).
    second_call = client._client.messages.calls[1]  # type: ignore[attr-defined]
    results_msg = second_call["messages"][-1]
    assert len(results_msg["content"]) == 2


def test_handler_error_becomes_is_error_result():
    def handler(_args: dict[str, Any]) -> ToolResult:
        return ToolResult(content="prometheus timeout", is_error=True)

    tool = ToolSpec(
        name="get_service_health",
        description="d",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "get_service_health", {})]),
            _message("end_turn", [_text("couldn't reach metrics")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    assert result.tool_calls[0].ok is False
    second_call = client._client.messages.calls[1]  # type: ignore[attr-defined]
    assert second_call["messages"][-1]["content"][0]["is_error"] is True


def test_handler_exception_is_caught_not_raised():
    def handler(_args: dict[str, Any]) -> str:
        raise ValueError("boom")

    tool = ToolSpec(
        name="get_service_health",
        description="d",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "get_service_health", {})]),
            _message("end_turn", [_text("recovered")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    record = result.tool_calls[0]
    assert record.ok is False
    assert "ValueError" in record.output and "boom" in record.output


def test_unknown_tool_is_reported_not_fatal():
    tool = _health_tool([])
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "nonexistent_tool", {})]),
            _message("end_turn", [_text("no such tool")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    record = result.tool_calls[0]
    assert record.ok is False
    assert "Unknown tool" in record.output


def test_no_tool_needed_returns_immediately():
    tool = _health_tool([])
    client = _client([_message("end_turn", [_text("hello, I need no tools")])])

    result = client.run_tool_loop(messages=[{"role": "user", "content": "hi"}], tools=[tool])

    assert result.rounds == 1
    assert result.tool_calls == []
    assert result.text == "hello, I need no tools"


def test_loop_limit_raises():
    tool = _health_tool([])
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "get_service_health", {"service": "a"})]),
            _message("tool_use", [_tool_use("toolu_2", "get_service_health", {"service": "a"})]),
        ]
    )
    with pytest.raises(ToolLoopLimitError):
        client.run_tool_loop(
            messages=[{"role": "user", "content": "x"}], tools=[tool], max_iterations=2
        )


def test_pause_turn_resumes():
    tool = _health_tool([])
    client = _client(
        [
            _message("pause_turn", [_text("thinking...")]),
            _message("end_turn", [_text("done")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    assert result.rounds == 2
    assert result.text == "done"


def test_record_timing_and_started_at():
    tool = _health_tool([])
    client = _client(
        [
            _message("tool_use", [_tool_use("toolu_1", "get_service_health", {"service": "a"})]),
            _message("end_turn", [_text("ok")]),
        ]
    )
    result = client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    record = result.tool_calls[0]
    assert isinstance(record.started_at, datetime)
    assert record.started_at.tzinfo is not None
    assert record.duration_ms >= 0


# --------------------------------------------------------------------------- streaming


def test_stream_text_yields_chunks():
    client = _client([], stream_chunks=["Hel", "lo, ", "world"])

    chunks = list(client.stream_text(messages=[{"role": "user", "content": "hi"}]))

    assert chunks == ["Hel", "lo, ", "world"]
    assert "".join(chunks) == "Hello, world"


# --------------------------------------------------------------------------- effort config


def test_effort_omitted_when_unset():
    tool = _health_tool([])
    client = _client([_message("end_turn", [_text("ok")])])
    client.run_tool_loop(messages=[{"role": "user", "content": "x"}], tools=[tool])

    # No output_config when effort is None - keeps the harness safe on Haiku 4.5.
    assert "output_config" not in client._client.messages.calls[0]  # type: ignore[attr-defined]


def test_effort_sent_when_set():
    client = LLMClient(
        LLMSettings(model="claude-opus-5", effort="low"),
        client=_FakeAnthropic([_message("end_turn", [_text("ok")])]),  # type: ignore[arg-type]
    )
    client.complete(messages=[{"role": "user", "content": "x"}])

    assert client._client.messages.calls[0]["output_config"] == {"effort": "low"}  # type: ignore[attr-defined]
