"""The Message Batches API as a second way to send the same request (Day 19).

A batch is the Messages API at half price and without the wait being yours: the requests
are submitted together, processed asynchronously (usually within the hour), and the
results read back by ``custom_id``, in any order. It fits work nobody is waiting on - an
eval sweep - and nothing interactive.

Two pieces:

- `MessageBatcher` is the wire: submit, poll until ``ended``, read the results.
- `DeferredClient` is what lets code written for `LLMClient.complete` run through a batch
  **unchanged**. It answers `complete` from the results it already holds, and a request it
  has no result for is queued and raises `PendingRequest`. The caller runs its work, lets
  the ones that raised wait, flushes the queue as one batch, and runs them again: each
  pass gets one call further, because every earlier call is now answered from the store.

  That replay is only sound because a request is identified by its content
  (`request_key`): the same arguments are the same request, whichever pass built them.
  So the work being replayed must be deterministic up to the model's replies, which the
  agents' forced-emit step is - the prompt is built from the query and the context, and a
  retry's feedback is rendered from the rejected payload. The validation-retry loop comes
  along for free: a refused report's re-request is simply the next pass's pending call.

Not batchable, and refused rather than approximated: streaming, and the tool loop (its
tools would run again on every pass).

**A batch outlives the process that submitted it.** The first live batch sat in the queue
for two hours and forty minutes, and the process polling it died on one connection error
after seventeen; the batch finished anyway and its 38 results waited on the server. So
polling tolerates a few failed polls in a row (`max_poll_failures`), the default patience
is the API's own maximum of a day, and a `DeferredClient` can `attach` to a batch it did
not submit in this process, so a run resumed after a crash reads what was paid for
instead of paying again. Whoever submits a batch is told its id at once (`on_submit`) so
it can be written down before anything else can go wrong.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import anthropic
from anthropic.types import Message, MessageParam
from pydantic import BaseModel

from .client import LLMClient
from .config import LLMSettings
from .tool_use import ToolLoopResult, ToolSpec


class BatchError(RuntimeError):
    """The batch could not give a request its answer: it errored, expired, was canceled,
    or never ended inside the timeout. Also raised for work a batch cannot carry."""


class PendingRequest(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised by `DeferredClient.complete` for a request that has no result yet. The
    request is already queued; the caller's job is to flush and run the work again."""

    def __init__(self, key: str) -> None:
        super().__init__(f"request {key} is queued for the next batch")
        self.key = key


def _jsonable(value: Any) -> Any:
    # An assistant turn replayed into `messages` carries the SDK's own content blocks.
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def request_key(params: Mapping[str, Any]) -> str:
    """A request's identity: the hash of its arguments in a canonical form. Doubles as
    the batch ``custom_id``, which the API limits to 64 characters of ``[A-Za-z0-9_-]``."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=_jsonable)
    return "req_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One request's outcome. Exactly one of ``message`` and ``error`` is set."""

    custom_id: str
    message: Message | None
    error: str | None


@dataclass(frozen=True, slots=True)
class BatchRun:
    """One submitted batch, for the record: what was sent and how it ended."""

    batch_id: str
    requests: int
    succeeded: int
    failed: int
    seconds: float


