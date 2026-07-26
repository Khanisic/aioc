"""The Claude API harness (Day 2): messages, streaming, and a manual ``tool_use`` loop.

Why a manual loop rather than the SDK's ``tool_runner`` helper: the downstream phases need
control the runner doesn't cleanly expose - per-call records for the Day 21 token-reduction
measurement, ``tool_choice``-forced structured output on Day 4, and the coordinator's
refinement loop. A hand-written loop is also more legible as CCA-F Domain 4 evidence.

The underlying ``anthropic.Anthropic`` client is injected, so tests drive the loop with a
scripted fake and no network or API key.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import anthropic
from anthropic.types import Message, MessageParam, ToolUseBlock

from .config import LLMSettings
from .tool_use import (
    ToolCallRecord,
    ToolLoopLimitError,
    ToolLoopResult,
    ToolResult,
    ToolSpec,
    Usage,
)

# The SDK's "omit this param" sentinel. Typed Any so an omitted optional (system, tools,
# tool_choice) satisfies the API's strict per-param TypedDict unions without a cast each.
_OMIT: Any = anthropic.NOT_GIVEN


class LLMClient:
    """A thin, typed wrapper over the Anthropic Messages API for the Reasoning Layer."""

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.settings = settings or LLMSettings()
        if client is not None:
            self._client = client
        else:
            key = self.settings.anthropic_api_key
            self._client = anthropic.Anthropic(
                api_key=key.get_secret_value() if key is not None else None,
                timeout=self.settings.timeout_seconds,
            )

    # -- shared request shaping ------------------------------------------------

    def _output_config(self) -> dict[str, Any]:
        """Add ``output_config`` only when effort is set - Haiku 4.5 rejects it (see config)."""
        if self.settings.effort is None:
            return {}
        return {"output_config": {"effort": self.settings.effort}}

    # -- single-shot messages --------------------------------------------------

    def complete(
        self,
        *,
        messages: Sequence[MessageParam],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Message:
        """One request, one response. No loop - the caller inspects ``stop_reason`` itself."""
        resp = self._client.messages.create(
            model=model or self.settings.model,
            max_tokens=max_tokens or self.settings.max_tokens,
            system=system if system is not None else _OMIT,
            messages=list(messages),
            tools=[t.to_api() for t in tools] if tools else _OMIT,
            tool_choice=cast("Any", tool_choice) if tool_choice is not None else _OMIT,
            **self._output_config(),
        )
        # cast: the Any-typed omit sentinel widens create()'s return to Any; without tools
        # this is always a non-streamed Message.
        return cast("Message", resp)

    # -- streaming -------------------------------------------------------------

    def stream_text(
        self,
        *,
        messages: Sequence[MessageParam],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """Yield text deltas as they arrive. The stream context stays open until exhausted."""
        with self._client.messages.stream(
            model=model or self.settings.model,
            max_tokens=max_tokens or self.settings.max_tokens,
            system=system if system is not None else _OMIT,
            messages=list(messages),
            **self._output_config(),
        ) as stream:
            yield from stream.text_stream

    # -- the tool_use loop -----------------------------------------------------

    def run_tool_loop(
        self,
        *,
        messages: Sequence[MessageParam],
        tools: Sequence[ToolSpec],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_iterations: int | None = None,
    ) -> ToolLoopResult:
        """Drive the agentic loop: call the model, run any tools it requests, feed the results
        back, and repeat until it stops asking for tools. Returns the final answer plus a full
        record of every tool call for tracing.
        """
        by_name = {t.name: t for t in tools}
        api_tools = [t.to_api() for t in tools]
        convo: list[MessageParam] = list(messages)
        records: list[ToolCallRecord] = []
        usage = Usage()
        limit = max_iterations or self.settings.max_tool_iterations

        for round_no in range(1, limit + 1):
            resp = self._client.messages.create(
                model=model or self.settings.model,
                max_tokens=max_tokens or self.settings.max_tokens,
                system=system if system is not None else _OMIT,
                messages=convo,
                tools=api_tools,
                **self._output_config(),
            )
            usage.input_tokens += resp.usage.input_tokens
            usage.output_tokens += resp.usage.output_tokens
            convo.append(cast("MessageParam", {"role": "assistant", "content": resp.content}))

            # A server-side tool paused mid-turn; re-send to let it resume (CONTRACTS-agnostic,
            # defensive - the harness only registers client tools today, so this is rare).
            if resp.stop_reason == "pause_turn":
                continue

            if resp.stop_reason != "tool_use":
                return ToolLoopResult(
                    final_message=resp,
                    messages=convo,
                    tool_calls=records,
                    stop_reason=resp.stop_reason,
                    rounds=round_no,
                    usage=usage,
                )

            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if isinstance(block, ToolUseBlock):
                    record, result_block = self._run_tool(block, by_name)
                    records.append(record)
                    tool_results.append(result_block)
            convo.append(cast("MessageParam", {"role": "user", "content": tool_results}))

        raise ToolLoopLimitError(
            f"tool_use loop still requesting tools after {limit} iterations "
            f"({len(records)} tool calls made)"
        )

    def _run_tool(
        self, block: ToolUseBlock, by_name: dict[str, ToolSpec]
    ) -> tuple[ToolCallRecord, dict[str, Any]]:
        """Execute one tool call, timing it and turning any failure into an ``is_error`` result
        the model can recover from rather than an exception that kills the loop."""
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        args = block.input if isinstance(block.input, dict) else {}

        spec = by_name.get(block.name)
        if spec is None:
            content, ok = f"Unknown tool: {block.name}", False
        else:
            try:
                out = spec.handler(args)
            except Exception as exc:
                content, ok = f"Tool '{block.name}' raised {type(exc).__name__}: {exc}", False
            else:
                if isinstance(out, ToolResult):
                    content, ok = out.content, not out.is_error
                else:
                    content, ok = out, True

        duration_ms = int((time.monotonic() - t0) * 1000)
        record = ToolCallRecord(
            id=block.id,
            name=block.name,
            arguments=args,
            output=content,
            ok=ok,
            duration_ms=duration_ms,
            started_at=started_at,
        )
        result_block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": content,
            "is_error": not ok,
        }
        return record, result_block
