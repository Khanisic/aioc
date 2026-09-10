"""Rollout health from Prometheus (Day 12): the reads behind `check_rollout_health`.

Everything here is a fact Prometheus holds about the demo services, reduced to the
contract's sec 7.4 shape. The rules that turn facts into a `status` are deterministic and
stated in the tool description, so an agent reading `degraded` can also read *why* from
the signals beside it - the judgement about what to do (roll back, hold, nothing) stays
with the Deployment agent, where it carries a confidence.

Version identity comes from the demo app's ``service_build_info`` gauge (constant 1, the
version in ``git_sha``): the current version is the label on the instances that are up,
the baseline is the most recent *other* version seen inside the window, and "was this
version ever deployed?" is a lookback over the same series. Reading it from Prometheus
rather than from each service's ``/version`` endpoint is what gives the tool history - a
rollback that finished two minutes ago is invisible to ``/version`` and plain in the gauge.

**Any signal that could not be measured is `null`, never `0`** (contract sec 7.4). The
error ratio needs the same ``or ... * 0`` care as the Incident context (Day 5): a service
with traffic and no 5xx has no numerator series, and the honest reading of that is an
error rate of zero, while a service with no traffic at all has no denominator and its
error rate is genuinely unmeasured.

**`chaos_knob_value` is never queried** - it is the Day 19 eval's ground truth, and the
guard here is the same one `aioc.observability.prometheus` enforces for the Incident
agent's context. **No `aioc.contracts` import** - the MCP boundary is JSON Schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

# Never queried by this module. The check is enforced on every query string before it is
# sent, because the failure would be silent: the evals would simply start passing.
FORBIDDEN_METRICS = ("chaos_knob_value",)

# Prometheus reports the demo services under this scrape job (docker/prometheus/prometheus.yml).
DEFAULT_JOB = "demo-app"

# The deterministic thresholds behind `degraded`. Stated in the tool description too.
DEGRADED_ERROR_RATE = 0.05

# Two consecutive scrapes must have passed since a version was last seen before it counts
# as replaced rather than as still rolling out.
_REPLACED_AFTER_SECONDS = 15.0


class PrometheusFailure(Exception):
    """Prometheus could not answer. ``code`` is the contract error code; the class is
    always `transient` - the query is fine, the upstream is not."""

    def __init__(self, code: str, message: str, *, retry_after_ms: int = 2000) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_ms = retry_after_ms


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class PrometheusReader:
    """A deliberately small instant-query client. ``client`` is injectable so the tests
    drive it with an `httpx.MockTransport`; the real thing is built per reader."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client
        self.queries: list[str] = []  # every query sent, for the tests and the guard

    def instant(self, query: str, *, at: datetime | None = None) -> list[dict[str, Any]]:
        for forbidden in FORBIDDEN_METRICS:
            if forbidden in query:
                raise RuntimeError(
                    f"refusing to query {forbidden!r}: it is the eval's injected ground truth"
                )
        self.queries.append(query)
        params: dict[str, str] = {"query": query}
        if at is not None:
            params["time"] = _stamp(at)
        try:
            if self._client is not None:
                resp = self._client.get(f"{self.base_url}/api/v1/query", params=params)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.get(f"{self.base_url}/api/v1/query", params=params)
            body = resp.json()
        except httpx.TimeoutException as exc:
            raise PrometheusFailure(
                "PROMETHEUS_TIMEOUT",
                f"Prometheus did not answer within {self._timeout:g}s ({type(exc).__name__}).",
            ) from exc
        except httpx.HTTPError as exc:
            raise PrometheusFailure(
                "PROMETHEUS_UNAVAILABLE",
                f"Prometheus unreachable at {self.base_url} ({type(exc).__name__}).",
            ) from exc
        except ValueError as exc:
            raise PrometheusFailure(
                "PROMETHEUS_UNAVAILABLE", f"Prometheus returned a non-JSON body: {exc}"
            ) from exc
        if resp.status_code >= 400 or body.get("status") != "success":
            raise PrometheusFailure(
                "PROMETHEUS_UNAVAILABLE",
                f"Prometheus rejected the query ({resp.status_code}): "
                f"{body.get('error', 'unknown error')}",
            )
        result = body.get("data", {}).get("result", [])
        return list(result) if isinstance(result, list) else []

    def scalar(self, query: str, *, at: datetime | None = None) -> float | None:
        """A query reduced to one number, or ``None`` when it produced no finite sample."""
        for series in self.instant(query, at=at):
            value = _sample(series)
            if value is not None:
                return value
        return None

    def by_labels(self, query: str, *labels: str, at: datetime | None = None) -> list[Sample]:
        out: list[Sample] = []
        for series in self.instant(query, at=at):
            value = _sample(series)
            if value is None:
                continue
            metric = series.get("metric", {})
            out.append(Sample(tuple(str(metric.get(label, "")) for label in labels), value))
        return out


