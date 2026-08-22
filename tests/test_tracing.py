"""Day 9: the tracing seam - null-object degradation and the Langfuse adapter.

Everything here is offline. The Langfuse SDK is exercised only through an injected stub
client (the same pattern the Voyage embedder tests use with a mock transport), and
`default_tracer` is always given explicit settings so a real key in the developer's
`.env` can never turn these tests into network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aioc.contracts import ErrorClass, ToolCallRef
from aioc.observability.tracing import (
    LangfuseTracer,
    NullTracer,
    TracingSettings,
    default_tracer,
)


def _settings(
    public: str | None = "pk-lf-test", secret: str | None = "sk-lf-test"
) -> TracingSettings:
    return TracingSettings(public_key=public, secret_key=secret)  # type: ignore[arg-type]


def _ref(*, ok: bool = True) -> ToolCallRef:
    return ToolCallRef.model_validate(
        {
            "id": "tc_1",
            "tool_name": "search_corpus",
            "server": "aioc-docs",
            "started_at": "2026-08-08T14:20:00Z",
            "duration_ms": 42,
            "ok": ok,
            "error_class": None if ok else ErrorClass.TRANSIENT.value,
            "tokens_returned": 850,
            "truncated": False,
        }
    )


# ------------------------------------------------------------------------ the null object


def test_default_tracer_is_null_without_keys():
    assert isinstance(default_tracer(_settings(public=None, secret=None)), NullTracer)
    # One key alone is not configured - a half-filled .env must degrade, not half-work.
    assert isinstance(default_tracer(_settings(secret=None)), NullTracer)
    assert isinstance(default_tracer(_settings(public=None)), NullTracer)


def test_default_tracer_is_langfuse_with_both_keys():
    # Constructing the tracer must not build the SDK client (that is lazy, on the first
    # request) - this assertion stays offline.
    assert isinstance(default_tracer(_settings()), LangfuseTracer)


def test_null_tracer_is_a_complete_no_op_with_a_null_trace_id():
    trace = NullTracer().start_request("coordinator_request", request_id="req_1", query="q")
    assert trace.trace_id is None
    span = trace.start_span("agent:incident", input_text="ctx")
    span.record_tool_call(_ref())
    span.end(output="s", status="complete", input_tokens=1, output_tokens=2)
    trace.end(output="s", status="complete", input_tokens=1, output_tokens=2)
    NullTracer().flush()


def test_settings_env_file_is_the_repo_root_dotenv():
    # The Day 8 regression, pinned here too: a path expression copied between modules at
    # different depths silently reads keys as unset. Pin to where pyproject.toml lives.
    env_file = TracingSettings.model_config["env_file"]
    assert isinstance(env_file, Path)
    assert env_file.name == ".env"
    assert (env_file.parent / "pyproject.toml").is_file()


# ------------------------------------------------------------------- the langfuse adapter


class _StubObservation:
    """Duck-types the slice of `LangfuseObservationWrapper` the adapter touches."""

    def __init__(self, name: str, kwargs: dict[str, Any]) -> None:
        self.name = name
        self.kwargs = kwargs
        self.trace_id = "lf_trace_1"
        self.children: list[_StubObservation] = []
        self.events: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.trace_io: list[dict[str, Any]] = []
        self.ended = 0

    def start_observation(self, *, name: str, **kwargs: Any) -> _StubObservation:
        child = _StubObservation(name, kwargs)
        self.children.append(child)
        return child

    def create_event(self, *, name: str, **kwargs: Any) -> None:
        self.events.append({"name": name, **kwargs})

    def update(self, **kwargs: Any) -> _StubObservation:
        self.updates.append(kwargs)
        return self

    def set_trace_io(self, **kwargs: Any) -> _StubObservation:
        self.trace_io.append(kwargs)
        return self

    def end(self) -> _StubObservation:
        self.ended += 1
        return self


class _StubClient:
    def __init__(self, *, auth_ok: bool = True, trace_url_raises: bool = False) -> None:
        self.roots: list[_StubObservation] = []
        self.flushes = 0
        self._auth_ok = auth_ok
        self._trace_url_raises = trace_url_raises

    def start_observation(self, *, name: str, **kwargs: Any) -> _StubObservation:
        root = _StubObservation(name, kwargs)
        self.roots.append(root)
        return root

    def flush(self) -> None:
        self.flushes += 1

    def auth_check(self) -> bool:
        if not self._auth_ok:
            raise RuntimeError("401 Unauthorized (stub)")
        return True

    def get_trace_url(self, *, trace_id: str) -> str:
        if self._trace_url_raises:
            # The real SDK fetches the project id here, so bad credentials raise
            # UnauthorizedError from this call - the shape of the first live failure.
            raise RuntimeError("401 Unauthorized (stub)")
        return f"https://stub.langfuse/trace/{trace_id}"


def test_langfuse_adapter_builds_the_observation_tree():
    client = _StubClient()
    tracer = LangfuseTracer(_settings(), client=client)  # type: ignore[arg-type]

    trace = tracer.start_request("coordinator_request", request_id="req_1", query="why?")
    assert trace.trace_id == "lf_trace_1"

    span = trace.start_span("agent:incident", input_text="the context block", metadata={"round": 0})
    span.record_tool_call(_ref())
    span.end(output="summary", status="complete", input_tokens=120, output_tokens=240)
    trace.end(output="the answer", status="complete", input_tokens=420, output_tokens=420)
    tracer.flush()

    (root,) = client.roots
    assert root.name == "coordinator_request"
    assert root.kwargs["as_type"] == "chain"
    assert root.kwargs["input"] == "why?"
    assert root.kwargs["metadata"] == {"request_id": "req_1"}

    (agent,) = root.children
    assert agent.name == "agent:incident"
    assert agent.kwargs["as_type"] == "agent"
    assert agent.kwargs["input"] == "the context block"

    (event,) = agent.events
    assert event["name"] == "tool:search_corpus"
    # The honesty rule from the module docstring: the measured timing rides in metadata.
    assert event["metadata"]["duration_ms"] == 42
    assert event["metadata"]["started_at"] == "2026-08-08T14:20:00+00:00"

    (agent_update,) = agent.updates
    assert agent_update["usage_details"] == {"input": 120, "output": 240}
    assert agent_update["level"] == "DEFAULT"
    assert agent.ended == 1

    (root_update,) = root.updates
    assert root_update["output"] == "the answer"
    assert root_update["usage_details"] == {"input": 420, "output": 420}
    assert root.trace_io == [{"output": "the answer"}]
    assert root.ended == 1
    assert client.flushes == 1


def test_langfuse_adapter_marks_errors():
    client = _StubClient()
    tracer = LangfuseTracer(_settings(), client=client)  # type: ignore[arg-type]

    trace = tracer.start_request("coordinator_request", request_id="req_1", query="why?")
    span = trace.start_span("agent:incident", input_text="ctx")
    span.record_tool_call(_ref(ok=False))
    span.end(
        output=None,
        status="error",
        input_tokens=10,
        output_tokens=0,
        error="RuntimeError: boom",
    )

    (root,) = client.roots
    (agent,) = root.children
    (event,) = agent.events
    assert event["level"] == "ERROR"
    assert event["status_message"] == "transient"
    (update,) = agent.updates
    assert update["level"] == "ERROR"
    assert update["status_message"] == "RuntimeError: boom"


def test_plain_spans_are_not_typed_as_agents():
    client = _StubClient()
    tracer = LangfuseTracer(_settings(), client=client)  # type: ignore[arg-type]
    trace = tracer.start_request("coordinator_request", request_id="req_1", query="why?")
    trace.start_span("plan", input_text="why?")
    (root,) = client.roots
    assert root.children[0].kwargs["as_type"] == "span"


def test_auth_check_delegates_and_raises_loudly():
    # The check script calls this before any work; a 401 must surface here, not as a
    # swallowed background-export failure after the run already reported success.
    assert LangfuseTracer(_settings(), client=_StubClient()).auth_check() is True  # type: ignore[arg-type]
    bad = LangfuseTracer(_settings(), client=_StubClient(auth_ok=False))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="401"):
        bad.auth_check()


def test_trace_url_never_raises():
    # Regression from the first live run: wrong-region keys made get_trace_url raise
    # UnauthorizedError after the check's real work had finished, crashing the script.
    ok = LangfuseTracer(_settings(), client=_StubClient())  # type: ignore[arg-type]
    assert ok.trace_url("t_1") == "https://stub.langfuse/trace/t_1"
    broken = LangfuseTracer(_settings(), client=_StubClient(trace_url_raises=True))  # type: ignore[arg-type]
    assert broken.trace_url("t_1") is None
    assert LangfuseTracer(_settings()).trace_url("t_1") is None  # no client built yet


def test_langfuse_tracer_refuses_to_run_without_keys():
    # Direct construction without keys is a programming error, reported loudly - only
    # default_tracer() is allowed to decide "no keys means no tracing".
    tracer = LangfuseTracer(_settings(public=None, secret=None))
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        tracer.start_request("coordinator_request", request_id="req_1", query="q")
    tracer.flush()  # flush without a built client is a quiet no-op, not a crash
