"""Tests for the deployment tool server (Day 12): `diff_release` and `check_rollout_health`.

All offline. GitHub is played by an `httpx.MockTransport` serving a two-ref repository whose
compose file, `.env.example`, and a Kubernetes manifest differ between the refs; Prometheus
is played by another `MockTransport` answering each battery query from a scripted scenario.
Under test: the envelope and meta, the structural keys-only diff, the `include` filter, the
sec 7.3 / 7.4 error codes from real code paths (all four classes distinctly), the
deterministic status rule, null-never-zero, the chaos gate, the four-part description
template, and the enum copies. No key, no network, no Docker.
"""

from __future__ import annotations

import ast
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import types

from aioc.contracts import Environment, RolloutStatus
from aioc.tools.deployment import health, release
from aioc.tools.deployment import server as ds
from aioc.tools.github.api import GitHubApi, GitHubSettings

# --------------------------------------------------------------------------- helpers


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    payload = json.loads(block.text)
    assert payload["ok"] is (not result.isError)
    return payload


_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_NOW_EPOCH = _NOW.timestamp()

# ------------------------------------------------------------- the two-ref repository

V1, V2 = "v1.4.2", "v1.4.3"
SHA_V1 = "a" * 40
SHA_V2 = "b" * 40

COMPOSE_V1 = """\
services:
  checkout-api:
    image: aioc-demo-service:day3
    environment:
      SERVICE_NAME: checkout-api
      DB_POOL_MAX: "10"
      DB_PASSWORD: super-secret-value-one
    ports:
      - "8001:8000"
  payments-api:
    image: aioc-demo-service:day3
    environment:
      SERVICE_NAME: payments-api
"""

_DIGEST = "1" * 64

COMPOSE_V2 = f"""\
services:
  checkout-api:
    image: aioc-demo-service:day4@sha256:{_DIGEST}
    environment:
      SERVICE_NAME: checkout-api
      DB_POOL_MAX: "20"
      DB_POOL_TIMEOUT_MS: "5000"
    ports:
      - "8001:8000"
    deploy:
      replicas: 2
  payments-api:
    image: aioc-demo-service:day3
    environment:
      SERVICE_NAME: payments-api
"""

ENV_V1 = "POSTGRES_PORT=5432\nFEATURE_FLAG_X=on\n# comment\n"
ENV_V2 = "POSTGRES_PORT=55432\nexport NEW_SHARED_KEY=value-two\n"

MANIFEST_V1 = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-api
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: ghcr.io/aioc/checkout:1.4.2
          env:
            - name: LOG_LEVEL
              value: INFO
          resources:
            limits:
              memory: 256Mi
---
apiVersion: v1
kind: Service
metadata:
  name: checkout-api
spec:
  ports:
    - port: 80
"""

MANIFEST_V2 = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-api
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: app
          image: ghcr.io/aioc/checkout:1.4.3
          env:
            - name: LOG_LEVEL
              value: DEBUG
          resources:
            limits:
              memory: 512Mi
---
apiVersion: v1
kind: Service
metadata:
  name: checkout-api
spec:
  ports:
    - port: 8080
"""

_FILES = {
    V1: {
        "docker-compose.yml": COMPOSE_V1,
        ".env.example": ENV_V1,
        "infrastructure/k8s/checkout-api.yaml": MANIFEST_V1,
        "README.md": "old",
    },
    V2: {
        "docker-compose.yml": COMPOSE_V2,
        ".env.example": ENV_V2,
        "infrastructure/k8s/checkout-api.yaml": MANIFEST_V2,
        "README.md": "new",
    },
}
_SHAS = {V1: SHA_V1, V2: SHA_V2}

_COMMITS = [
    {
        "sha": SHA_V2,
        "commit": {
            # A body is where a value hides in prose: the tool must not return it.
            "message": (
                "Raise the pool ceiling and add a pool timeout\n\n"
                "DB_POOL_MAX goes from 20 to 40 and DB_POOL_TIMEOUT_MS=2500 is new."
            ),
            "author": {"date": "2026-09-08T10:00:00Z"},
        },
    }
]


