"""Day 4 done-when: the Incident agent returns schema-validated output.

Run it:

    uv run python examples/incident_structured_demo.py

Needs ``ANTHROPIC_API_KEY`` (console key - see the DAY 2 block in ``.env.example``). Without a
key it prints how to set one and exits cleanly.

Unlike the Day 3 prose demo, this calls ``diagnose``: the model is forced through the
``emit_incident_report`` tool, and the result is a validated ``IncidentAgentResponse`` - the
same contract object the coordinator will consume from Day 6. Day 5 replaces the hand-written
context below with real Prometheus data from the demo app.
"""

from __future__ import annotations

from aioc.agents import IncidentAgent
from aioc.llm import LLMSettings

_CONTEXT = """\
Service inventory: checkout-api (front), payments-api, inventory-api (downstreams).

Prometheus observations, window 14:00-14:15 UTC:
- payments-api http_requests_total 5xx ratio: 0.1% at 14:00, 7.4% at 14:10, still rising.
- payments-api http_request_duration_seconds p99: 120ms at 14:00, 2100ms at 14:10.
- checkout-api 5xx ratio: 0.1% -> 2.1% over the same window (calls payments-api per checkout).
- inventory-api: all metrics flat and nominal.
- process_resident_memory_bytes for payments-api: steady growth from 180MB at 12:00
  to 1.4GB at 14:10.

Deploy log: no deploys to any of the three services in the last 24 hours.
On-call note: no infrastructure maintenance scheduled."""


def main() -> int:
    settings = LLMSettings()
    if settings.anthropic_api_key is None:
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Set it in your shell:  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  or add it to .env (see the DAY 2 block in .env.example)."
        )
        return 0

    agent = IncidentAgent()
    response = agent.diagnose(
        "Checkout success rate is dropping and customers are complaining. "
        "What is going on and what should we do first?",
        context=_CONTEXT,
    )

    # It validated as an IncidentAgentResponse or diagnose() would have raised. Print the wire
    # form and a few highlights so the structure is visible at a glance.
    print(response.model_dump_json(indent=2, exclude_none=False))
    print("\n--- highlights ---")
    print(f"status:            {response.status.value}")
    print(f"summary:           {response.summary}")
    fm = response.findings.failure_mode
    detail = "" if fm.value is None else f" ({fm.value.value})"
    print(f"failure_mode:      {detail.strip() or 'null'} @ confidence {fm.confidence}")
    print(f"evidence items:    {len(response.evidence)}")
    print(f"gaps:              {len(response.gaps)}")
    print(f"overall_confidence:{response.overall_confidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
