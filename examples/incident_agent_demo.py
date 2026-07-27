"""Day 3 done-when: the Incident agent returns prose about a synthetic incident.

Run it:

    uv run python examples/incident_agent_demo.py

Needs ``ANTHROPIC_API_KEY`` (console key - see the DAY 2 block in ``.env.example``).
Without a key it prints how to set one and exits cleanly.

The context block below is the kind of digest the coordinator will pass on Day 7 - handed
over explicitly, never inherited. Day 5 replaces it with real Prometheus data from the
demo app.
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
    result = agent.investigate(
        "Checkout success rate is dropping and customers are complaining. "
        "What is going on and what should we do first?",
        context=_CONTEXT,
    )

    print(f"Model: {result.model}  (stop: {result.stop_reason})\n")
    print(result.text)
    print(f"\nTokens: in={result.input_tokens}, out={result.output_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
