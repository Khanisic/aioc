"""Day 24: the orchestrator as a service - closed by default, jobs not long requests, and a
smoke request that runs the real path for free.

Offline: the app is built around an injected job queue whose pipeline is the shipped
`build_pipeline` over `NullTracer` and `MemoryAuditLog`, or a scripted stand-in, so
nothing here reaches the network, the database, or a model. The container rehearsal
(`docker compose --profile api up`) is the live counterpart.

What the tests hold:

- a URL that spends money is closed until opened twice: no token means every request is
  refused (503), a blank token is no token, a wrong one is 401, and a ``live`` request
  needs `AIOC_API_LIVE` as well (403);
- what the server caps it refuses rather than trims (rounds, extra fields);
- a request is a job: 202 with a Location, polled to its result, a failure recorded as
  the job's result and never the server's, a full queue refused with 429;
- a smoke request is the real `respond()` path - the contract response, both scripted
  agents run concurrently, the gate denying the scripted restart into the audit log -
  with no tokens spent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aioc import SCHEMA_VERSION
from aioc.api.app import create_app
from aioc.api.jobs import JobQueue, QueueFull
from aioc.api.pipeline import Outcome, RequestBody, build_pipeline
from aioc.api.readiness import Check, check_configured, check_database, check_prometheus
from aioc.api.settings import ApiSettings
from aioc.api.smoke import SmokeCoordinator, overlap_ms, smoke_plan, smoke_runners
from aioc.contracts import AgentName, CoordinatorResponse
from aioc.hitl import MemoryAuditLog
from aioc.llm import Usage
from aioc.observability.tracing import NullTracer

TOKEN = "test-token-not-a-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(**changes: Any) -> ApiSettings:
    base: dict[str, Any] = {"api_token": SecretStr(TOKEN), "live_enabled": False}
    base.update(changes)
    # Built by field name: nothing here may read the developer's `.env`.
    return ApiSettings.model_construct(
        **{**ApiSettings.model_construct().__dict__, **base}  # type: ignore[arg-type]
    )


def _ok_checks() -> list[Callable[[], Check]]:
    return [lambda: Check("database", True, True, "18 incidents in the corpus")]


def _smoke_queue(audit: MemoryAuditLog | None = None, **kwargs: Any) -> JobQueue:
    # `is None`, not `or`: an empty audit log has a length of 0 and is falsy.
    log = audit if audit is not None else MemoryAuditLog()
    pipeline = build_pipeline(NullTracer(), log, smoke_sleep_seconds=0.2)
    return JobQueue(pipeline, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(_settings(), queue=_smoke_queue(max_concurrent=2), checks=_ok_checks())
    with TestClient(app) as test_client:
        yield test_client


def _wait(client: TestClient, job_id: str, *, seconds: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/v1/requests/{job_id}", headers=AUTH).json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not finish in {seconds}s")


# =================================================================== open, and closed


def test_health_and_the_index_need_no_token(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
    index = client.get("/").json()
    assert index["schema_version"] == SCHEMA_VERSION and index["live_requests"] is False


def test_readiness_is_200_when_the_database_is_usable_and_503_when_not():
    for ok, code in ((True, 200), (False, 503)):
        checks: list[Callable[[], Check]] = [
            lambda ok=ok: Check("database", ok, True, "detail"),
            lambda: Check("langfuse", False, False, "LANGFUSE_* keys are not set: no traces"),
        ]
        app = create_app(_settings(), queue=_smoke_queue(), checks=checks)
        with TestClient(app) as test_client:
            reply = test_client.get("/readyz")
        assert reply.status_code == code
        assert reply.json()["ready"] is ok
        # An optional dependency that is down is reported and never fails readiness.
        assert reply.json()["checks"]["langfuse"] == {
            "ok": False,
            "required": False,
            "detail": "LANGFUSE_* keys are not set: no traces",
        }


def test_without_a_token_configured_every_request_is_refused():
    app = create_app(_settings(api_token=None), queue=_smoke_queue(), checks=_ok_checks())
    with TestClient(app) as test_client:
        reply = test_client.post("/v1/requests", json={"query": "q"}, headers=AUTH)
        assert reply.status_code == 503
        assert "AIOC_API_TOKEN is not set" in reply.json()["detail"]
        assert test_client.get("/v1/requests/job_x", headers=AUTH).status_code == 503


def test_a_blank_token_in_the_environment_is_no_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIOC_API_TOKEN", "  ")
    assert ApiSettings(_env_file=None).api_token is None  # type: ignore[call-arg]
    monkeypatch.setenv("AIOC_API_TOKEN", "chosen")
    token = ApiSettings(_env_file=None).api_token  # type: ignore[call-arg]
    assert token is not None and token.get_secret_value() == "chosen"


def test_the_service_is_closed_and_smoke_only_by_default(monkeypatch: pytest.MonkeyPatch):
    for name in ("AIOC_API_TOKEN", "AIOC_API_LIVE", "AIOC_API_MAX_ROUNDS"):
        monkeypatch.delenv(name, raising=False)
    settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_token is None and settings.live_enabled is False
    assert (settings.max_concurrent, settings.max_refinement_rounds) == (1, 0)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
def test_a_missing_or_wrong_token_is_401(client: TestClient, headers: dict[str, str]):
    reply = client.post("/v1/requests", json={"query": "q"}, headers=headers)
    assert reply.status_code == 401
    assert reply.headers["www-authenticate"] == "Bearer"
    assert client.get("/v1/requests/job_x", headers=headers).status_code == 401


def test_a_live_request_needs_the_live_switch(client: TestClient):
    reply = client.post("/v1/requests", json={"query": "q", "mode": "live"}, headers=AUTH)
    assert reply.status_code == 403
    assert "AIOC_API_LIVE" in reply.json()["detail"]


def test_a_live_request_runs_when_switched_on():
    seen: list[RequestBody] = []

    def pipeline(request: RequestBody) -> Outcome:
        seen.append(request)
        raise RuntimeError("scripted: no model here")

    settings = _settings(live_enabled=True, max_refinement_rounds=1)
    app = create_app(settings, queue=JobQueue(pipeline), checks=_ok_checks())
    body = {"query": "q", "mode": "live", "synthesis": "model", "max_refinement_rounds": 1}
    with TestClient(app) as test_client:
        reply = test_client.post("/v1/requests", json=body, headers=AUTH)
        assert reply.status_code == 202
        _wait(test_client, reply.json()["id"])
    (request,) = seen
    assert (request.mode, request.synthesis, request.max_refinement_rounds) == ("live", "model", 1)


def test_rounds_above_the_cap_are_refused_not_trimmed(client: TestClient):
    reply = client.post(
        "/v1/requests", json={"query": "q", "max_refinement_rounds": 1}, headers=AUTH
    )
    assert reply.status_code == 400 and "capped at 0" in reply.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"query": "q", "bogus": 1},
        {"query": ""},
        {"query": "q", "mode": "free"},
        {"query": "q", "max_refinement_rounds": 3},
        {},
    ],
)
def test_a_malformed_request_is_422(client: TestClient, body: dict[str, Any]):
    assert client.post("/v1/requests", json=body, headers=AUTH).status_code == 422


def test_an_unknown_job_is_404(client: TestClient):
    assert client.get("/v1/requests/job_nope", headers=AUTH).status_code == 404


# ======================================================================= the smoke path


def test_a_smoke_request_runs_the_real_path_for_free():
    audit = MemoryAuditLog()
    app = create_app(_settings(), queue=_smoke_queue(audit), checks=_ok_checks())
    with TestClient(app) as test_client:
        reply = test_client.post(
            "/v1/requests", json={"query": "checkout-api is failing"}, headers=AUTH
        )
        assert reply.status_code == 202
        job_id = reply.json()["id"]
        assert reply.headers["location"] == f"/v1/requests/{job_id}"
        assert reply.json()["status"] in ("queued", "running")
        job = _wait(test_client, job_id)

    assert job["status"] == "succeeded" and job["error"] is None
    response = CoordinatorResponse.model_validate(job["response"])  # the contract, unchanged
    assert response.status.value == "complete"
    assert [r.agent for r in response.agent_responses] == [AgentName.INCIDENT, AgentName.DOCS]
    assert {s.agent for s in response.skipped_agents} == {AgentName.GITHUB, AgentName.DEPLOYMENT}
    assert all(r.summary.startswith("SCRIPTED (smoke test)") for r in response.agent_responses)
    assert (response.cost.input_tokens, response.cost.output_tokens) == (0, 0)
    # The gate ran on the response and the decision is in the log it was given.
    (approval,) = job["approvals"]
    assert approval["decision"] == "denied"
    assert approval["request"]["action"].startswith("SCRIPTED (smoke test): restart")
    (logged,) = audit.decisions(request_id=response.request_id)
    assert logged.decision.value == "denied"
    assert job["trace_id"] is None and job["trace_url"] is None  # NullTracer


def test_the_smoke_agents_run_concurrently():
    windows: dict[str, tuple[float, float]] = {}
    runners = smoke_runners(windows, sleep_seconds=0.2)
    barrier = threading.Barrier(2, timeout=2)

    def run(agent: AgentName) -> None:
        barrier.wait()
        runners[agent].run(
            "q", context="c", request_id="req_1", invocation_id=agent.value, usage=Usage()
        )

    threads = [threading.Thread(target=run, args=(agent,)) for agent in runners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    overlap = overlap_ms(windows)
    assert overlap is not None and overlap > 0


def test_the_smoke_coordinator_never_builds_a_client():
    coordinator = SmokeCoordinator()
    assert not hasattr(coordinator, "_client")
    plan = coordinator.plan("anything at all", situation="ignored", usage=Usage())
    assert plan == smoke_plan("anything at all")
    assert {inv.agent for inv in plan.selected_agents} == {AgentName.INCIDENT, AgentName.DOCS}


# ============================================================================ the queue


def _blocking() -> tuple[Callable[[RequestBody], Outcome], threading.Event]:
    release = threading.Event()

    def pipeline(_request: RequestBody) -> Outcome:
        release.wait(5)
        raise RuntimeError("released")

    return pipeline, release


def test_a_full_queue_is_429():
    pipeline, release = _blocking()
    queue = JobQueue(pipeline, max_concurrent=1, max_pending=2)
    app = create_app(_settings(), queue=queue, checks=_ok_checks())
    try:
        with TestClient(app) as test_client:
            codes = [
                test_client.post("/v1/requests", json={"query": "q"}, headers=AUTH).status_code
                for _ in range(3)
            ]
            assert codes == [202, 202, 429]
            release.set()
    finally:
        release.set()


def test_a_failed_job_is_the_jobs_result_not_the_servers():
    def pipeline(_request: RequestBody) -> Outcome:
        raise ValueError("the planner refused:\n  a multi-line reason")

    app = create_app(_settings(), queue=JobQueue(pipeline), checks=_ok_checks())
    with TestClient(app) as test_client:
        job_id = test_client.post("/v1/requests", json={"query": "q"}, headers=AUTH).json()["id"]
        job = _wait(test_client, job_id)
        assert test_client.get("/healthz").status_code == 200
    assert job["status"] == "failed"
    assert job["error"] == "ValueError: the planner refused: a multi-line reason"
    assert job["response"] is None and job["approvals"] == []


def test_finished_jobs_are_evicted_oldest_first_and_waiting_ones_never():
    pipeline, release = _blocking()
    release.set()  # every job fails at once
    queue = JobQueue(pipeline, max_concurrent=1, max_pending=10, keep_finished=2)
    first = queue.submit(RequestBody(query="1"))
    deadline = time.monotonic() + 5
    while not first.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    jobs = [first] + [queue.submit(RequestBody(query=str(n))) for n in range(2, 5)]
    while not all(job.finished for job in jobs) and time.monotonic() < deadline:
        time.sleep(0.01)
    queue.submit(RequestBody(query="5"))  # eviction runs on submit
    assert queue.get(first.id) is None
    assert queue.get(jobs[-1].id) is not None
    queue.shutdown()


def test_the_queue_refuses_beyond_its_pending_limit():
    pipeline, release = _blocking()
    queue = JobQueue(pipeline, max_concurrent=1, max_pending=1)
    queue.submit(RequestBody(query="1"))
    with pytest.raises(QueueFull, match="1 request"):
        queue.submit(RequestBody(query="2"))
    release.set()
    queue.shutdown()


# ======================================================================== readiness


def test_a_database_that_cannot_be_reached_is_not_ready():
    check = check_database("postgresql://nobody:nothing@127.0.0.1:1/none", timeout_seconds=1)
    assert (check.ok, check.required) == (False, True)
    # psycopg names the failure by its subclass: refused, or timed out, by platform.
    assert check.detail.split(":")[0] in ("OperationalError", "ConnectionTimeout")


def test_prometheus_is_reported_never_required(monkeypatch: pytest.MonkeyPatch):
    def refused(url: str, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", refused)
    check = check_prometheus("http://prometheus:9090")
    assert (check.ok, check.required) == (False, False)
    assert check.detail == "http://prometheus:9090 unreachable (ConnectError)"

    monkeypatch.setattr(
        httpx, "get", lambda url, **_k: httpx.Response(200, request=httpx.Request("GET", url))
    )
    assert check_prometheus("http://prometheus:9090/").detail == (
        "http://prometheus:9090 answered 200"
    )


def test_a_key_is_reported_as_configured_and_never_tested():
    assert check_configured("anthropic", True, "missing").detail == "configured"
    unset = check_configured("anthropic", False, "ANTHROPIC_API_KEY is not set")
    assert (unset.ok, unset.required, unset.detail) == (
        False,
        False,
        "ANTHROPIC_API_KEY is not set",
    )


@pytest.mark.integration
def test_the_stack_database_is_ready():
    from aioc.tools.incident.store import dsn

    check = check_database(dsn())
    if not check.ok and "OperationalError" in check.detail:
        pytest.skip(f"database unreachable ({check.detail})")
    assert check.ok and check.detail.startswith("18 incidents in the corpus")


# ================================================================ the documented manifests


def test_the_kubernetes_manifests_match_the_service():
    """`infrastructure/k8s/` is never applied, so nothing else would notice it drift."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / "infrastructure" / "k8s"
    docs = {d["kind"]: d for d in yaml.safe_load_all((root / "aioc.yaml").read_text()) if d}
    app = create_app(_settings(), queue=_smoke_queue(), checks=_ok_checks())
    routes = {getattr(route, "path", None) for route in app.routes}

    (container,) = docs["Deployment"]["spec"]["template"]["spec"]["containers"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz" in routes
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz" in routes
    assert docs["Deployment"]["spec"]["replicas"] == 1  # jobs live in one process's memory
    (migrate,) = docs["Job"]["spec"]["template"]["spec"]["containers"]
    assert migrate["command"] == ["python", "-m", "aioc.db", "migrate"]

    # Every AIOC_API_* name in the ConfigMap is a setting the service reads, and every
    # switch that spends money is off.
    aliases = {field.validation_alias for field in ApiSettings.model_fields.values()}
    config = docs["ConfigMap"]["data"]
    assert {key for key in config if key.startswith("AIOC_API_")} <= aliases
    assert config["AIOC_API_LIVE"] == "false" and config["AIOC_API_MAX_ROUNDS"] == "0"
    secret = yaml.safe_load((root / "secret.example.yaml").read_text())
    assert "AIOC_API_TOKEN" in secret["stringData"] and not any(secret["stringData"].values())


def test_a_blank_key_is_reported_as_not_set(monkeypatch: pytest.MonkeyPatch):
    # The Day 25 fresh-clone test: `.env.example` ships `ANTHROPIC_API_KEY=` and the
    # LANGFUSE_* keys blank, compose's env_file hands the blanks to the container, and
    # /readyz said "configured" for keys that were empty strings.
    from aioc.api.readiness import default_checks
    from aioc.llm import LLMSettings
    from aioc.observability.tracing import TracingSettings
    from aioc.retrieval import EmbeddingSettings

    for name in (
        "ANTHROPIC_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "VOYAGE_API_KEY",
    ):
        monkeypatch.setenv(name, "  ")
    assert LLMSettings().anthropic_api_key is None
    assert not TracingSettings().configured
    assert EmbeddingSettings().voyage_api_key is None

    checks = {check.name: check for check in (run() for run in default_checks("x", "y")[1:3])}
    assert (
        not checks["anthropic"].ok and "ANTHROPIC_API_KEY is not set" in checks["anthropic"].detail
    )
    assert not checks["langfuse"].ok