def _contents(text: str) -> dict[str, Any]:
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def _repo(request: httpx.Request) -> httpx.Response:
    """GitHub, for the two-ref repository above."""
    path = request.url.path
    if path.startswith("/repos/o/r/commits/"):
        ref = path.rsplit("/", 1)[1]
        if ref in _SHAS:
            return httpx.Response(200, json={"sha": _SHAS[ref]})
        return httpx.Response(404, json={"message": "Not Found"})
    if path.startswith("/repos/o/r/compare/"):
        files = [
            {"filename": name, "status": "modified"}
            for name in _FILES[V2]
            if _FILES[V1].get(name) != _FILES[V2].get(name)
        ]
        return httpx.Response(
            200, json={"files": files, "commits": _COMMITS, "total_commits": len(_COMMITS)}
        )
    if path.startswith("/repos/o/r/contents/"):
        name = path[len("/repos/o/r/contents/") :]
        ref = request.url.params.get("ref", "")
        text = _FILES.get(ref, {}).get(name)
        if text is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=_contents(text))
    return httpx.Response(500, json={"message": f"unexpected {path}"})


def _api(handler: Any = _repo, *, token: str | None = "ghp_test") -> GitHubApi:
    settings = GitHubSettings(token=token, repository="o/r", api_url="https://api.test")  # type: ignore[arg-type]
    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return GitHubApi(settings, client=client)


def _diff(**args: Any) -> dict[str, Any]:
    params = {"service": "checkout-api", "from_version": V1, "to_version": V2, **args}
    return _payload(ds.call(ds.DIFF_RELEASE, params, api=_api()))


# ------------------------------------------------------------------------ diff_release


def test_diff_release_returns_the_contract_shape_with_meta():
    payload = _diff()
    data, meta = payload["data"], payload["meta"]
    assert set(data) == {
        "service",
        "from_version",
        "to_version",
        "config_keys_added",
        "config_keys_removed",
        "config_keys_changed",
        "image_changes",
        "commits",
        "manifest_changes",
    }
    assert data["service"] == "checkout-api"
    assert data["from_version"] == V1 and data["to_version"] == V2
    assert meta["truncated"] is False and meta["source"] == "github"
    assert meta["returned"] > 0 and meta["token_estimate"] > 0 and meta["query_ms"] >= 0
    assert meta["as_of"].endswith("Z")


def test_config_keys_are_diffed_structurally_across_compose_env_and_manifests():
    data = _diff()["data"]
    # compose environment + env file + k8s env names, keys only
    assert data["config_keys_added"] == ["DB_POOL_TIMEOUT_MS", "NEW_SHARED_KEY"]
    assert data["config_keys_removed"] == ["DB_PASSWORD", "FEATURE_FLAG_X"]
    assert data["config_keys_changed"] == ["DB_POOL_MAX", "LOG_LEVEL", "POSTGRES_PORT"]


def test_config_values_never_appear_anywhere_in_the_response():
    """The sec 4.4 rule at the source: values are hashed as they are parsed. The check is
    over the whole serialised payload, not a field, because a leak through `commits` or
    `manifest_changes` would be the same leak."""
    result = ds.call(
        ds.DIFF_RELEASE,
        {"service": "checkout-api", "from_version": V1, "to_version": V2},
        api=_api(),
    )
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    for value in ("super-secret-value-one", "value-two", "55432", "5000", "DEBUG", "512Mi"):
        assert value not in block.text, value


def test_image_changes_carry_container_from_to_and_digests():
    images = {i["container"]: i for i in _diff()["data"]["image_changes"]}
    assert set(images) == {"checkout-api", "app"}
    compose = images["checkout-api"]
    assert compose["from_image"] == "aioc-demo-service:day3"
    assert compose["to_image"] == "aioc-demo-service:day4"
    assert compose["from_digest"] is None
    assert compose["to_digest"] == "sha256:" + "1" * 64
    k8s = images["app"]
    assert (k8s["from_image"], k8s["to_image"]) == (
        "ghcr.io/aioc/checkout:1.4.2",
        "ghcr.io/aioc/checkout:1.4.3",
    )


