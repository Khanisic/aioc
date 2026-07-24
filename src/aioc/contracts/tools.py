"""Tool request/response envelope and the four-class error taxonomy (CONTRACTS.md sec 6).

Scope note: the Reasoning Layer consumes tool results, so the *envelope*, the *meta*
block, and the *error taxonomy* are modelled here (they drive retry decisions and the
token-reduction measurement). The per-tool `data` payloads in sec 7 are JSON-Schema
normative and owned by the Platform Layer's MCP server, so `data` stays an open dict.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from ._common import StrictModel
from .enums import ErrorClass


class ToolMeta(StrictModel):
    """Required on every success response. The baseline the trimming work reduces against."""

    truncated: bool
    returned: int
    token_estimate: int
    total_available: int | None = None
    query_ms: int | None = None
    source: str | None = None
    as_of: datetime | None = None


class ToolSuccess(StrictModel):
    """The JSON payload inside a successful tool result's ``content[].text``.

    An empty result is a success (``ok: true`` with an empty collection in ``data`` and
    ``meta.returned: 0``), not an error.
    """

    ok: Literal[True] = True
    data: dict[str, Any]
    meta: ToolMeta


class ToolError(StrictModel):
    """The error body. ``class`` on the wire aliases to ``error_class`` (``class`` is a keyword).

    Only ``transient`` is retryable; retrying any other class fails identically, so the
    agent must change the request, escalate, or record a `Gap` with ``resolvable: false``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    error_class: ErrorClass = Field(alias="class")
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None = None
    details: dict[str, Any] | None = None
    remediation: str

    @model_validator(mode="after")
    def _check(self) -> "ToolError":
        if self.error_class is ErrorClass.TRANSIENT:
            if not self.retryable:
                raise ValueError("transient errors must set retryable=true")
            if self.retry_after_ms is None:
                raise ValueError("transient errors must set retry_after_ms (non-null)")
        else:
            if self.retryable:
                raise ValueError("only transient errors may be retryable")
            if self.retry_after_ms is not None:
                raise ValueError("retry_after_ms must be null for non-transient errors")
        return self


class ToolFailure(StrictModel):
    """The JSON payload inside a failed tool result's ``content[].text``."""

    ok: Literal[False] = False
    error: ToolError
