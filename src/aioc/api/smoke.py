"""The zero-cost smoke pipeline: the real executor, tracer, and gate over scripted agents.

Day 9 wrote these fixtures to prove the Langfuse wiring without a Claude call
(`scripts/check_day9_trace.py --fake-agents`). Day 24 moved them here, because the
deployed service needs the same proof: a ``smoke`` request on the live URL runs a
scripted plan through `respond()` - the plan span, two scripted agents in parallel
through the real `Executor`, the deterministic synthesis, the HITL gate writing to the
real audit log, and the real tracer - and spends nothing. Only the plan and the agents'
output are scripted, and every summary says so (``SCRIPTED (smoke test)``), so a smoke
response can never be mistaken for a diagnosis.

The scripted query ignores what was asked: the plan is fixed, which is what lets the
check be exact.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aioc.contracts import AgentName, DocsAgentResponse, IncidentAgentResponse
from aioc.coordinator import Coordinator, SelectionPlan
from aioc.coordinator.executor import AgentRunner
from aioc.llm import Usage

# Long enough that two runners overlapping is a measurement, short enough for a request.
SMOKE_SLEEP_SECONDS = 1.5


def smoke_plan(query: str) -> SelectionPlan:
    incident_context = (
        "checkout-api 5xx rose from 0.1% to 2.4% in the last 15 minutes while payments-api "
        "p99 went 130ms -> 1900ms; inventory-api is nominal. Determine the failing service "
        "and the likely failure mode."
    )
    docs_context = (
        "An active latency incident implicates payments-api. Search the historical incident "
        "corpus for past payments-api latency or 5xx incidents and report how they were "
        "resolved, citing the documents."
    )
    return SelectionPlan.model_validate(
        {
            "intent": {
                "value": "incident_diagnosis",
                "confidence": 0.9,
                "evidence": [],
                "reasoning": "scripted plan for the zero-cost smoke test",
                "detail": None,
            },
            "selected_agents": [
                {
                    "invocation_id": "inv_incident",
                    "agent": "incident",
                    "reason": "a live symptom needs a diagnosis of the failing service",
                    "mode": "parallel",
                    "depends_on": [],
                    "context_passed": incident_context,
                    "round": 0,
                },
                {
                    "invocation_id": "inv_docs",
                    "agent": "docs",
                    "reason": "past resolutions for the implicated service inform the fix",
                    "mode": "parallel",
                    "depends_on": [],
                    "context_passed": docs_context,
                    "round": 0,
                },
            ],
            "skipped_agents": [
                {
                    "agent": "github",
                    "reason": "no code artefact is named and no deploy is implicated yet",
                },
                {
                    "agent": "deployment",
                    "reason": "no release comparison is needed to answer this query",
                },
            ],
            "gaps": [],
        }
    )


def smoke_incident_response(request_id: str, invocation_id: str) -> IncidentAgentResponse:
    def assessment(value: str, reasoning: str) -> dict[str, Any]:
        return {
            "value": value,
            "confidence": 0.7,
            "evidence": ["ev_1"],
            "reasoning": reasoning,
            "detail": None,
        }

    return IncidentAgentResponse.model_validate(
        {
            "agent": "incident",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": "complete",
            "status_detail": None,
            "summary": "SCRIPTED (smoke test): payments-api latency is degrading checkout-api.",
            "findings": {
                "incident_window": {"start": "2026-08-21T14:00:00Z", "end": None},
                "affected_services": ["checkout-api", "payments-api"],
                "severity": assessment("sev2", "customer-facing errors without a full outage"),
                "failure_mode": assessment(
                    "downstream_latency", "payments-api p99 rose 14x while checkout-api errored"
                ),
                "root_cause": assessment(
                    "payments-api latency breaching checkout-api's timeout",
                    "the 502 pattern matches downstream timeouts",
                ),
                "contributing_factors": [],
                "timeline": [],
                "impact": {
                    "error_rate_before": None,
                    "error_rate_after": None,
                    "p50_latency_ms_before": None,
                    "p50_latency_ms_after": None,
                    "p99_latency_ms_before": None,
                    "p99_latency_ms_after": None,
                    "requests_affected": None,
                    "duration_seconds": None,
                },
                # One production write, so a smoke request exercises the HITL gate and its
                # audit log too: the default approver denies it, and the denial is a row.
                "recommended_actions": [
                    {
                        "id": "act_1",
                        "action": "SCRIPTED (smoke test): restart payments-api to clear the "
                        "saturated connection pool.",
                        "rationale": "Scripted so the approval gate has a decision to record.",
                        "risk": "medium",
                        "risk_detail": None,
                        "reversible": True,
                        "requires_approval": True,
                        "target_service": "payments-api",
                        "command": None,
                    }
                ],
                "similar_incidents": [],
            },
            "evidence": [
                {
                    "id": "ev_1",
                    "source_type": "metric",
                    "source_type_detail": None,
                    "source_ref": 'http_request_duration_seconds{service="payments-api"}',
                    "excerpt": "payments-api p99 went 130ms -> 1900ms.",
                    "observed_at": "2026-08-21T14:15:00Z",
                    "uri": None,
                    "tool_call_id": None,
                }
            ],
            "gaps": [],
            "overall_confidence": 0.7,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


def smoke_docs_response(request_id: str, invocation_id: str) -> DocsAgentResponse:
    question = "How were past payments-api latency incidents resolved?"
    quote = "Resolved by resizing the payments-api connection pool and adding a timeout."
    return DocsAgentResponse.model_validate(
        {
            "agent": "docs",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": "complete",
            "status_detail": None,
            "summary": "SCRIPTED (smoke test): past incidents were resolved by pool resizing.",
            "findings": {
                "answer": {
                    "value": "Past payments-api latency incidents were resolved by connection "
                    "pool resizing and stricter timeouts.",
                    "confidence": 0.7,
                    "evidence": ["ev_doc_1"],
                    "reasoning": "one corpus document describes the same resolution",
                    "detail": None,
                },
                "claims": [
                    {
                        "id": "claim_1",
                        "statement": "A previous payments-api latency incident was resolved by "
                        "resizing the connection pool.",
                        "supported": True,
                        "sources": [
                            {
                                "document_id": "inc_fake_0001",
                                "title": "payments-api latency (scripted)",
                                "chunk_id": None,
                                "uri": None,
                                "quote": quote,
                                "relevance": 0.82,
                            }
                        ],
                        "confidence": 0.7,
                    }
                ],
                "coverage": {
                    "sub_questions": [question],
                    "answered": [question],
                    "unanswered": [],
                    "documents_searched": 18,
                    "documents_retrieved": 3,
                    "documents_cited": 1,
                    "corpus_snapshot": None,
                },
            },
            "evidence": [
                {
                    "id": "ev_doc_1",
                    "source_type": "document",
                    "source_type_detail": None,
                    "source_ref": "inc_fake_0001",
                    "excerpt": quote,
                    "observed_at": None,
                    "uri": None,
                    "tool_call_id": None,
                }
            ],
            "gaps": [],
            "overall_confidence": 0.7,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


class SmokeRunner:
    """A scripted agent that sleeps instead of thinking and records its wall-clock window,
    which is the local proof that the parallel group ran concurrently."""

    def __init__(
        self,
        make_response: Callable[[str, str], Any],
        windows: dict[str, tuple[float, float]] | None = None,
        *,
        sleep_seconds: float = SMOKE_SLEEP_SECONDS,
    ) -> None:
        self._make_response = make_response
        self.windows: dict[str, tuple[float, float]] = {} if windows is None else windows
        self._sleep_seconds = sleep_seconds

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> Any:
        started = time.monotonic()
        time.sleep(self._sleep_seconds)
        self.windows[invocation_id] = (started, time.monotonic())
        return self._make_response(request_id, invocation_id)


def smoke_runners(
    windows: dict[str, tuple[float, float]] | None = None,
    *,
    sleep_seconds: float = SMOKE_SLEEP_SECONDS,
) -> dict[AgentName, AgentRunner]:
    """The two scripted agents the smoke plan selects, sharing one window record."""
    shared: dict[str, tuple[float, float]] = {} if windows is None else windows
    return {
        AgentName.INCIDENT: SmokeRunner(
            smoke_incident_response, shared, sleep_seconds=sleep_seconds
        ),
        AgentName.DOCS: SmokeRunner(smoke_docs_response, shared, sleep_seconds=sleep_seconds),
    }


class SmokeCoordinator(Coordinator):
    """A coordinator that plans without a model: `smoke_plan`, whatever was asked."""

    def __init__(self) -> None:  # no client: nothing here may reach the API
        pass

    def plan(
        self,
        query: str,
        *,
        situation: str | None = None,
        usage: Usage | None = None,
        round_number: int = 0,
    ) -> SelectionPlan:
        return smoke_plan(query)


def overlap_ms(windows: dict[str, tuple[float, float]]) -> float | None:
    """How long two runners' windows overlapped, ``None`` unless exactly two ran."""
    if len(windows) != 2:
        return None
    (a_start, a_end), (b_start, b_end) = windows.values()
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)) * 1000)