def test_manifest_changes_are_dotted_paths_only():
    paths = _diff()["data"]["manifest_changes"]
    assert "services.checkout-api.deploy.replicas" in paths  # added in v2
    assert "spec.template.spec.containers[0].resources.limits.memory" in paths
    assert "service.spec.ports[0].port" in paths  # non-workload kinds carry their kind
    assert "spec.replicas" not in paths  # unchanged
    assert "services.checkout-api.image" not in paths  # reported under image_changes
    assert all("=" not in p and " " not in p for p in paths)


def test_commits_between_the_releases_are_reported():
    commits = _diff()["data"]["commits"]
    assert commits == [
        {
            "sha": SHA_V2,
            "message": "Raise the pool ceiling and add a pool timeout",
            "authored_at": "2026-09-08T10:00:00Z",
        }
    ]


def test_a_commit_is_its_subject_line_so_a_value_in_its_body_never_leaves():
    reply = json.dumps(_diff())
    assert "Raise the pool ceiling" in reply
    assert "2500" not in reply and "goes from 20 to 40" not in reply


def test_a_service_named_in_no_manifest_still_gets_the_commits():
    data = _diff(service="ghost-api")["data"]
    assert data["config_keys_changed"] == ["POSTGRES_PORT"]  # shared env file still applies
    assert data["image_changes"] == [] and data["manifest_changes"] == []
    assert len(data["commits"]) == 1


@pytest.mark.parametrize(
    ("include", "computed"),
    [
        ("config", {"config_keys_added", "config_keys_removed", "config_keys_changed"}),
        ("images", {"image_changes"}),
        ("manifests", {"manifest_changes"}),
        ("commits", {"commits"}),
    ],
)
def test_include_leaves_the_other_sections_null_not_empty(include: str, computed: set[str]):
    data = _diff(include=include)["data"]
    sections = {
        "config_keys_added",
        "config_keys_removed",
        "config_keys_changed",
        "image_changes",
        "manifest_changes",
        "commits",
    }
    for section in sections:
        if section in computed:
            assert data[section] is not None, section
        else:
            assert data[section] is None, section  # not computed, as distinct from empty


def test_include_commits_reads_no_file_contents():
    seen: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _repo(request)

    ds.call(
        ds.DIFF_RELEASE,
        {"service": "checkout-api", "from_version": V1, "to_version": V2, "include": "commits"},
        api=_api(spy),
    )
    assert not any("/contents/" in p for p in seen)


def test_same_version_is_a_validation_error_not_an_empty_diff():
    payload = _payload(
        ds.call(ds.DIFF_RELEASE, {"service": "s", "from_version": V1, "to_version": V1})
    )
    error = payload["error"]
    assert error["class"] == "validation" and error["code"] == "SAME_VERSION"
    assert error["details"]["field"] == "to_version" and error["retryable"] is False


def test_an_unknown_release_is_a_business_error_naming_which_ref():
    payload = _payload(
        ds.call(
            ds.DIFF_RELEASE,
            {"service": "s", "from_version": V1, "to_version": "v9.9.9"},
            api=_api(),
        )
    )
    error = payload["error"]
    assert error["class"] == "business" and error["code"] == "UNKNOWN_RELEASE_VERSION"
    assert error["details"]["version"] == "v9.9.9"
    assert "check_rollout_health" in error["remediation"]


def test_a_missing_token_is_registry_scope_missing():
    payload = _payload(
        ds.call(
            ds.DIFF_RELEASE,
            {"service": "s", "from_version": V1, "to_version": V2},
            api=_api(token=None),
        )
    )
    error = payload["error"]
    assert error["class"] == "permission" and error["code"] == "REGISTRY_SCOPE_MISSING"
    assert "required_scope" in error["details"]


def test_github_down_is_registry_unavailable_and_retryable():
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    payload = _payload(
        ds.call(
            ds.DIFF_RELEASE,
            {"service": "s", "from_version": V1, "to_version": V2},
            api=_api(down),
        )
    )
    error = payload["error"]
    assert error["class"] == "transient" and error["code"] == "REGISTRY_UNAVAILABLE"
    assert error["retryable"] is True and error["retry_after_ms"] >= 1000


