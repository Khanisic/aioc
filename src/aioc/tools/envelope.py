"""The MCP tool wire envelope (CONTRACTS.md sec 6). Shared by all six tool servers.

**This module must not import `aioc.contracts`, and neither must any tool server.** The MCP
boundary is JSON Schema, not Pydantic (contract sec 6): a tool server that imports the
reasoning layer's models couples the two halves that the wire format exists to keep separate,
and would let a Pydantic-only change break a tool. The duplication of enum names here is the
price of that independence, and it is deliberate.

Two shapes, and `isError` always agrees with `ok`:

    success:  {"ok": true,  "data": {...}, "meta": {...}}      isError: false
    failure:  {"ok": false, "error": {"class": ..., ...}}      isError: true

Never encode a failure as prose inside a success payload. That is the exact failure mode the
four-class taxonomy exists to prevent - an agent cannot retry, escalate, or record a gap
against an error it cannot see.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from mcp import types

ErrorClass = Literal["transient", "validation", "business", "permission"]

# `transient` is the only retryable class. Retrying a validation, business, or permission
# error will fail identically - the caller must change the request, escalate, or record a Gap
# with resolvable: false.
_RETRYABLE: frozenset[str] = frozenset({"transient"})


def _text_result(payload: dict[str, Any], *, is_error: bool) -> types.CallToolResult:
    """Wrap a payload as the single text block the contract specifies.

    Returning `CallToolResult` rather than a content list is what lets a tool set `isError`
    itself. The alternative - raising, and letting the framework catch it - produces a
    plain-text error message and loses the structured error entirely.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, default=str))],
        isError=is_error,
    )


def ok(
    data: dict[str, Any],
    *,
    returned: int,
    truncated: bool,
    total_available: int | None = None,
    token_estimate: int | None = None,
    query_ms: int | None = None,
    source: str | None = None,
    as_of: str | None = None,
) -> types.CallToolResult:
    """A success response. `meta` is required on every one of them (contract sec 6.2).

    `meta` is not bookkeeping: the Day 21 trimming work and the Day 24 token-reduction
    measurement are both computed from these numbers, so a tool that omits them leaves
    nothing to reduce against. `truncated`, `returned`, and `token_estimate` are non-null.

    An empty collection is a success, not an error - `returned: 0`. A `business` error means
    the request cannot be computed, not that it computed to nothing.
    """
    if token_estimate is None:
        # Rough and documented as rough: a real tokenizer here would mean a network call per
        # tool response. ~4 characters per token is close enough to trend against, which is
        # all the Day 24 comparison needs.
        token_estimate = max(1, len(json.dumps(data, default=str)) // 4)
    return _text_result(
        {
            "ok": True,
            "data": data,
            "meta": {
                "truncated": truncated,
                "total_available": total_available,
                "returned": returned,
                "token_estimate": token_estimate,
                "query_ms": query_ms,
                "source": source,
                "as_of": as_of,
            },
        },
        is_error=False,
    )


def err(
    error_class: ErrorClass,
    code: str,
    message: str,
    *,
    remediation: str,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
) -> types.CallToolResult:
    """An error response. The class determines `retryable`, so it can never disagree.

    Per-class requirements from contract sec 6.4 are asserted rather than trusted, because a
    transient error with no retry hint or a validation error with no field is exactly the
    response an agent cannot act on:

    - `transient`  -> `retry_after_ms` required, retry hint in `remediation`
    - `validation` -> `details.field` and `details.expected` required
    - `permission` -> `details.required_scope` required
    - `business`   -> an alternative in `remediation`
    """
    retryable = error_class in _RETRYABLE
    if retryable and retry_after_ms is None:
        raise ValueError("transient errors must set retry_after_ms (contract sec 6.4)")
    if not retryable and retry_after_ms is not None:
        raise ValueError(f"{error_class} is not retryable; retry_after_ms must be null")

    details = dict(details or {})
    if error_class == "validation":
        missing = {"field", "expected"} - set(details)
        if missing:
            raise ValueError(f"validation errors must set details.{sorted(missing)}")
    if error_class == "permission" and "required_scope" not in details:
        raise ValueError("permission errors must set details.required_scope")
    if not remediation.strip():
        raise ValueError("every error needs a remediation the caller can act on")

    return _text_result(
        {
            "ok": False,
            "error": {
                # `class` is the wire name; it is a Python keyword, hence the dict literal.
                "class": error_class,
                "code": code,
                "message": message,
                "retryable": retryable,
                "retry_after_ms": retry_after_ms,
                "details": details or None,
                "remediation": remediation,
            },
        },
        is_error=True,
    )


class Timer:
    """Wall-clock milliseconds for `meta.query_ms`, measured monotonically."""

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        return self

    def __exit__(self, *_: object) -> Literal[False]:
        self.ms = int((time.monotonic() - self._start) * 1000)
        return False
