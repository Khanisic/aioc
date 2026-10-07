"""Requests as jobs: accepted at once, run in the background, read back by id (Day 24).

A live `respond()` takes minutes - the Day 21 four-agent run was about four - and a
hosting platform's proxy may cut an HTTP request long before that. So a request is
accepted with ``202`` and an id, runs on a bounded worker pool, and is polled with GET.
Nothing is lost by this that a synchronous call would have kept: the durable records of
a request are its trace and its audit-log rows, written as it runs; the job is only the
handle a client polls.

Bounded twice, because each running job is spend: ``max_concurrent`` workers, and
``max_pending`` jobs waiting or running before a new one is refused.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .pipeline import Outcome, RequestBody

Status = Literal["queued", "running", "succeeded", "failed"]
Pipeline = Callable[[RequestBody], Outcome]


class QueueFull(RuntimeError):  # noqa: N818 - names the condition the client sees
    """As many jobs are waiting or running as the service will commit to."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Job:
    id: str
    request: RequestBody
    submitted_at: datetime
    status: Status = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: Outcome | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "failed")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            outcome = self.outcome
            body: dict[str, Any] = {
                "id": self.id,
                "status": self.status,
                "mode": self.request.mode,
                "query": self.request.query,
                "submitted_at": _stamp(self.submitted_at),
                "started_at": _stamp(self.started_at),
                "finished_at": _stamp(self.finished_at),
                "error": self.error,
            }
        body.update(
            {
                "trace_id": None if outcome is None else outcome.response.trace_id,
                "trace_url": None if outcome is None else outcome.trace_url,
                "response": None if outcome is None else outcome.response.model_dump(mode="json"),
                "approvals": []
                if outcome is None
                else [decision.model_dump(mode="json") for decision in outcome.approvals],
            }
        )
        return body


class JobQueue:
    def __init__(
        self,
        pipeline: Pipeline,
        *,
        max_concurrent: int = 1,
        max_pending: int = 4,
        keep_finished: int = 100,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._pipeline = pipeline
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="aioc-job")
        self._max_pending = max_pending
        self._keep = keep_finished
        self._clock = clock
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def submit(self, request: RequestBody) -> Job:
        with self._lock:
            pending = sum(1 for job in self._jobs.values() if not job.finished)
            if pending >= self._max_pending:
                raise QueueFull(
                    f"{pending} request(s) are already waiting or running; try again later"
                )
            job = Job(
                id=f"job_{uuid.uuid4().hex[:12]}", request=request, submitted_at=self._clock()
            )
            self._jobs[job.id] = job
            self._evict()
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _run(self, job: Job) -> None:
        with job._lock:  # noqa: SLF001 - the queue owns its jobs' lifecycle
            job.status = "running"
            job.started_at = self._clock()
        try:
            outcome = self._pipeline(job.request)
        except Exception as exc:  # noqa: BLE001 - a job's failure is its result, never the server's
            with job._lock:  # noqa: SLF001
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {' '.join(str(exc).split())}"
                job.finished_at = self._clock()
            return
        with job._lock:  # noqa: SLF001
            job.status = "succeeded"
            job.outcome = outcome
            job.finished_at = self._clock()

    def _evict(self) -> None:
        # Oldest finished first; a job still waiting or running is never dropped.
        finished = [job_id for job_id, job in self._jobs.items() if job.finished]
        for job_id in finished[: max(0, len(finished) - self._keep)]:
            del self._jobs[job_id]
