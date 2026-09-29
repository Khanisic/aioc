"""The Claude API harness for the Reasoning Layer (Day 2).

`LLMClient` wraps the Anthropic Messages API with three entry points:

  - `LLMClient.complete` - one request, one response.
  - `LLMClient.stream_text` - stream text deltas as they arrive.
  - `LLMClient.run_tool_loop` - the manual ``tool_use`` loop the agents build on.

Register tools as `ToolSpec` objects; the loop returns a `ToolLoopResult` with the final
answer and a `ToolCallRecord` for every tool call. `McpStdioToolset` (Day 11) turns a stdio
MCP server into `ToolSpec`s so an agent can drive it through that same loop.

Day 19 added the two cost levers. Prompt caching is request shaping inside `LLMClient`
(`LLMSettings.prompt_caching`, on by default) and `Usage` carries the cache counters. The
Batch API is `aioc.llm.batch`: `MessageBatcher` is the wire and `DeferredClient` runs code
written for `complete` through a batch unchanged. `aioc.llm.pricing` prices a `Usage`.
"""

from __future__ import annotations

from .batch import (
    BatchError,
    BatchResult,
    BatchRun,
    DeferredClient,
    MessageBatcher,
    PendingRequest,
    request_key,
)
from .client import LLMClient, system_text
from .config import LLMSettings
from .mcp import McpStdioToolset, McpToolsetError, open_module_toolset
from .pricing import BATCH_DISCOUNT, CACHE_WRITE_MULTIPLIER, PRICES, Price, price, price_uncached
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
    "BATCH_DISCOUNT",
    "CACHE_WRITE_MULTIPLIER",
    "PRICES",
    "BatchError",
    "BatchResult",
    "BatchRun",
    "DeferredClient",
    "LLMClient",
    "LLMSettings",
    "McpStdioToolset",
    "McpToolsetError",
    "MessageBatcher",
    "PendingRequest",
    "Price",
    "ToolCallRecord",
    "ToolHandler",
    "ToolLoopLimitError",
    "ToolLoopResult",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "open_module_toolset",
    "price",
    "price_uncached",
    "request_key",
    "system_text",
]
