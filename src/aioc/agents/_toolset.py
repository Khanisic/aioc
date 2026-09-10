"""The tool-driven agent's shared plumbing (Day 12): the `Toolset` seam and the ledger.

Two agents now drive an AIOC MCP server over the wire (GitHub on Day 11, Deployment on
Day 12), and both need the same three things: a seam through which tests inject an
in-process fake toolset, an honest `ToolCallRef` per wire call, and a way to check that
what the model reports traces back to what a tool actually returned. The GitHub agent
pioneered all three; this module is them lifted out, with the domain-specific indexing
(PRs by number, releases by version) left to a subclass hook.

Grounding is done against the DECODED strings of each reply, not only the wire text: on
the wire a commit message's quotes, newlines, and non-ASCII are JSON-escaped, so a
faithful excerpt would never match the raw output (the first Day 11 live run failed on
exactly this). The raw text stays too - a model that quotes ``"touched_paths": [...]`` as
it saw it is being faithful, not sloppy - and so does a flattened ``key: value`` line per
scalar leaf, because a number the tool returned (``"error_rate": 0.31``) is evidence as
much as a string is. The agent's explicit context block is a third source: the agent
literally has no information beyond the context it was handed and the replies it
received, so an excerpt must appear in one of them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import uuid4

from aioc.contracts import ErrorClass, ToolCallRef
from aioc.llm import ToolCallRecord, ToolSpec


class Toolset(Protocol):
    """What an agent needs from an open MCP toolset. `McpStdioToolset` satisfies it and
    tests inject in-process fakes through the same seam."""

    @property
    def server_name(self) -> str: ...

    @property
    def tools(self) -> list[ToolSpec]: ...


ToolsetFactory = Callable[[], AbstractContextManager[Toolset]]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def normalise(text: str) -> str:
    """Whitespace-collapsed, quote-free, case-preserving. Quotes are dropped on both sides
    of a comparison so ``"error_rate": 0.31`` and ``error_rate: 0.31`` are the same fact."""
    return " ".join(text.replace('"', "").split())


def parse_envelope(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def error_class_of(envelope: dict[str, Any] | None) -> ErrorClass:
    """The envelope's class when it has one; a transport failure (the server died, the call
    timed out, the text was not JSON) is transient by the taxonomy's own definition."""
    error = (envelope or {}).get("error")
    if isinstance(error, dict):
        try:
            return ErrorClass(str(error.get("class")))
        except ValueError:
            pass
    return ErrorClass.TRANSIENT


def string_leaves(value: Any) -> Iterator[str]:
    """Every string in a decoded JSON value, depth first, in document order."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_leaves(item)
    elif isinstance(value, list):
        if value and all(isinstance(item, str) for item in value):
            # A list of strings (touched paths, say) is quoted naturally as "a, b, c";
            # accept that join as well as each element on its own.
            yield ", ".join(value)
        for item in value:
            yield from string_leaves(item)


def scalar_facts(value: Any) -> Iterator[str]:
    """Every scalar leaf of a decoded JSON value as ``key: value`` - the way a model quotes
    a number or a flag it read (``p99_latency_ms: 812.4``, ``truncated: false``)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from scalar_facts(item)
            else:
                yield f"{key}: {json.dumps(item)}"
    elif isinstance(value, list):
        for item in value:
            yield from scalar_facts(item)


_DIFF_MARKER = re.compile(r"^[+\- ]", re.MULTILINE)


def unmarked(text: str) -> str:
    """A multi-line patch hunk with its per-line `+`/`-`/space markers removed. Quoting a
    hunk that way is still verbatim content; quoting it with the markers is too."""
    if "\n" not in text:
        return ""
    stripped = _DIFF_MARKER.sub("", text)
    return stripped if stripped != text else ""


class ToolLedger:
    """Everything the tools returned, indexed for stamping and grounding.

    Built from the loop's `ToolCallRecord`s after the fact - the envelope text is the
    record's ``output``, so no handler wrapping is needed and a fake toolset in tests goes
    through exactly the same path as the wire. Subclasses override `index` to pick out
    the facts their agent stamps from each successful reply.
    """

    def __init__(
        self, records: list[ToolCallRecord], server: str, *, context: str | None = None
    ) -> None:
        self.refs: list[ToolCallRef] = []
        self.ref_for_record: dict[str, str] = {}  # record.id -> ToolCallRef.id
        self.outputs: list[tuple[str, str]] = []  # (tc id, normalised text)
        self.any_ok = False
        self._context = normalise(context) if context else ""
        for record in records:
            envelope = parse_envelope(record.output)
            tc_id = new_id("tc")
            ok = record.ok and bool(envelope and envelope.get("ok"))
            error_class: ErrorClass | None = None
            meta: dict[str, Any] = {}
            if ok:
                self.any_ok = True
                meta = dict((envelope or {}).get("meta") or {})
                self.index(dict((envelope or {}).get("data") or {}), tc_id, record)
            else:
                error_class = error_class_of(envelope)
            self.refs.append(
                ToolCallRef(
                    id=tc_id,
                    tool_name=record.name,
                    server=server,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                    ok=ok,
                    error_class=error_class,
                    tokens_returned=meta.get("token_estimate") if ok else None,
                    truncated=bool(meta.get("truncated")) if ok else False,
                )
            )
            self.ref_for_record[record.id] = tc_id
            leaves = list(string_leaves(envelope)) if envelope is not None else []
            facts = list(scalar_facts(envelope)) if envelope is not None else []
            texts = [record.output, *leaves, *(unmarked(leaf) for leaf in leaves), *facts]
            self.outputs.append((tc_id, " | ".join(normalise(t) for t in texts if t)))

    def index(self, data: dict[str, Any], tc_id: str, record: ToolCallRecord) -> None:
        """Hook: pick the stampable facts out of one successful reply's ``data``."""

    def quoted_in(self, excerpt: str) -> str | None:
        """The tool call id whose output contains ``excerpt`` verbatim, or None."""
        needle = normalise(excerpt)
        if not needle:
            return None
        for tc_id, text in self.outputs:
            if needle in text:
                return tc_id
        return None

    def in_context(self, excerpt: str) -> bool:
        """Whether ``excerpt`` appears verbatim in the explicit context block."""
        needle = normalise(excerpt)
        return bool(needle) and needle in self._context