@dataclass(frozen=True)
class Sample:
    labels: tuple[str, ...]
    value: float


def _sample(series: dict[str, Any]) -> float | None:
    try:
        value = float(series["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None  # NaN from an empty histogram quantile is "not measured", not a number
    return value


# --------------------------------------------------------------------------- the reads


def _service_of(instance: str) -> str:
    return instance.split(":", 1)[0]


@dataclass
class RolloutReading:
    """Everything `check_rollout_health` needs, as read - the status rule runs on this."""

    known_services: list[str]
    desired: int
    ready: int
    current_versions: dict[str, str]  # ready instance -> git_sha (empty: no build_info)
    seen_versions: dict[str, float]  # git_sha -> last-seen epoch seconds inside the window
    error_rate: float | None
    p50_ms: float | None
    p99_ms: float | None
    restart_count: int | None
    probe_failures: int | None


def read_rollout(
    reader: PrometheusReader,
    *,
    service: str,
    lookback_minutes: int,
    job: str = DEFAULT_JOB,
    now: datetime | None = None,
) -> RolloutReading:
    """Run the query battery for one service. Raises `PrometheusFailure` on transport
    trouble; an absent series is simply `None` in the reading."""
    at = now or datetime.now(UTC)
    window = f"{lookback_minutes}m"
    inst = f'job="{job}",instance=~"{service}:.*"'
    svc = f'service="{service}"'

    targets = reader.by_labels(f'up{{job="{job}"}}', "instance", at=at)
    known = sorted({_service_of(s.labels[0]) for s in targets})
    mine = [s for s in targets if _service_of(s.labels[0]) == service]
    desired = len(mine)
    ready_instances = {s.labels[0] for s in mine if s.value == 1.0}

    current: dict[str, str] = {}
    for s in reader.by_labels(f"service_build_info{{{svc}}}", "instance", "git_sha", at=at):
        if s.labels[0] in ready_instances and s.labels[1]:
            current[s.labels[0]] = s.labels[1]

    seen: dict[str, float] = {}
    for s in reader.by_labels(
        f"timestamp(last_over_time(service_build_info{{{svc}}}[{window}]))", "git_sha", at=at
    ):
        if s.labels[0]:
            seen[s.labels[0]] = max(seen.get(s.labels[0], 0.0), s.value)

    error_rate, p50, p99 = _traffic_signals(reader, svc=svc, window=window, at=at)
    restarts = reader.scalar(f"sum(changes(process_start_time_seconds{{{inst}}}[{window}]))", at=at)
    probe_failures = reader.scalar(
        f"sum(count_over_time(up{{{inst}}}[{window}]) - sum_over_time(up{{{inst}}}[{window}]))",
        at=at,
    )
    return RolloutReading(
        known_services=known,
        desired=desired,
        ready=len(ready_instances),
        current_versions=current,
        seen_versions=seen,
        error_rate=error_rate,
        p50_ms=p50,
        p99_ms=p99,
        restart_count=None if restarts is None else int(restarts),
        probe_failures=None if probe_failures is None else int(probe_failures),
    )


def _traffic_signals(
    reader: PrometheusReader, *, svc: str, window: str, at: datetime
) -> tuple[float | None, float | None, float | None]:
    total = f"sum(rate(http_requests_total{{{svc}}}[{window}]))"
    errors = f'sum(rate(http_requests_total{{{svc},status=~"5.."}}[{window}]))'
    # `or total * 0`: traffic with no 5xx series is a measured zero, no traffic is null.
    error_rate = reader.scalar(f"({errors} or {total} * 0) / {total}", at=at)
    p50 = reader.scalar(_quantile(0.5, svc, window), at=at)
    p99 = reader.scalar(_quantile(0.99, svc, window), at=at)
    return (
        error_rate,
        None if p50 is None else p50 * 1000.0,
        None if p99 is None else p99 * 1000.0,
    )


def _quantile(q: float, svc: str, window: str) -> str:
    return (
        f"histogram_quantile({q}, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{svc}}}[{window}])))"
    )


def read_baseline(
    reader: PrometheusReader,
    *,
    service: str,
    lookback_minutes: int,
    at_epoch: float,
) -> tuple[float | None, float | None]:
    """The error rate and p99 as they stood at ``at_epoch`` - the moment the baseline
    version was last seen - over the same lookback, so the deltas compare like with like."""
    moment = datetime.fromtimestamp(at_epoch, tz=UTC)
    error_rate, _, p99 = _traffic_signals(
        reader, svc=f'service="{service}"', window=f"{lookback_minutes}m", at=moment
    )
    return error_rate, p99


# ------------------------------------------------------------------------ the status rule


@dataclass(frozen=True)
class Verdict:
    """The rule's output: the version assessed, its status, and the baseline (if any)."""

    version: str | None
    status: str
    baseline_version: str | None
    baseline_at: float | None  # epoch seconds the baseline was last seen
    updated: int
    problem: str | None  # why a version request could not be honoured, else None


def judge(reading: RolloutReading, *, requested_version: str | None, now_epoch: float) -> Verdict:
    """Deterministic status from the reading. The rules, in order (also in the description):

    - ``failed``: no instance of the service is up.
    - a requested version that no ready instance runs and that was never seen inside the
      window is ``VERSION_NOT_DEPLOYED`` (reported through ``problem``); one that was seen
      and then replaced is ``rolled_back``.
    - ``in_progress``: the assessed version runs on some ready instances but not all.
    - ``degraded``: error rate at or above 5%, any restart, any failed scrape, or fewer
      ready instances than desired.
    - ``unknown``: instances are up but not one signal could be measured.
    - ``healthy`` otherwise.
    """
    current = reading.current_versions
    running = sorted(set(current.values()))
    latest_running = (
        max(running, key=lambda v: reading.seen_versions.get(v, 0.0)) if running else None
    )

    if requested_version is not None:
        version: str | None = requested_version
        if reading.ready > 0 and not current:
            return Verdict(version, "unknown", None, None, 0, "VERSION_NOT_OBSERVABLE")
        if requested_version not in running:
            last_seen = reading.seen_versions.get(requested_version)
            if last_seen is None:
                return Verdict(version, "unknown", None, None, 0, "VERSION_NOT_DEPLOYED")
            if reading.ready == 0:
                return Verdict(version, "failed", None, None, 0, None)
            # Seen in the window, no longer running: replaced by what runs now.
            return Verdict(version, "rolled_back", None, None, 0, None)
    else:
        version = latest_running

    if reading.ready == 0:
        return Verdict(version, "failed", None, None, 0, None)

    updated = sum(1 for sha in current.values() if version is not None and sha == version)
    baseline, baseline_at = _baseline_of(reading, version, now_epoch)

    if version is not None and 0 < updated < reading.ready:
        return Verdict(version, "in_progress", baseline, baseline_at, updated, None)

    signals = (
        reading.error_rate,
        reading.p50_ms,
        reading.p99_ms,
        reading.restart_count,
        reading.probe_failures,
    )
    if all(s is None for s in signals) and version is None:
        return Verdict(version, "unknown", baseline, baseline_at, updated, None)

    degraded = (
        (reading.error_rate is not None and reading.error_rate >= DEGRADED_ERROR_RATE)
        or bool(reading.restart_count)
        or bool(reading.probe_failures)
        or reading.ready < reading.desired
    )
    status = "degraded" if degraded else "healthy"
    if all(s is None for s in signals):
        status = "unknown"
    return Verdict(version, status, baseline, baseline_at, updated, None)


def _baseline_of(
    reading: RolloutReading, version: str | None, now_epoch: float
) -> tuple[str | None, float | None]:
    """The most recent *other* version seen in the window and no longer running - the
    release this one replaced. A version last seen within two scrapes of now is still
    rolling out, not a baseline."""
    running = set(reading.current_versions.values())
    candidates = [
        (last_seen, sha)
        for sha, last_seen in reading.seen_versions.items()
        if sha != version
        and sha not in running
        and now_epoch - last_seen >= _REPLACED_AFTER_SECONDS
    ]
    if not candidates:
        return None, None
    last_seen, sha = max(candidates)
    return sha, last_seen
