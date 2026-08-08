"""Prometheus reads for the Reasoning Layer (Day 5).

Day 3 gave the demo app metrics and Day 4 gave it injectable faults. This module is what
turns those into the ``context`` block the Incident agent already accepts, replacing the
hand-written fixture the Day 4 example used.

Two pieces:

- `PrometheusClient` - a thin PromQL wrapper (`instant`, `instant_vector`). Deliberately not
  a general-purpose client: it returns plain floats keyed by label, because everything
  downstream wants "the p99 for payments-api", not a Prometheus result envelope.
- `build_incident_context` - runs a fixed battery of queries and formats the answers as the
  explicit context block. Nothing is inherited; the agent sees exactly this text.

**`chaos_knob_value` is deliberately excluded from the context.** The demo app publishes
every injected fault as a gauge, which makes it the Day 19 eval's ground truth - and handing
the agent its own answer key would make every eval score meaningless. The exclusion is
enforced in code (`_FORBIDDEN_IN_CONTEXT`) rather than left to whoever edits the query list,
because the failure is silent: the evals would simply start passing.

Queries are shaped for the metrics `demo-app/services/app.py` actually exposes:
``http_requests_total{service,path,method,status}``, ``http_request_duration_seconds``
(a histogram, so ``_bucket``), and the ``process_*`` collectors that prometheus_client
registers by default. The process metrics carry no ``service`` label - only ``instance``
(``checkout-api:8000``) - so they are keyed by instance and mapped back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")

# The demo app's three services, in fan-out order. checkout-api is the only front service.
DEMO_SERVICES = ("checkout-api", "payments-api", "inventory-api")

# Never put these in an agent's context. `chaos_knob_value` is the injected ground truth the
# Day 19 eval scores against; showing it to the agent turns diagnosis into transcription.
_FORBIDDEN_IN_CONTEXT = ("chaos_knob_value",)


class PrometheusError(RuntimeError):
    """Prometheus was unreachable, or answered with something unusable."""


@dataclass(frozen=True)
class Window:
    """A closed observation window. `rate()` needs a lookback, so `step` is the range used
    inside the PromQL expressions rather than a resolution for a range query."""

    start: datetime
    end: datetime
    lookback: str = "5m"

    @classmethod
    def last(cls, minutes: int, *, now: datetime | None = None, lookback: str = "5m") -> Window:
        end = now or datetime.now(UTC)
        return cls(start=end - timedelta(minutes=minutes), end=end, lookback=lookback)

    def rfc3339(self) -> tuple[str, str]:
        return (_stamp(self.start), _stamp(self.end))


def _stamp(moment: datetime) -> str:
    """RFC 3339 with an explicit Z, per the contract's timestamp primitive (sec 1)."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class PrometheusClient:
    """Minimal PromQL client. Returns floats keyed by a label, not result envelopes."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self._timeout = timeout_seconds
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def instant(self, query: str, *, at: datetime | None = None) -> list[dict[str, Any]]:
        """Run an instant query and return the raw result list.

        Raises `PrometheusError` on transport failure or a non-success Prometheus status, so
        callers never have to distinguish "no series" from "the query was wrong" - the first
        is an empty list, the second raises.
        """
        params: dict[str, str] = {"query": query}
        if at is not None:
            params["time"] = _stamp(at)
        try:
            resp = self._http().get(f"{self.base_url}/api/v1/query", params=params)
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise PrometheusError(
                f"Prometheus unreachable at {self.base_url} ({type(exc).__name__}). "
                "Is the stack up?  ->  make up"
            ) from exc
        except ValueError as exc:
            raise PrometheusError(f"Prometheus returned a non-JSON body: {exc}") from exc

        if body.get("status") != "success":
            raise PrometheusError(
                f"Prometheus rejected the query: {body.get('error', 'unknown error')}"
            )
        result = body.get("data", {}).get("result", [])
        return list(result) if isinstance(result, list) else []

    def instant_vector(
        self, query: str, *, label: str = "service", at: datetime | None = None
    ) -> dict[str, float]:
        """An instant query reduced to ``{label_value: sample}``.

        Series whose value is not a finite float (Prometheus reports ``NaN`` for an empty
        histogram quantile) are dropped rather than coerced to zero. A missing measurement is
        absent, never a placeholder - the same rule the agents follow for null.
        """
        out: dict[str, float] = {}
        for series in self.instant(query, at=at):
            key = series.get("metric", {}).get(label)
            if key is None:
                continue
            try:
                value = float(series["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if value != value:  # NaN
                continue
            out[str(key)] = value
        return out

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ------------------------------------------------------------------ the query battery


def _queries(lookback: str) -> dict[str, tuple[str, str]]:
    """`{name: (promql, label_to_key_on)}`.

    Kept as data rather than inline calls so `build_incident_context` stays readable and so
    the forbidden-metric guard can inspect every query before any of them runs.
    """
    return {
        # The `or ... * 0` is load-bearing, not a flourish. A service with traffic and no 5xx
        # produces NO series for the numerator, so the bare division returns nothing and the
        # context reads "not measured" - telling the agent it could not look, when the truth is
        # it looked and found zero errors. Those are different findings under the contract, and
        # the agent is required to distinguish them. Multiplying the denominator by zero
        # supplies an explicit 0 for every service that has traffic, while a service with no
        # traffic at all stays absent, which is genuinely "not measured".
        "error_ratio": (
            f'(sum by (service) (rate(http_requests_total{{status=~"5.."}}[{lookback}]))'
            f" or sum by (service) (rate(http_requests_total[{lookback}])) * 0)"
            f" / sum by (service) (rate(http_requests_total[{lookback}]))",
            "service",
        ),
        "request_rate": (
            f"sum by (service) (rate(http_requests_total[{lookback}]))",
            "service",
        ),
        "p50_seconds": (
            "histogram_quantile(0.5, sum by (service, le) "
            f"(rate(http_request_duration_seconds_bucket[{lookback}])))",
            "service",
        ),
        "p99_seconds": (
            "histogram_quantile(0.99, sum by (service, le) "
            f"(rate(http_request_duration_seconds_bucket[{lookback}])))",
            "service",
        ),
        # The process collectors carry `instance`, not `service` - see the module docstring.
        "rss_bytes": ('process_resident_memory_bytes{job="demo-app"}', "instance"),
        "cpu_seconds": (
            f'rate(process_cpu_seconds_total{{job="demo-app"}}[{lookback}])',
            "instance",
        ),
        "up": ('up{job="demo-app"}', "instance"),
    }


def _service_of(instance: str) -> str:
    """`checkout-api:8000` -> `checkout-api`."""
    return instance.split(":", 1)[0]


def _rekey_by_service(values: dict[str, float]) -> dict[str, float]:
    return {_service_of(instance): value for instance, value in values.items()}


def collect_service_metrics(
    client: PrometheusClient, window: Window
) -> dict[str, dict[str, float]]:
    """Run the battery and return ``{service: {metric: value}}``.

    A service missing from a given query is simply absent from its inner dict; the caller
    renders that as "not measured" rather than inventing a zero.
    """
    queries = _queries(window.lookback)

    leaked = [
        name
        for name, (promql, _) in queries.items()
        if any(forbidden in promql for forbidden in _FORBIDDEN_IN_CONTEXT)
    ]
    if leaked:
        raise PrometheusError(
            f"query {leaked} references a metric that must never reach an agent's context "
            f"({', '.join(_FORBIDDEN_IN_CONTEXT)}); it is the eval's ground truth"
        )

    per_service: dict[str, dict[str, float]] = {svc: {} for svc in DEMO_SERVICES}
    for name, (promql, label) in queries.items():
        values = client.instant_vector(promql, label=label, at=window.end)
        if label == "instance":
            values = _rekey_by_service(values)
        for service, value in values.items():
            per_service.setdefault(service, {})[name] = value
    return per_service


# ------------------------------------------------------------------ context rendering


def _fmt_ratio(value: float | None) -> str:
    return "not measured" if value is None else f"{value * 100:.2f}%"


def _fmt_ms(seconds: float | None) -> str:
    return "not measured" if seconds is None else f"{seconds * 1000:.0f}ms"


def _fmt_mb(byte_count: float | None) -> str:
    return "not measured" if byte_count is None else f"{byte_count / 1e6:.0f}MB"


def build_incident_context(
    client: PrometheusClient,
    window: Window,
    *,
    extra_notes: str | None = None,
) -> str:
    """Render live Prometheus data as the Incident agent's explicit context block.

    This is the Day 5 replacement for the hand-written fixture. The shape deliberately
    matches what the Day 3/4 prompt was already written against - service inventory, then
    per-service observations, then the operational notes an on-call would have - so the
    agent's prompt did not have to change to consume real data.

    "not measured" is used rather than 0 wherever a series is absent, because the agent is
    required to distinguish "looked and found nothing" from "could not look", and a zero
    would quietly become the former.
    """
    metrics = collect_service_metrics(client, window)
    start, end = window.rfc3339()

    lines = [
        "Service inventory: checkout-api (front, fans out to both downstreams), "
        "payments-api, inventory-api (downstreams).",
        "",
        f"Prometheus observations, window {start} to {end} (rates over {window.lookback}):",
    ]

    for service in DEMO_SERVICES:
        m = metrics.get(service, {})
        if not m:
            lines.append(f"- {service}: no series returned; the service may not be scraped.")
            continue
        reachable = m.get("up")
        state = "up" if reachable == 1.0 else "DOWN" if reachable == 0.0 else "unknown"
        lines.append(
            f"- {service} ({state}): "
            f"5xx ratio {_fmt_ratio(m.get('error_ratio'))}, "
            f"request rate {m.get('request_rate', float('nan')):.2f}/s, "
            f"p50 {_fmt_ms(m.get('p50_seconds'))}, "
            f"p99 {_fmt_ms(m.get('p99_seconds'))}, "
            f"RSS {_fmt_mb(m.get('rss_bytes'))}, "
            f"CPU {m.get('cpu_seconds', float('nan')):.3f} cores"
        )

    lines += [
        "",
        "Topology: every checkout-api /process call fans out to payments-api and "
        "inventory-api and returns 502 if either downstream fails, so a checkout-api "
        "502 rate implicates a downstream rather than checkout-api itself.",
    ]
    if extra_notes:
        lines += ["", extra_notes.strip()]

    rendered = "\n".join(lines)
    # Belt and braces: the guard above checks the queries, this checks the output. A metric
    # name reaching the agent through a note or a future formatter is the same leak.
    for forbidden in _FORBIDDEN_IN_CONTEXT:
        if forbidden in rendered:
            raise PrometheusError(
                f"rendered context contains '{forbidden}', which is the eval's ground truth"
            )
    return rendered
