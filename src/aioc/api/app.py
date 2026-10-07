"""The HTTP surface (Day 24): health, readiness, and requests as jobs.

    uvicorn --factory aioc.api.app:create_app --host 0.0.0.0 --port 8080

| Route | Auth | What it does |
|---|---|---|
| ``GET /`` | none | what this is, and the contract version it speaks |
| ``GET /healthz`` | none | the process is up |
| ``GET /readyz`` | none | what it is connected to; 503 when the database is not usable |
| ``POST /v1/requests`` | bearer | accept a request as a job: 202 and its id |
| ``GET /v1/requests/{id}`` | bearer | the job: its status, then its response and trace |

A request body is `RequestBody`: the query, the optional situation, ``mode`` (``smoke``,
the default and free, or ``live``), the synthesis, and the refinement rounds. The service
is closed until `AIOC_API_TOKEN` is set, and ``live`` needs `AIOC_API_LIVE` as well
(`ApiSettings`). The response body is the frozen contract's `CoordinatorResponse`
unchanged; the gate's decisions (``approvals``) and the trace link ride beside it, never
inside it, as everywhere else.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from aioc import SCHEMA_VERSION

from .jobs import JobQueue, QueueFull
from .pipeline import RequestBody
from .readiness import Check, default_checks, readiness
from .settings import ApiSettings

_bearer = HTTPBearer(auto_error=False)


def _default_queue(settings: ApiSettings) -> JobQueue:
    # Runtime imports: building these reads `.env` and constructs clients, which an app
    # given its own queue (every test) must never do.
    from aioc.hitl.audit import default_audit_log
    from aioc.observability.tracing import default_tracer

    from .pipeline import build_pipeline

    return JobQueue(
        build_pipeline(default_tracer(), default_audit_log()),
        max_concurrent=settings.max_concurrent,
        max_pending=settings.max_pending,
        keep_finished=settings.keep_finished,
    )


def _default_checks() -> list[Callable[[], Check]]:
    import os

    from aioc.observability.prometheus import DEFAULT_URL
    from aioc.tools.incident.store import dsn

    return default_checks(dsn(), os.environ.get("PROMETHEUS_URL", DEFAULT_URL))


def create_app(
    settings: ApiSettings | None = None,
    *,
    queue: JobQueue | None = None,
    checks: list[Callable[[], Check]] | None = None,
) -> FastAPI:
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.queue = queue if queue is not None else _default_queue(settings)
        app.state.checks = checks if checks is not None else _default_checks()
        try:
            yield
        finally:
            app.state.queue.shutdown()

    app = FastAPI(
        title="AIOC",
        summary="Enterprise AI Operations Center: a coordinator over four subagents.",
        version=SCHEMA_VERSION,
        lifespan=lifespan,
    )

    def authorised(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if settings.api_token is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "requests are disabled on this deployment: AIOC_API_TOKEN is not set",
            )
        expected = settings.api_token.get_secret_value().encode()
        given = b"" if credentials is None else credentials.credentials.encode()
        if credentials is None or not secrets.compare_digest(given, expected):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "a bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # Route-level, not a parameter: an annotation defined in this function cannot be
    # resolved under postponed evaluation, and FastAPI then reads the parameter as a
    # required query string (a 422 on every request, found by the container rehearsal).
    auth = [Depends(authorised)]

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "service": "aioc",
            "schema_version": SCHEMA_VERSION,
            "live_requests": settings.live_enabled,
            "routes": ["/healthz", "/readyz", "/v1/requests", "/v1/requests/{id}", "/docs"],
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        ready, body = readiness(request.app.state.checks)
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.post("/v1/requests", status_code=status.HTTP_202_ACCEPTED, dependencies=auth)
    def submit(body: RequestBody, request: Request) -> JSONResponse:
        if body.mode == "live" and not settings.live_enabled:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "live requests are off on this deployment (AIOC_API_LIVE); "
                "a smoke request runs the same path for free",
            )
        if body.max_refinement_rounds > settings.max_refinement_rounds:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"max_refinement_rounds is capped at {settings.max_refinement_rounds} "
                "on this deployment (AIOC_API_MAX_ROUNDS)",
            )
        try:
            job = request.app.state.queue.submit(body)
        except QueueFull as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        return JSONResponse(
            job.to_dict(),
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Location": f"/v1/requests/{job.id}"},
        )

    @app.get("/v1/requests/{job_id}", dependencies=auth)
    def read(job_id: str, request: Request) -> dict[str, Any]:
        job = request.app.state.queue.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no request {job_id} here")
        result: dict[str, Any] = job.to_dict()
        return result

    return app