def test_missing_repository_is_a_validation_error_naming_the_variable():
    api = GitHubApi(
        GitHubSettings(token="ghp_x", repository=None, api_url="https://api.test")  # type: ignore[arg-type]
    )
    payload = _payload(
        ds.call(ds.DIFF_RELEASE, {"service": "s", "from_version": V1, "to_version": V2}, api=api)
    )
    assert payload["error"]["code"] == "REPOSITORY_NOT_CONFIGURED"
    assert payload["error"]["details"]["field"] == "GITHUB_REPO"


def test_more_release_files_than_the_cap_is_flagged_truncated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ds, "MAX_RELEASE_FILES", 1)
    assert _diff()["meta"]["truncated"] is True


# --------------------------------------------------------- release.py (pure parsing)


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("docker-compose.yml", "compose"),
        ("deploy/compose.prod.yaml", "compose"),
        (".env.example", "env"),
        ("config/app.env", "env"),
        ("infrastructure/k8s/checkout.yaml", "manifest"),
        ("k8s/base/deployment.yml", "manifest"),
        ("src/aioc/agents/deployment.py", None),
        ("docs/CONTRACTS.md", None),
        ("tests/fixtures/sample.yaml", None),
    ],
)
def test_classify_path(path: str, kind: str | None):
    assert release.classify_path(path) == kind


def test_compose_env_lists_and_maps_both_yield_keys():
    as_list = "services:\n  s:\n    environment:\n      - A=1\n      - B\n"
    as_map = "services:\n  s:\n    environment:\n      A: 1\n      B:\n"
    assert set(release.parse_compose(as_list, "s").config) == {"A", "B"}
    assert set(release.parse_compose(as_map, "s").config) == {"A", "B"}


def test_a_reindented_manifest_is_not_a_change():
    import yaml

    before = release.parse_compose(COMPOSE_V1, "checkout-api")
    reindented = yaml.safe_dump(yaml.safe_load(COMPOSE_V1), indent=4, sort_keys=True)
    assert reindented != COMPOSE_V1  # a patch-based diff would see every line change
    after = release.parse_compose(reindented, "checkout-api")
    diff = release.diff_snapshots(before, after)
    assert all(not v for v in diff.values())


def test_an_unparsable_manifest_contributes_nothing_rather_than_raising():
    assert release.parse_compose("services: [unclosed", "s") == release.ReleaseSnapshot()
    assert release.parse_manifest(": : :", "s") == release.ReleaseSnapshot()


def test_split_digest():
    assert release.split_digest("img:tag") == ("img:tag", None)
    assert release.split_digest("img:tag@sha256:" + "f" * 64) == ("img:tag", "sha256:" + "f" * 64)


def test_snapshot_holds_hashes_not_values():
    snap = release.parse_env_file("SECRET=hunter2\n")
    assert "hunter2" not in repr(snap)
    assert len(snap.config["SECRET"]) == 64


# ------------------------------------------------------------------- check_rollout_health


def _vector(*rows: tuple[dict[str, str], float]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [_NOW_EPOCH, str(value)]} for labels, value in rows
            ],
        },
    }


