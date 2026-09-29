"""Tool abstractions for the harness ``tool_use`` loop (Day 2).

These are the harness's own lightweight types, deliberately decoupled from the frozen
contract. The contract's `ToolCallRef` and the four-class error taxonomy (CONTRACTS.md
sec 6) describe the *MCP* boundary; those land in Phase 2 when real tool servers exist.
A harness tool here is just a Python callable Claude can invoke, so `ToolCallRecord` mirrors
`ToolCallRef` in spirit (id, name, timing, ok) without importing it - the boundary the
CLAUDE.md calls out (a tool server must not depend on the contract models) cuts both ways.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from anthropic.types import Message, MessageParam, TextBlock, ToolParam


@dataclass(slots=True)
class ToolResult:
    """What a tool handler returns. A handler may return a bare ``str`` for the success case;
    wrap it in `ToolResult` to signal failure (``is_error=True``) so Claude can adapt or retry."""

    content: str
    is_error: bool = False


# A handler receives the parsed tool input and returns text for the model. Returning a bare
# str is the success shortcut; return a `ToolResult` to mark an error.
ToolHandler = Callable[[dict[str, Any]], "ToolResult | str"]


@dataclass(slots=True)
class ToolSpec:
    """A tool Claude can call: its wire schema plus the Python callable that fulfils it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_api(self) -> ToolParam:
        """The ``tools[]`` entry sent to the Messages API (handler stays client-side).

        Cast: ``input_schema`` is a plain dict on this side so callers aren't forced to build
        the SDK's ``InputSchema`` TypedDict; the shape is validated by the API on the wire.
        """
        return cast(
            "ToolParam",
            {
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
            },
        )


@dataclass(slots=True)
class ToolCallRecord:
    """One tool invocation, captured for tracing and the Day 21 token-reduction measurement."""

    id: str
    name: str
    arguments: dict[str, Any]
    output: str
    ok: bool
    duration_ms: int
    started_at: datetime


@dataclass(slots=True)
class Usage:
    """Token usage accumulated across every model call in a loop.

    A plain value object on purpose - no lock. Concurrent runners (the Day 9 parallel
    group) each get their own accumulator, folded into the request total with `add` after
    the join; ``+=`` on a shared instance from two threads would lose counts silently.

    ``input_tokens`` is every prompt token the model read, however it was billed. With
    prompt caching on (Day 19) the API reports the prompt in three parts - the uncached
    remainder, the tokens written to the cache, and the tokens read from it - and `record`
    adds them back together, so a token count recorded before caching and one recorded
    after it measure the same thing. The two cache counters say how much of that total was
    billed at the write and read rates; they stay ``None`` until a response reports them
    (the contract's null: not measured, which is different from a measured zero).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def record(self, reported: Any) -> None:
        """Add one response's ``usage`` block. The only place the API's three-part prompt
        count is put back together - every call site goes through here."""
        read = _counter(reported, "cache_read_input_tokens")
        write = _counter(reported, "cache_creation_input_tokens")
        self.input_tokens += reported.input_tokens + (read or 0) + (write or 0)
        self.output_tokens += reported.output_tokens
        if read is not None:
            self.cache_read_tokens = (self.cache_read_tokens or 0) + read
        if write is not None:
            self.cache_write_tokens = (self.cache_write_tokens or 0) + write

    def add(self, other: Usage) -> None:
        """Fold another accumulator into this one."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        if other.cache_read_tokens is not None:
            self.cache_read_tokens = (self.cache_read_tokens or 0) + other.cache_read_tokens
        if other.cache_write_tokens is not None:
            self.cache_write_tokens = (self.cache_write_tokens or 0) + other.cache_write_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """The part of the prompt billed at the full input rate."""
        return self.input_tokens - (self.cache_read_tokens or 0) - (self.cache_write_tokens or 0)

    def as_record(self) -> dict[str, int | None]:
        """The shape the live checks write to ``test-results/`` and `scripts/cost_review.py`
        prices: the two totals, and the cache counters that say how the input was billed."""
        return {
            "in": self.input_tokens,
            "out": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }


def _counter(reported: Any, name: str) -> int | None:
    value = getattr(reported, name, None)
    # bool is an int; a scripted fake that answers every attribute is not a count either.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(slots=True)
class ToolLoopResult:
    """The outcome of `LLMClient.run_tool_loop`: the final answer plus the full audit trail."""

    final_message: Message
    messages: list[MessageParam]
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    stop_reason: str | None = None
    rounds: int = 0
    usage: Usage = field(default_factory=Usage)

    @property
    def text(self) -> str:
        """The final assistant turn's text, concatenated across any text blocks."""
        return "".join(
            block.text for block in self.final_message.content if isinstance(block, TextBlock)
        )


class ToolLoopLimitError(RuntimeError):
    """Raised when the loop hits ``max_tool_iterations`` still wanting to call tools.

    This is a runaway agent, not a slow one - surface it loudly rather than truncating.
    """
