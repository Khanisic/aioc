"""A stdio MCP server as a set of harness `ToolSpec`s (Day 11).

Until today every agent read its data in-process (Prometheus, the retrieval layer); the
MCP servers under `aioc.tools` existed for Claude Code and for the tests. `McpStdioToolset`
is the seam that lets an agent drive one of them over the real wire: it launches the server
as a subprocess, speaks MCP to it, and exposes every tool the server lists as a `ToolSpec`
whose handler forwards the call. `LLMClient.run_tool_loop` then sees ordinary tools.

Why a thread and not `asyncio.run` per call: the MCP client is async and its transport must
be entered and exited from the same task, while the agents are synchronous and may already
be running on the executor's worker threads (the Day 9 parallel group). So the session
lives on one dedicated thread with its own event loop for the toolset's lifetime, and each
tool call is submitted to that loop and awaited from the caller's thread. One subprocess
per toolset, opened once per agent run, closed on exit.

Error handling follows the contract envelope (sec 6): a tool call that returns
``isError: true`` becomes a `ToolResult(is_error=True)` whose content is the structured
error payload verbatim, so the model can read the class and act on it - retry only
`transient`, change the request on `validation`, record a gap on the rest. Transport
failures (the server died, the call timed out) raise `McpToolsetError`; the harness loop
turns a raised handler into an error result the model can see, and the agent decides.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .tool_use import ToolResult, ToolSpec


class McpToolsetError(RuntimeError):
    """The server could not be started, listed, or called over the wire."""


def _text_of(result: types.CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, types.TextContent))


class McpStdioToolset:
    """One running stdio MCP server, as `ToolSpec`s. Use as a context manager."""

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        env: dict[str, str] | None = None,
        startup_timeout_s: float = 30.0,
        call_timeout_s: float = 120.0,
    ) -> None:
        self._params = StdioServerParameters(command=command, args=list(args), env=env)
        self._startup_timeout = startup_timeout_s
        self._call_timeout = call_timeout_s
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._ready: Future[list[types.Tool]] = Future()
        self._closed: Future[None] = Future()
        self._server_name: str | None = None
        self._tools: list[ToolSpec] = []

    @classmethod
    def for_module(
        cls, module: str, *, env: dict[str, str] | None = None, **kwargs: Any
    ) -> McpStdioToolset:
        """Run ``python -m <module>`` with the interpreter this process runs on, so the
        server sees the same virtualenv (and therefore the same `aioc` package)."""
        return cls(sys.executable, ["-m", module], env=env, **kwargs)

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> McpStdioToolset:
        self._thread = threading.Thread(target=self._run_loop, name="aioc-mcp-client", daemon=True)
        self._thread.start()
        try:
            listed = self._ready.result(timeout=self._startup_timeout)
        except TimeoutError as exc:
            self._shutdown()
            raise McpToolsetError(
                f"MCP server {self._params.command} {' '.join(self._params.args)} did not "
                f"initialise within {self._startup_timeout}s"
            ) from exc
        except Exception as exc:
            self._shutdown()
            raise McpToolsetError(f"MCP server failed to start: {exc}") from exc
        self._tools = [self._spec(tool) for tool in listed]
        return self

    def __exit__(self, *_: object) -> None:
        self._shutdown()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - reported through the futures
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            loop.close()
            if not self._closed.done():
                self._closed.set_result(None)

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                self._server_name = init.serverInfo.name
                self._session = session
                tools = (await session.list_tools()).tools
                self._ready.set_result(list(tools))
                await self._stop.wait()
        self._session = None

    def _shutdown(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(timeout=self._startup_timeout)

    # ------------------------------------------------------------------ tools

    @property
    def server_name(self) -> str:
        """What the server called itself at initialisation (e.g. ``aioc-github``)."""
        if self._server_name is None:
            raise McpToolsetError("toolset is not open")
        return self._server_name

    @property
    def tools(self) -> list[ToolSpec]:
        if not self._tools:
            raise McpToolsetError("toolset is not open")
        return list(self._tools)

    def _spec(self, tool: types.Tool) -> ToolSpec:
        name = tool.name

        def handler(args: dict[str, Any]) -> ToolResult:
            return self.call(name, args)

        return ToolSpec(
            name=name,
            description=tool.description or "",
            input_schema=dict(tool.inputSchema),
            handler=handler,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Invoke one tool over the wire from any thread."""
        loop, session = self._loop, self._session
        if loop is None or session is None or loop.is_closed():
            raise McpToolsetError("toolset is not open")
        future = asyncio.run_coroutine_threadsafe(session.call_tool(name, arguments), loop)
        try:
            result = future.result(timeout=self._call_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise McpToolsetError(f"{name} did not return within {self._call_timeout}s") from exc
        except Exception as exc:
            raise McpToolsetError(f"{name} failed over the MCP wire: {exc}") from exc
        return ToolResult(content=_text_of(result), is_error=bool(result.isError))


@contextmanager
def open_module_toolset(module: str, **kwargs: Any) -> Iterator[McpStdioToolset]:
    """`with open_module_toolset("aioc.tools.github.server") as toolset:` - the one-liner."""
    with McpStdioToolset.for_module(module, **kwargs) as toolset:
        yield toolset