class _Prom:
    """A scripted Prometheus. Each scenario knob maps onto the battery query it answers."""

    def __init__(
        self,
        *,
        instances: dict[str, float] | None = None,  # instance -> up
        current: dict[str, str] | None = None,  # instance -> git_sha (build_info now)
        seen: dict[str, float] | None = None,  # git_sha -> last-seen epoch
        error_rate: float | None = 0.0,
        p50_s: float | None = 0.040,
        p99_s: float | None = 0.120,
        restarts: float | None = 0.0,
        probe_failures: float | None = 0.0,
        baseline_error_rate: float | None = 0.0,
        baseline_p99_s: float | None = 0.100,
    ) -> None:
        self.instances = (
            instances
            if instances is not None
            else {"checkout-api:8000": 1.0, "payments-api:8000": 1.0, "inventory-api:8000": 1.0}
        )
        self.current = current if current is not None else {"checkout-api:8000": "v2sha"}
        self.seen = seen if seen is not None else {"v2sha": _NOW_EPOCH - 5}
        self.error_rate, self.p50_s, self.p99_s = error_rate, p50_s, p99_s
        self.restarts, self.probe_failures = restarts, probe_failures
        self.baseline_error_rate, self.baseline_p99_s = baseline_error_rate, baseline_p99_s
        self.queries: list[tuple[str, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        at = request.url.params.get("time")
        self.queries.append((query, at))
        historical = at is not None and at != health._stamp(_NOW)
        if query.startswith("up{"):
            return httpx.Response(
                200, json=_vector(*[({"instance": i}, v) for i, v in self.instances.items()])
            )
        if query.startswith("timestamp(last_over_time(service_build_info"):
            return httpx.Response(
                200, json=_vector(*[({"git_sha": sha}, ts) for sha, ts in self.seen.items()])
            )
        if query.startswith("service_build_info"):
            return httpx.Response(
                200,
                json=_vector(
                    *[({"instance": i, "git_sha": sha}, 1.0) for i, sha in self.current.items()]
                ),
            )
        if "status=~" in query:
            value = self.baseline_error_rate if historical else self.error_rate
            return httpx.Response(200, json=_vector(*([({}, value)] if value is not None else [])))
        if "histogram_quantile(0.5" in query:
            return httpx.Response(
                200, json=_vector(*([({}, self.p50_s)] if self.p50_s is not None else []))
            )
        if "histogram_quantile(0.99" in query:
            value = self.baseline_p99_s if historical else self.p99_s
            return httpx.Response(200, json=_vector(*([({}, value)] if value is not None else [])))
        if "process_start_time_seconds" in query:
            return httpx.Response(
                200, json=_vector(*([({}, self.restarts)] if self.restarts is not None else []))
            )
        if "count_over_time(up" in query:
            value = self.probe_failures
            return httpx.Response(200, json=_vector(*([({}, value)] if value is not None else [])))
        return httpx.Response(400, json={"status": "error", "error": f"unscripted: {query}"})


def _reader(prom: Any) -> health.PrometheusReader:
    client = httpx.Client(transport=httpx.MockTransport(prom))
    return health.PrometheusReader("http://prom:9090", client=client)


def _health(prom: Any = None, *, env: str = "development", **args: Any) -> dict[str, Any]:
    prom = prom if prom is not None else _Prom()
    params = {"service": "checkout-api", "environment": "development", **args}
    settings = ds.DeploymentSettings(environment=env)  # type: ignore[call-arg]
    return _payload(
        ds.call(ds.CHECK_ROLLOUT_HEALTH, params, reader=_reader(prom), settings=settings, now=_NOW)
    )


def test_check_rollout_health_returns_the_contract_shape_with_meta():
    payload = _health()
    data, meta = payload["data"], payload["meta"]
    assert set(data) == {
        "service",
        "environment",
        "version",
        "status",
        "replicas",
        "signals",
        "compared_to_baseline",
    }
    assert set(data["replicas"]) == {"desired", "ready", "updated", "unavailable"}
    assert set(data["signals"]) == {
        "error_rate",
        "p50_latency_ms",
        "p99_latency_ms",
        "restart_count",
        "probe_failures",
    }
    assert data["status"] in ds.ROLLOUT_STATUSES
    assert meta["source"] == "prometheus" and meta["returned"] == 1 and meta["truncated"] is False
    assert meta["as_of"] == "2026-09-09T12:00:00Z"


def test_a_healthy_rollout():
    data = _health()["data"]
    assert data["version"] == "v2sha" and data["status"] == "healthy"
    assert data["replicas"] == {"desired": 1, "ready": 1, "updated": 1, "unavailable": 0}
    assert data["signals"] == {
        "error_rate": 0.0,
        "p50_latency_ms": 40.0,
        "p99_latency_ms": 120.0,
        "restart_count": 0,
        "probe_failures": 0,
    }
    assert data["compared_to_baseline"] is None  # no previous version in the window


def test_traffic_with_no_5xx_is_zero_and_no_traffic_is_null():
    """The or-trick, end to end: the scripted zero is a measured zero; an absent series is
    null. A `0` where nothing was measured would be a fabricated measurement."""
    assert _health(_Prom(error_rate=0.0))["data"]["signals"]["error_rate"] == 0.0
    quiet = _health(_Prom(error_rate=None, p50_s=None, p99_s=None))["data"]
    assert quiet["signals"]["error_rate"] is None
    assert quiet["signals"]["p50_latency_ms"] is None and quiet["signals"]["p99_latency_ms"] is None
    assert quiet["status"] == "healthy"  # restarts and probes were measured and are fine


def test_nothing_measurable_is_unknown_not_healthy():
    prom = _Prom(error_rate=None, p50_s=None, p99_s=None, restarts=None, probe_failures=None)
    assert _health(prom)["data"]["status"] == "unknown"


@pytest.mark.parametrize(
    "knobs",
    [
        {"error_rate": 0.31},
        {"error_rate": 0.05},  # at the threshold
        {"restarts": 1.0},
        {"probe_failures": 3.0},
    ],
)
def test_degraded_rules(knobs: dict[str, Any]):
    assert _health(_Prom(**knobs))["data"]["status"] == "degraded"


def test_fewer_ready_than_desired_is_degraded():
    prom = _Prom(
        instances={"checkout-api:8000": 1.0, "checkout-api:8001": 0.0},
        current={"checkout-api:8000": "v2sha"},
    )
    data = _health(prom)["data"]
    assert data["status"] == "degraded"
    assert data["replicas"] == {"desired": 2, "ready": 1, "updated": 1, "unavailable": 1}


def test_no_instance_up_is_failed_with_signals_still_reported():
    prom = _Prom(instances={"checkout-api:8000": 0.0}, current={}, probe_failures=12.0)
    data = _health(prom)["data"]
    assert data["status"] == "failed" and data["version"] is None
    assert data["replicas"]["ready"] == 0 and data["signals"]["probe_failures"] == 12


def test_mixed_versions_is_in_progress():
    prom = _Prom(
        instances={"checkout-api:8000": 1.0, "checkout-api:8001": 1.0},
        current={"checkout-api:8000": "v2sha", "checkout-api:8001": "v1sha"},
        seen={"v1sha": _NOW_EPOCH - 3, "v2sha": _NOW_EPOCH - 3},
    )
    data = _health(prom, version="v2sha")["data"]
    assert data["status"] == "in_progress" and data["replicas"]["updated"] == 1


def test_a_replaced_version_is_rolled_back():
    prom = _Prom(seen={"v1sha": _NOW_EPOCH - 600, "v2sha": _NOW_EPOCH - 5})
    data = _health(prom, version="v1sha")["data"]
    assert data["status"] == "rolled_back" and data["version"] == "v1sha"


def test_a_version_never_seen_in_the_window_is_version_not_deployed():
    payload = _health(version="v7sha")
    error = payload["error"]
    assert error["class"] == "business" and error["code"] == "VERSION_NOT_DEPLOYED"
    assert error["details"]["seen_in_window"] == ["v2sha"]
    assert "lookback_minutes" in error["remediation"]


def test_a_previous_version_in_the_window_becomes_the_baseline_with_deltas():
    prom = _Prom(
        seen={"v1sha": _NOW_EPOCH - 600, "v2sha": _NOW_EPOCH - 5},
        error_rate=0.31,
        p99_s=2.1,
        baseline_error_rate=0.004,
        baseline_p99_s=0.18,
    )
    data = _health(prom)["data"]
    assert data["status"] == "degraded"
    baseline = data["compared_to_baseline"]
    assert baseline["baseline_version"] == "v1sha"
    assert baseline["error_rate_delta"] == pytest.approx(0.306)
    assert baseline["p99_delta_ms"] == pytest.approx(1920.0)
    # The baseline signals were read AT the moment the old version was last seen.
    historical = [at for q, at in prom.queries if at and at != health._stamp(_NOW)]
    assert historical and all(a == historical[0] for a in historical)
    assert historical[0] == health._stamp(datetime.fromtimestamp(_NOW_EPOCH - 600, tz=UTC))


def test_a_version_last_seen_seconds_ago_is_not_yet_a_baseline():
    prom = _Prom(seen={"v1sha": _NOW_EPOCH - 4, "v2sha": _NOW_EPOCH - 2})
    assert _health(prom)["data"]["compared_to_baseline"] is None


def test_an_up_service_without_build_info_cannot_confirm_a_requested_version():
    prom = _Prom(current={}, seen={})
    assert _health(prom)["data"]["version"] is None  # unrequested: honest null
    error = _health(prom, version="v2sha")["error"]
    assert error["class"] == "business" and error["code"] == "VERSION_NOT_OBSERVABLE"


def test_an_unscraped_service_is_unknown_service_listing_the_known_ones():
    error = _health(service="ghost-api")["error"]
    assert error["class"] == "business" and error["code"] == "UNKNOWN_SERVICE"
    assert error["details"]["known_services"] == ["checkout-api", "inventory-api", "payments-api"]


def test_another_environment_is_not_answered_from_this_one():
    error = _health(environment="production")["error"]
    assert error["class"] == "business" and error["code"] == "ENVIRONMENT_NOT_MONITORED"
    assert error["details"] == {"environment": "production", "monitored": "development"}
    # And the monitored environment itself comes from AIOC_ENV.
    assert _health(env="staging", environment="staging")["ok"] is True


def test_chaos_services_return_a_permission_error_not_data():
    error = _health(service="chaos-injector")["error"]
    assert error["class"] == "permission" and error["code"] == "CHAOS_SCOPE_REQUIRED"


def test_the_battery_never_queries_the_ground_truth_metric():
    prom = _Prom(seen={"v1sha": _NOW_EPOCH - 600, "v2sha": _NOW_EPOCH - 5})
    _health(prom)
    assert prom.queries and not any("chaos_knob_value" in q for q, _ in prom.queries)
    with pytest.raises(RuntimeError, match="ground truth"):
        _reader(prom).instant("chaos_knob_value")


@pytest.mark.parametrize("lookback", [4, 1441, "30", 7.5, True])
def test_invalid_lookback_is_a_structured_validation_error(lookback: Any):
    error = _health(lookback_minutes=lookback)["error"]
    assert error["class"] == "validation" and error["code"] == "INVALID_LOOKBACK"
    assert error["details"]["field"] == "lookback_minutes"


def test_a_prometheus_timeout_is_transient():
    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    error = _health(slow)["error"]
    assert error["class"] == "transient" and error["code"] == "PROMETHEUS_TIMEOUT"
    assert error["retryable"] is True and error["retry_after_ms"] == 2000


def test_prometheus_unreachable_is_transient_but_not_a_timeout():
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    error = _health(down)["error"]
    assert error["class"] == "transient" and error["code"] == "PROMETHEUS_UNAVAILABLE"


# ------------------------------------------------------- the taxonomy, both tools


def test_all_four_error_classes_return_distinctly():
    """One error of each class from this server's real paths, structurally distinguishable."""
    validation = _payload(ds.call(ds.CHECK_ROLLOUT_HEALTH, {"service": "s"}))  # no environment
    permission = _health(service="chaos-injector")
    business = _health(service="ghost-api")
    transient = _payload(
        ds.call(
            ds.DIFF_RELEASE,
            {"service": "s", "from_version": V1, "to_version": V2},
            api=_api(lambda _r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
        )
    )
    by_class = {p["error"]["class"]: p for p in (validation, permission, business, transient)}
    assert set(by_class) == {"validation", "permission", "business", "transient"}
    assert by_class["transient"]["error"]["retryable"] is True
    assert by_class["transient"]["error"]["retry_after_ms"] is not None
    for cls in ("validation", "permission", "business"):
        assert by_class[cls]["error"]["retryable"] is False
        assert by_class[cls]["error"]["retry_after_ms"] is None
    assert {"field", "expected"} <= set(by_class["validation"]["error"]["details"])
    assert "required_scope" in by_class["permission"]["error"]["details"]
    assert by_class["business"]["error"]["remediation"]
    for p in by_class.values():
        assert p["ok"] is False


@pytest.mark.parametrize(
    ("name", "args", "field"),
    [
        (ds.DIFF_RELEASE, {"service": "s", "from_version": "a"}, "to_version"),
        (ds.DIFF_RELEASE, {"service": "", "from_version": "a", "to_version": "b"}, "service"),
        (
            ds.DIFF_RELEASE,
            {"service": "s", "from_version": "a", "to_version": "b", "include": "everything"},
            "include",
        ),
        (ds.CHECK_ROLLOUT_HEALTH, {"service": "s", "environment": "prod"}, "environment"),
        (ds.CHECK_ROLLOUT_HEALTH, {"service": "s", "environment": "development", "x": 1}, "x"),
    ],
)
def test_invalid_input_is_a_structured_validation_error(
    name: str, args: dict[str, Any], field: str
):
    error = _payload(ds.call(name, args))["error"]
    assert error["class"] == "validation" and error["code"] == "INVALID_INPUT"
    assert error["details"]["field"] == field and error["details"]["expected"]


def test_unknown_tool_is_a_validation_error():
    error = _payload(ds.call("deploy_it", {}))["error"]
    assert error["code"] == "UNKNOWN_TOOL" and error["details"]["received"] == "deploy_it"


# --------------------------------------------------------------- template + drift


@pytest.mark.parametrize("name", ds.TOOL_NAMES)
def test_descriptions_follow_the_four_part_template_in_order(name: str):
    text = ds.DESCRIPTIONS[name]
    examples = text.index("Example queries this tool answers:")
    edges = text.index("Edge cases and limits:")
    when = text.index("When to use this vs. the alternative:")
    assert 0 < examples < edges < when
    assert text[examples:edges].count('- "') >= 3
    # Part 4 names the sibling tool explicitly - that line is the routing discriminator.
    other = {ds.DIFF_RELEASE, ds.CHECK_ROLLOUT_HEALTH} - {name}
    assert all(alt in text[when:] for alt in other)


def test_every_schema_matches_its_tool_and_rejects_unknown_fields():
    for name in ds.TOOL_NAMES:
        assert ds.SCHEMAS[name]["additionalProperties"] is False
    assert ds.DIFF_RELEASE_SCHEMA["required"] == ["service", "from_version", "to_version"]
    assert ds.CHECK_ROLLOUT_HEALTH_SCHEMA["required"] == ["service", "environment"]
    assert ds.CHECK_ROLLOUT_HEALTH_SCHEMA["properties"]["lookback_minutes"] == {
        "type": "integer",
        "minimum": 5,
        "maximum": 1440,
        "default": 30,
        "description": "Window for the signals, 5-1440 minutes.",
    }


def test_the_server_does_not_import_the_contract_models():
    for module in (ds, health, release):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [n for n in imported if n.startswith("aioc.contracts")], module.__name__


def test_the_longhand_enum_copies_match_the_contract_enums():
    assert set(ds.ENVIRONMENTS) == {m.value for m in Environment}
    assert set(ds.ROLLOUT_STATUSES) == {m.value for m in RolloutStatus}


def test_settings_env_file_is_the_repo_root_dotenv():
    env_file = ds.DeploymentSettings.model_config["env_file"]
    assert isinstance(env_file, Path)
    assert env_file.name == ".env"
    assert (env_file.parent / "pyproject.toml").is_file()


@pytest.mark.asyncio
async def test_list_tools_exposes_both_tools_with_their_descriptions():
    tools = await ds.list_tools()
    assert [t.name for t in tools] == list(ds.TOOL_NAMES)
    assert tools[0].description == ds.DIFF_RELEASE_DESCRIPTION
    assert tools[1].inputSchema == ds.CHECK_ROLLOUT_HEALTH_SCHEMA
