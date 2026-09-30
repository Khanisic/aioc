"""The Messages API's own rules on a conversation, for the scripted fake clients.

A fake that accepts any conversation lets the harness send one the real API refuses, and
the first time anyone finds out is a live run. That happened on 2026-09-30: a tool loop
that stopped at `max_tokens` part-way through a `tool_use` handed its conversation on with
that call unanswered, and the forced emit was refused with a 400. Every fake that stands in
for `messages.create` should call `check_conversation` on what it is sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _blocks(content: Any) -> list[Any]:
    return content if isinstance(content, list) else []


def _field(block: Any, name: str) -> Any:
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def check_conversation(messages: Sequence[Any]) -> None:
    """Raise AssertionError where the real API would answer 400.

    The rule checked is the one the live refusal named: every `tool_use` in an assistant
    turn is answered by a `tool_result` with its id in the message immediately after, and
    a `tool_result` answers a `tool_use` of the turn immediately before.
    """
    for i, message in enumerate(messages):
        role = _field(message, "role")
        content = _blocks(_field(message, "content"))
        if role == "assistant":
            asked = [_field(b, "id") for b in content if _field(b, "type") == "tool_use"]
            if not asked:
                continue
            following = messages[i + 1] if i + 1 < len(messages) else None
            answered = {
                _field(b, "tool_use_id")
                for b in _blocks(_field(following, "content") if following is not None else None)
                if _field(b, "type") == "tool_result"
            }
            missing = [tool_id for tool_id in asked if tool_id not in answered]
            assert not missing, (
                f"messages.{i}: `tool_use` ids were found without `tool_result` blocks "
                f"immediately after: {', '.join(missing)}"
            )
        elif role == "user":
            results = [
                _field(b, "tool_use_id") for b in content if _field(b, "type") == "tool_result"
            ]
            if not results:
                continue
            previous = messages[i - 1] if i > 0 else None
            asked_before = {
                _field(b, "id")
                for b in _blocks(_field(previous, "content") if previous is not None else None)
                if _field(b, "type") == "tool_use"
            }
            stray = [tool_id for tool_id in results if tool_id not in asked_before]
            assert not stray, (
                f"messages.{i}: `tool_result` blocks answer no `tool_use` in the previous "
                f"message: {', '.join(stray)}"
            )
