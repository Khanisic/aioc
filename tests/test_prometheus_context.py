"""Day 5: Prometheus reads and the context block they render.

Driven by a fake httpx transport, so no Prometheus and no Docker. The one thing these tests
care about more than formatting is the `chaos_knob_value` exclusion: the demo app publishes
every injected fault as a gauge, which makes it the Day 19 eval's ground truth. Leaking it
into the agent's context would not break anything visibly - the evals would simply start
passing - so it gets a guard and a test rather than a comment.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from aioc.observability import (
    DEMO_SERVICES,
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
    collect_service_metrics,
)
from aioc.observability import prometheus as prom

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _vector(*pairs: tuple[dict[str, str], float]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1785000000, str(value)]} for labels, value in pairs
            ],
        },
    }


def _client(responder) -> PrometheusClient:
    transport = httpx.MockTransport(responder)
    return PrometheusClient("http://prom:9090", client=httpx.Client(transport=transport))


def _healthy_responder(request: httpx.Request) -> httpx.Response:
    """Answers each battery query with plausible healthy values."""
    query = request.url.params.get("query", "")
    if "http_requests_total" in query and "5.." in query:
        return httpx.Response(200, json=_vector(*[({"service": s}, 0.001) for s in DEMO_SERVICES]))
    if "http_requests_total" in query:
        return httpx.Response(200, json=_vector(*[({"service": s}, 4.0) for s in DEMO_SERVICES]))
    if "0.5," in query:
        return httpx.Response(200, json=_vector(*[({"service": s}, 0.040) for s in DEMO_SERVICES]))
    if "0.99," in query:
        return httpx.Response(200, json=_vector(*[({"service": s}, 0.120) for s in DEMO_SERVICES]))
    if "process_resident_memory_bytes" in query:
        return httpx.Response(
            200, json=_vector(*[({"instance": f"{s}:8000"}, 55_000_000.0) for s in DEMO_SERVICES])
        )
    if "process_cpu_seconds_total" in query:
        return httpx.Response(
            200, json=_vector(*[({"instance": f"{s}:8000"}, 0.02) for s in DEMO_SERVICES])
        )
    if query.startswith("up"):
        return httpx.Response(
            200, json=_vector(*[({"instance": f"{s}:8000"}, 1.0) for s in DEMO_SERVICES])
        )
    return httpx.Response(200, json=_vector())


# ------------------------------------------------------------------------------- client


def test_instant_vector_keys_by_label_and_drops_nan():
    # An empty histogram yields NaN from histogram_quantile. Coercing that to 0.0 would tell
    # the agent latency was zero, which is a fabricated measurement, not a missing one.
    client = _client(
        lambda _r: httpx.Response(
            200,
            json=_vector(
                ({"service": "checkout-api"}, 0.5), ({"service": "payments-api"}, float("nan"))
            ),
        )
    )
    values = client.instant_vector("irrelevant")
    assert values == {"checkout-api": 0.5}


def test_instant_raises_a_clear_error_when_prometheus_is_unreachable():
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PrometheusError, match="make up"):
        _client(refuse).instant("up")


def test_instant_raises_when_prometheus_rejects_the_query():
    # A bad query is a bug in our code, not an absence of data - it must not look like
    # "no series returned", which is what a silent empty list would look like.
    client = _client(
        lambda _r: httpx.Response(200, json={"status": "error", "error": "parse error at char 3"})
    )
    with pytest.raises(PrometheusError, match="parse error"):
        client.instant("http_requests_total{")


def test_process_metrics_are_rekeyed_from_instance_to_service():
    metrics = collect_service_metrics(_client(_healthy_responder), Window.last(15, now=_NOW))
    # prometheus_client's process collectors carry `instance`, not `service`.
    assert metrics["checkout-api"]["rss_bytes"] == 55_000_000.0


# ------------------------------------------------------------------- the ground-truth guard


def test_no_battery_query_touches_the_chaos_ground_truth():
    for name, (promql, _label) in prom._queries("5m").items():
        assert "chaos_knob_value" not in promql, f"{name} would leak the eval's answer key"


def test_collect_refuses_a_query_referencing_the_ground_truth(monkeypatch):
    # Simulates someone adding a chaos_knob_value query to the battery later. It must fail
    # loudly, because the symptom otherwise is the Day 19 eval quietly scoring 100%.
    monkeypatch.setattr(
        prom, "_queries", lambda lookback: {"leak": ('chaos_knob_value{service="x"}', "service")}
    )
    with pytest.raises(PrometheusError, match="ground truth"):
        collect_service_metrics(_client(_healthy_responder), Window.last(15, now=_NOW))


def test_rendered_context_never_contains_the_ground_truth_metric():
    context = build_incident_context(
        _client(_healthy_responder),
        Window.last(15, now=_NOW),
        # Even smuggled in through the operator's own notes, it must not survive.
        extra_notes="On-call note: nothing scheduled.",
    )
    assert "chaos_knob_value" not in context


def test_extra_notes_carrying_the_ground_truth_are_rejected():
    with pytest.raises(PrometheusError, match="ground truth"):
        build_incident_context(
            _client(_healthy_responder),
            Window.last(15, now=_NOW),
            extra_notes="chaos_knob_value{service='payments-api',knob='error_rate'} = 0.5",
        )


# --------------------------------------------------------------------------- context shape


def test_context_names_every_service_and_the_window():
    context = build_incident_context(_client(_healthy_responder), Window.last(15, now=_NOW))
    for service in DEMO_SERVICES:
        assert service in context
    assert "2026-07-30T11:45:00Z" in context
    assert "2026-07-30T12:00:00Z" in context


def test_context_says_not_measured_rather_than_zero_for_absent_series():
    # The agent is required to distinguish "looked and found nothing" from "could not look".
    # A zero would quietly become the former, so an absent series must read as absent.
    def only_up(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query.startswith("up"):
            return httpx.Response(200, json=_vector(({"instance": "checkout-api:8000"}, 1.0)))
        return httpx.Response(200, json=_vector())

    context = build_incident_context(_client(only_up), Window.last(15, now=_NOW))
    assert "not measured" in context


def test_context_reports_a_down_service_as_down():
    def down(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query.startswith("up"):
            return httpx.Response(
                200,
                json=_vector(
                    ({"instance": "checkout-api:8000"}, 1.0),
                    ({"instance": "payments-api:8000"}, 0.0),
                ),
            )
        return _healthy_responder(request)

    context = build_incident_context(_client(down), Window.last(15, now=_NOW))
    assert "payments-api (DOWN)" in context


def test_context_explains_the_fan_out_topology():
    # Without this, a checkout-api 502 rate reads as checkout-api being broken. The topology
    # line is what lets the agent attribute it to a downstream instead.
    context = build_incident_context(_client(_healthy_responder), Window.last(15, now=_NOW))
    assert "502" in context and "downstream" in context


def test_error_ratio_query_yields_zero_rather_than_nothing_when_there_are_no_errors():
    # Measured against live Prometheus: the bare division returns NO series for a service with
    # traffic and no 5xx, which renders as "not measured" - i.e. the agent is told nobody
    # looked, when in fact zero errors were found. Those are different findings under the
    # contract. The `or <denominator> * 0` term is what supplies the explicit zero.
    promql, _label = prom._queries("5m")["error_ratio"]
    assert " or " in promql, "no fallback term: a zero-error service would read as unmeasured"
    assert "* 0" in promql


def test_a_service_with_no_traffic_at_all_still_reads_as_not_measured():
    # The other half of the same rule: absent must stay absent. The zero fallback keys off the
    # denominator, so a service Prometheus is not scraping produces no series either way.
    def no_traffic(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query.startswith("up"):
            return httpx.Response(200, json=_vector(({"instance": "checkout-api:8000"}, 1.0)))
        return httpx.Response(200, json=_vector())

    context = build_incident_context(_client(no_traffic), Window.last(15, now=_NOW))
    assert "5xx ratio not measured" in context


def test_window_last_produces_an_rfc3339_z_pair():
    start, end = Window.last(15, now=_NOW).rfc3339()
    assert start.endswith("Z") and end.endswith("Z")
    assert "+00:00" not in start