class MessageBatcher:
    """Submit, wait, read. The ``anthropic`` client is injected, like `LLMClient`'s, so
    the offline suite drives this against a scripted fake."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        poll_seconds: float = 20.0,
        timeout_seconds: float = 24 * 60 * 60,
        max_poll_failures: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_poll: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self._client = client
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._max_poll_failures = max_poll_failures
        self._sleep = sleep
        self._clock = clock
        self._on_poll = on_poll

    @classmethod
    def from_settings(cls, settings: LLMSettings | None = None, **kwargs: Any) -> MessageBatcher:
        settings = settings or LLMSettings()
        key = settings.anthropic_api_key
        client = anthropic.Anthropic(
            api_key=key.get_secret_value() if key is not None else None,
            timeout=settings.timeout_seconds,
        )
        return cls(client, **kwargs)

    def submit(self, requests: Mapping[str, Mapping[str, Any]]) -> str:
        """One batch of ``custom_id -> messages.create arguments``. Returns the batch id."""
        if not requests:
            raise ValueError("a batch needs at least one request")
        batch = self._client.messages.batches.create(
            requests=cast(
                "Any",
                [{"custom_id": cid, "params": dict(params)} for cid, params in requests.items()],
            )
        )
        return batch.id

    def wait(self, batch_id: str) -> Any:
        """Poll until the batch has ended. `BatchError` when it has not inside the timeout,
        or when the polls themselves keep failing - the batch is left running either way,
        and its id is in the message so it can be attached to later."""
        started = self._clock()
        failures = 0
        while True:
            elapsed = self._clock() - started
            try:
                batch = self._client.messages.batches.retrieve(batch_id)
            except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
                # One failed poll says nothing about the batch. Several in a row say the
                # API cannot be reached from here, and waiting longer will not change it.
                failures += 1
                if self._on_poll is not None:
                    self._on_poll(batch_id, f"poll failed ({type(exc).__name__})", elapsed)
                if failures >= self._max_poll_failures:
                    raise BatchError(
                        f"batch {batch_id}: {failures} polls in a row failed "
                        f"({type(exc).__name__}: {exc}); it is still running and its "
                        "results can be read later by that id"
                    ) from exc
                self._sleep(self._poll_seconds)
                continue
            failures = 0
            if self._on_poll is not None:
                self._on_poll(batch_id, batch.processing_status, elapsed)
            if batch.processing_status == "ended":
                return batch
            if elapsed >= self._timeout_seconds:
                raise BatchError(
                    f"batch {batch_id} had not ended after {elapsed:.0f}s "
                    f"(status {batch.processing_status!r}); it is still running and its "
                    "results can be read later by that id"
                )
            self._sleep(self._poll_seconds)

    def results(self, batch_id: str) -> dict[str, BatchResult]:
        """Every request's outcome, keyed by ``custom_id`` - results arrive in any order."""
        out: dict[str, BatchResult] = {}
        for entry in self._client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                out[entry.custom_id] = BatchResult(entry.custom_id, result.message, None)
            else:
                out[entry.custom_id] = BatchResult(entry.custom_id, None, _describe(result))
        return out

    def run(
        self,
        requests: Mapping[str, Mapping[str, Any]],
        *,
        on_submit: Callable[[str, int], None] | None = None,
    ) -> tuple[BatchRun, dict[str, BatchResult]]:
        """Submit, wait, read - and say what came back for every request that was sent.
        ``on_submit`` is told the batch id and size as soon as the batch exists."""
        started = self._clock()
        batch_id = self.submit(requests)
        if on_submit is not None:
            on_submit(batch_id, len(requests))
        return self.collect(batch_id, expected=list(requests), started=started)

    def collect(
        self, batch_id: str, *, expected: Sequence[str] = (), started: float | None = None
    ) -> tuple[BatchRun, dict[str, BatchResult]]:
        """Wait for a batch that already exists and read it. ``expected`` names the
        requests that must have a result; any missing gets one that says so."""
        began = self._clock() if started is None else started
        self.wait(batch_id)
        results = self.results(batch_id)
        for cid in expected:
            if cid not in results:
                results[cid] = BatchResult(cid, None, "the batch ended with no result for it")
        succeeded = sum(1 for r in results.values() if r.message is not None)
        run = BatchRun(
            batch_id=batch_id,
            requests=max(len(expected), len(results)),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            seconds=self._clock() - began,
        )
        return run, results


def _describe(result: Any) -> str:
    """`errored: invalid_request_error: ...`, or the bare type for expired / canceled."""
    error = getattr(result, "error", None)
    inner = getattr(error, "error", error)
    kind, message = getattr(inner, "type", None), getattr(inner, "message", None)
    detail = ": ".join(str(part) for part in (kind, message) if part)
    return f"{result.type}: {detail}" if detail else str(result.type)


class DeferredClient(LLMClient):
    """An `LLMClient` whose `complete` is answered by a batch (see the module docstring).

    Holds no API client and makes no call of its own: `flush` hands the queue to a
    `MessageBatcher`. Request shaping is inherited, so a deferred request is byte for
    byte the request `LLMClient.complete` would have sent.
    """

    def __init__(self, settings: LLMSettings | None = None) -> None:
        self.settings = settings or LLMSettings()
        self.results: dict[str, Message] = {}
        self.failed: dict[str, str] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.runs: list[BatchRun] = []

    def complete(
        self,
        *,
        messages: Sequence[MessageParam],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Message:
        params = self.request_params(
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        key = request_key(params)
        if key in self.results:
            return self.results[key]
        if key in self.failed:
            raise BatchError(f"batched request {key} did not succeed: {self.failed[key]}")
        self.pending[key] = params
        raise PendingRequest(key)

    def flush(
        self, batcher: MessageBatcher, *, on_submit: Callable[[str, int], None] | None = None
    ) -> BatchRun | None:
        """Send everything queued as one batch and keep the answers. ``None`` when the
        queue is empty - nothing is waiting, so there is nothing to submit."""
        if not self.pending:
            return None
        queued, self.pending = self.pending, {}
        run, results = batcher.run(queued, on_submit=on_submit)
        self._keep(run, results)
        return run

    def attach(self, batcher: MessageBatcher, batch_id: str) -> BatchRun:
        """Keep the answers of a batch submitted earlier - by a process that has since
        died, or by this one before a crash. A request's ``custom_id`` is its key, so the
        answers land exactly where a fresh submission's would."""
        run, results = batcher.collect(batch_id)
        self._keep(run, results)
        return run

    def _keep(self, run: BatchRun, results: Mapping[str, BatchResult]) -> None:
        for key, result in results.items():
            if result.message is not None:
                self.results[key] = result.message
            else:
                self.failed[key] = result.error or "no result"
        self.runs.append(run)

    def stream_text(self, **_kwargs: Any) -> Iterator[str]:
        raise BatchError("streaming cannot be batched - a batch has nobody to stream to")

    def run_tool_loop(self, **_kwargs: Any) -> ToolLoopResult:
        raise BatchError(
            "the tool loop cannot be batched - replaying it would run its tools again on "
            "every pass; batch the forced-emit agents (incident, docs) instead"
        )
