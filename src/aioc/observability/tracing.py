"""Tracing into Langfuse (Day 9, Platform Layer).

The seam is three small protocols - `Tracer`, `RequestTrace`, `AgentSpan` - shaped around
what the coordinator actually records: one trace per request, one span for the planning
call, one span per agent invocation, and the contract `ToolCallRef` records attached to
the agent span that made them. The executor talks only to the protocols; tests inject a
recording fake, and the Langfuse SDK stays behind `LangfuseTracer`.

Tracing is **opt-in at the entry point**. `Executor` and `respond` default to
`NullTracer`, and the live scripts pass `default_tracer()` explicitly. This is deliberate,
not an oversight: the offline suite must make zero network calls even on a machine whose
`.env` carries real Langfuse keys, and a tracer that self-activated from the environment
would break that silently. `default_tracer()` follows the Day 8 `default_embedder()`
pattern - honest degradation to `NullTracer` when the keys are unset - so the entry
points never need to know which backend they got.

One honesty note on tool-call spans: the Langfuse observation API stamps timestamps at
creation, and the contract's `ToolCallRef` records arrive *after* the agent returns. They
are therefore recorded as child events whose metadata carries the measured `started_at`
and `duration_ms` - the true timing is in the metadata, not the event's own timestamp.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aioc.contracts import ToolCallRef

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse._client.span import LangfuseObservationWrapper


class TracingSettings(BaseSettings):
    """Read from the process environment first and the repo `.env` second, the same
    precedence `aioc.llm.LLMSettings` and `aioc.retrieval.EmbeddingSettings` use."""

    # parents[3] from src/aioc/observability/tracing.py is the repo root - the same depth
    # as retrieval/embeddings.py, whose Day 8 regression (a path copied from one directory
    # deeper, silently reading keys as unset) is why the resolved path is pinned by a test.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Tests construct these settings explicitly by field name; the aliases are for
        # the environment only.
        populate_by_name=True,
    )

    public_key: SecretStr | None = Field(default=None, validation_alias="LANGFUSE_PUBLIC_KEY")
    secret_key: SecretStr | None = Field(default=None, validation_alias="LANGFUSE_SECRET_KEY")
    host: str = Field(default="https://cloud.langfuse.com", validation_alias="LANGFUSE_HOST")

    @property
    def configured(self) -> bool:
        return self.public_key is not None and self.secret_key is not None


# ------------------------------------------------------------------------------ the seam


class AgentSpan(Protocol):
    """One traced unit of work inside a request: the planning call or one agent run."""

    def record_tool_call(self, ref: ToolCallRef) -> None:
        """Attach one contract `ToolCallRef` to this span (see the module docstring on
        why its measured timing rides in metadata)."""
        ...

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None: ...


class RequestTrace(Protocol):
    """One coordinator request, from `received_at` to the assembled response."""

    @property
    def trace_id(self) -> str | None:
        """What `CoordinatorResponse.trace_id` carries; ``None`` when tracing is off."""
        ...

    def start_span(
        self, name: str, *, input_text: str, metadata: dict[str, Any] | None = None
    ) -> AgentSpan: ...

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None: ...


class Tracer(Protocol):
    """The factory the entry points hold. Thread-safety contract: `RequestTrace.start_span`
    may be called from concurrent worker threads (the Day 9 parallel group), and each
    returned `AgentSpan` is used only by the thread that started it."""

    def start_request(self, name: str, *, request_id: str, query: str) -> RequestTrace: ...

    def flush(self) -> None:
        """Block until buffered spans are exported - short-lived scripts call this before
        exiting, long-lived processes never need to."""
        ...


# ------------------------------------------------------------------------- the null object


class NullSpan:
    def record_tool_call(self, ref: ToolCallRef) -> None:
        return None

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        return None


class NullTrace:
    @property
    def trace_id(self) -> str | None:
        return None

    def start_span(
        self, name: str, *, input_text: str, metadata: dict[str, Any] | None = None
    ) -> AgentSpan:
        return NullSpan()

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        return None


class NullTracer:
    """Tracing switched off: every operation is a no-op and `trace_id` stays ``None``.

    A null object rather than ``None`` so the executor never branches on "is tracing on" -
    the alternative scatters conditionals through exactly the code Day 9 makes concurrent.
    """

    def start_request(self, name: str, *, request_id: str, query: str) -> RequestTrace:
        return NullTrace()

    def flush(self) -> None:
        return None


# -------------------------------------------------------------------- the langfuse adapter


class _LangfuseSpan:
    def __init__(self, observation: LangfuseObservationWrapper) -> None:
        self._observation = observation

    def record_tool_call(self, ref: ToolCallRef) -> None:
        self._observation.create_event(
            name=f"tool:{ref.tool_name}",
            level="DEFAULT" if ref.ok else "ERROR",
            status_message=None if ref.ok else (ref.error_class.value if ref.error_class else None),
            metadata={
                "tool_call_id": ref.id,
                "server": ref.server,
                "started_at": ref.started_at.isoformat(),
                "duration_ms": ref.duration_ms,
                "ok": ref.ok,
                "tokens_returned": ref.tokens_returned,
                "truncated": ref.truncated,
            },
        )

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        self._observation.update(
            output=output,
            level="ERROR" if error is not None else "DEFAULT",
            status_message=error if error is not None else status,
            usage_details={"input": input_tokens, "output": output_tokens},
        )
        self._observation.end()


class _LangfuseTrace:
    def __init__(self, root: LangfuseObservationWrapper) -> None:
        self._root = root

    @property
    def trace_id(self) -> str | None:
        return str(self._root.trace_id)

    def start_span(
        self, name: str, *, input_text: str, metadata: dict[str, Any] | None = None
    ) -> AgentSpan:
        observation = self._root.start_observation(
            name=name,
            as_type="agent" if name.startswith("agent:") else "span",
            input=input_text,
            metadata=metadata,
        )
        return _LangfuseSpan(observation)

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        self._root.update(
            output=output,
            level="ERROR" if error is not None else "DEFAULT",
            status_message=error if error is not None else status,
            usage_details={"input": input_tokens, "output": output_tokens},
        )
        self._root.set_trace_io(output=output)
        self._root.end()


class LangfuseTracer:
    """The Langfuse SDK behind the `Tracer` protocol.

    The client is built lazily on the first `start_request` (and is injectable for tests):
    constructing the SDK client spins up OTel export machinery, and `default_tracer` must
    be able to *choose* this tracer without paying that cost until a request actually runs.
    """

    def __init__(
        self, settings: TracingSettings | None = None, *, client: Langfuse | None = None
    ) -> None:
        self._settings = settings or TracingSettings()
        self._client = client

    def _ensure_client(self) -> Langfuse:
        if self._client is None:
            public, secret = self._settings.public_key, self._settings.secret_key
            if public is None or secret is None:
                raise RuntimeError(
                    "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set (shell or .env); "
                    "use default_tracer(), which degrades to NullTracer without them."
                )
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=public.get_secret_value(),
                secret_key=secret.get_secret_value(),
                host=self._settings.host,
            )
        return self._client

    def start_request(self, name: str, *, request_id: str, query: str) -> RequestTrace:
        root = self._ensure_client().start_observation(
            name=name,
            as_type="chain",
            input=query,
            metadata={"request_id": request_id},
        )
        return _LangfuseTrace(root)

    def flush(self) -> None:
        if self._client is not None:
            self._client.flush()

    def trace_url(self, trace_id: str) -> str | None:
        """A direct link to the trace in the Langfuse UI, or ``None`` before any request
        has run. Adapter-specific on purpose - the protocol has no notion of a UI."""
        if self._client is None:
            return None
        return self._client.get_trace_url(trace_id=trace_id)


def default_tracer(settings: TracingSettings | None = None) -> Tracer:
    """The configured tracer: Langfuse when both keys are set, otherwise `NullTracer`."""
    settings = settings or TracingSettings()
    if not settings.configured:
        return NullTracer()
    return LangfuseTracer(settings)
