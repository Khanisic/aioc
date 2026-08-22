"""Observability: metrics read out of Prometheus (Day 5), tracing into Langfuse (Day 9).

Both directions of the same concern. `prometheus` is the read side - it is what gives the
Incident agent something real to diagnose; `tracing` is the write side - one trace per
coordinator request, with a span for the planning call and each agent invocation.
"""

from .prometheus import (
    DEMO_SERVICES,
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
    collect_service_metrics,
)
from .tracing import (
    AgentSpan,
    LangfuseTracer,
    NullTracer,
    RequestTrace,
    Tracer,
    TracingSettings,
    default_tracer,
)

__all__ = [
    "DEMO_SERVICES",
    "AgentSpan",
    "LangfuseTracer",
    "NullTracer",
    "PrometheusClient",
    "PrometheusError",
    "RequestTrace",
    "Tracer",
    "TracingSettings",
    "Window",
    "build_incident_context",
    "collect_service_metrics",
    "default_tracer",
]
