"""AIOC demo app service (Day 3).

One parameterized FastAPI service, run three times from the same image:

- ``checkout-api``  - the front service; each /process call fans out to its downstreams
  and it generates its own steady background traffic so every service emits moving
  metrics without an external load generator.
- ``payments-api``  - leaf downstream.
- ``inventory-api`` - leaf downstream.

Everything is driven by environment variables (see docker-compose.yml):

    SERVICE_NAME          logical name, used as the ``service`` metric label
    SERVICE_PORT          listen port (default 8000; compose maps host ports onto it)
    DOWNSTREAM_URLS       comma-separated base URLs this service calls per /process
    SELF_TRAFFIC_SECONDS  >0 enables the background traffic loop at that interval
    DEMO_GIT_SHA          fake "deployed commit" reported by /version - Day 4's
                          code_regression failure mode ties a 500-spike to this value

The ``/_chaos`` knobs are the service-side surface Day 4's ``demo-app/chaos/inject.py``
drives. Day 3 ships them healthy-by-default and reversible (POST {"reset": true}); the
four named FailureMode scripts land on Day 4 and are compositions of these knobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = os.environ.get("SERVICE_NAME", "demo-service")
PORT = int(os.environ.get("SERVICE_PORT", "8000"))
DOWNSTREAMS = [
    url.strip() for url in os.environ.get("DOWNSTREAM_URLS", "").split(",") if url.strip()
]
SELF_TRAFFIC_SECONDS = float(os.environ.get("SELF_TRAFFIC_SECONDS", "0"))
GIT_SHA = os.environ.get("DEMO_GIT_SHA", "baseline")

# ----------------------------------------------------------------------------- metrics

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by service, path, method, and status code.",
    ["service", "path", "method", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by service and path.",
    ["service", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
CHAOS_KNOB = Gauge(
    "chaos_knob_value",
    "Current value of each chaos knob - lets the eval compare agent output to injected truth.",
    ["service", "knob"],
)

# ----------------------------------------------------------------------------- chaos knobs

_HEALTHY: dict[str, float] = {
    "extra_latency_ms": 0.0,  # added to every /process call (downstream_latency)
    "error_rate": 0.0,  # probability a /process call returns 500 (code_regression)
    "leak_mb_per_request": 0.0,  # bytes retained per /process call (resource_exhaustion)
}
_chaos: dict[str, float] = dict(_HEALTHY)
_leaked: list[bytearray] = []


def _set_chaos(updates: dict[str, float]) -> None:
    for knob, value in updates.items():
        _chaos[knob] = float(value)
    if _chaos["leak_mb_per_request"] == 0.0:
        _leaked.clear()
    for knob, value in _chaos.items():
        CHAOS_KNOB.labels(service=SERVICE, knob=knob).set(value)


_set_chaos({})  # publish healthy baseline gauges at startup

# ----------------------------------------------------------------------------- app

_http: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _http
    _http = httpx.AsyncClient(timeout=10.0)
    traffic: asyncio.Task[None] | None = None
    if SELF_TRAFFIC_SECONDS > 0:
        traffic = asyncio.create_task(_self_traffic())
    yield
    if traffic is not None:
        traffic.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await traffic
    await _http.aclose()


app = FastAPI(title=SERVICE, lifespan=_lifespan)


async def _self_traffic() -> None:
    """Hit our own /process on an interval so the whole call tree emits steady metrics."""
    assert _http is not None
    while True:
        await asyncio.sleep(SELF_TRAFFIC_SECONDS)
        with contextlib.suppress(httpx.HTTPError):
            await _http.get(f"http://localhost:{PORT}/process")


@app.middleware("http")
async def _observe(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    if path in ("/metrics", "/healthz"):
        return await call_next(request)
    started = time.monotonic()
    response = await call_next(request)
    elapsed = time.monotonic() - started
    REQUESTS.labels(
        service=SERVICE, path=path, method=request.method, status=str(response.status_code)
    ).inc()
    LATENCY.labels(service=SERVICE, path=path).observe(elapsed)
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE}


@app.get("/version")
async def version() -> dict[str, str]:
    return {"service": SERVICE, "git_sha": GIT_SHA}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/process")
@app.post("/process")
async def process() -> Response:
    """The one unit of business work. Front services fan out to their downstreams."""
    if _chaos["extra_latency_ms"] > 0:
        await asyncio.sleep(_chaos["extra_latency_ms"] / 1000.0)
    if _chaos["leak_mb_per_request"] > 0:
        _leaked.append(bytearray(int(_chaos["leak_mb_per_request"] * 1024 * 1024)))
    # noqa on S311: chaos sampling, not cryptography.
    if _chaos["error_rate"] > 0 and random.random() < _chaos["error_rate"]:  # noqa: S311
        return JSONResponse(
            status_code=500,
            content={"service": SERVICE, "error": "internal error", "git_sha": GIT_SHA},
        )

    downstream_results: dict[str, str] = {}
    assert _http is not None
    for base in DOWNSTREAMS:
        try:
            resp = await _http.get(f"{base}/process")
            ok = resp.status_code == 200
            downstream_results[base] = "ok" if ok else f"http {resp.status_code}"
        except httpx.HTTPError as exc:
            downstream_results[base] = f"unreachable: {type(exc).__name__}"

    degraded = [base for base, status in downstream_results.items() if status != "ok"]
    return JSONResponse(
        status_code=502 if degraded else 200,
        content={
            "service": SERVICE,
            "status": "degraded" if degraded else "ok",
            "downstreams": downstream_results,
        },
    )


@app.get("/_chaos")
async def chaos_state() -> dict[str, float]:
    return dict(_chaos)


@app.post("/_chaos", response_model=None)
async def chaos_set(updates: dict[str, float | bool]) -> Response:
    """Set chaos knobs, or reset to the healthy baseline with {"reset": true}.

    Unknown knob names are rejected so a Day 4 inject.py typo fails loudly instead of
    silently injecting nothing.
    """
    if updates.pop("reset", False):
        _set_chaos(dict(_HEALTHY))
        return JSONResponse(content=dict(_chaos))
    unknown = set(updates) - set(_HEALTHY)
    if unknown:
        return JSONResponse(
            status_code=400,
            content={"error": f"unknown chaos knobs: {sorted(unknown)}", "known": sorted(_HEALTHY)},
        )
    _set_chaos({knob: float(value) for knob, value in updates.items()})
    return JSONResponse(content=dict(_chaos))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)  # noqa: S104 - container-internal bind
