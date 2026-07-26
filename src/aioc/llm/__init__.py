"""The Claude API harness for the Reasoning Layer (Day 2).

`LLMClient` wraps the Anthropic Messages API with three entry points:

  - `LLMClient.complete` - one request, one response.
  - `LLMClient.stream_text` - stream text deltas as they arrive.
  - `LLMClient.run_tool_loop` - the manual ``tool_use`` loop the agents build on.

Register tools as `ToolSpec` objects; the loop returns a `ToolLoopResult` with the final
answer and a `ToolCallRecord` for every tool call.
"""

from __future__ import annotations

from .client import LLMClient
from .config import LLMSettings
from .tool_use import (
    ToolCallRecord,
    ToolHandler,
    ToolLoopLimitError,
    ToolLoopResult,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "LLMClient",
    "LLMSettings",
    "ToolCallRecord",
    "ToolHandler",
    "ToolLoopLimitError",
    "ToolLoopResult",
    "ToolResult",
    "ToolSpec",
    "Usage",
]
