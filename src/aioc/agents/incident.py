"""Incident agent skeleton (Day 3): expert-SRE system prompt, single turn, no tools.

Day 3 scope per EXECUTION_PLAN.md: the agent returns *prose*. The prose-first sequencing is
deliberate - Day 4 swaps the free-text tail for schema-validated ``IncidentFindings`` via
``tool_use`` + ``tool_choice``, and Day 5 wires real Prometheus data. The rules/agents.md
requirement that every agent return an ``aioc.contracts.AgentResponse`` lands with that Day 4
step; this module is the prompt-and-plumbing skeleton it builds on.

Two contract behaviours are already enforced here because they are cheap and graded:

- **Explicit context passing.** ``investigate`` requires a non-empty ``context`` block and
  embeds it verbatim in the prompt. Nothing is inherited implicitly - the same invariant
  ``AgentInvocation.context_passed`` enforces at the schema level (CONTRACTS.md sec 5).
- **Confidence bands.** CONTRACTS.md sec 2.1 says agents *must* be prompted with the band
  table because the Day 19 eval harness scores calibration against it. The table is in the
  system prompt verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic.types import TextBlock

from aioc.llm import LLMClient

AGENT_NAME = "incident"

# The band table is quoted from CONTRACTS.md sec 2.1 and must not drift from it - the eval
# harness (Day 19) scores calibration against these exact ranges.
_CONFIDENCE_BANDS = """\
| Range | Meaning |
|---|---|
| 0.90-1.00 | Directly evidenced by two or more independent sources |
| 0.70-0.89 | Directly evidenced by a single reliable source |
| 0.50-0.69 | Inferred from correlated signals; no direct statement |
| 0.25-0.49 | Plausible hypothesis, weak or partial evidence |
| below 0.25 | Speculation - do not state a conclusion; record it as a gap instead |"""

INCIDENT_SYSTEM_PROMPT = f"""\
You are the Incident agent of AIOC, an AI operations center. You are an expert Site
Reliability Engineer investigating production incidents: metric anomalies, error spikes,
latency regressions, failed deploys, and resource exhaustion.

Ground rules:

1. Work ONLY from the context provided in the message. You inherit nothing. If the context
   does not contain something, you do not know it - say so instead of guessing.
2. Cite evidence for every claim. Name the specific metric, log line, event, or timestamp
   that supports it. A claim with no citable evidence is a hypothesis and must be labelled
   as one.
3. Estimate confidence for every conclusion as a number from 0.0 to 1.0, calibrated
   against these bands:

{_CONFIDENCE_BANDS}

   Below 0.25 means you must not state the conclusion at all - list it under gaps as
   something that could not be determined and what data would resolve it.
4. Distinguish "looked and found nothing" from "could not look". They are different
   findings and both are worth reporting.
5. Never repeat configuration VALUES (connection strings, tokens, passwords) even if they
   appear in the context. Refer to configuration by key name only.

Structure your prose response as:
- **Summary** - one or two sentences on what is happening.
- **Findings** - each with its evidence citation and confidence number.
- **Gaps** - what you could not determine and what data would resolve it.
- **Recommended next steps** - ordered, most valuable first.

Be concise and concrete. This is an incident, not an essay."""


@dataclass(slots=True)
class IncidentProse:
    """Day 3 return shape: the agent's prose plus the accounting the later phases need."""

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class IncidentAgent:
    """Single-turn, tool-less Incident agent (Day 3 skeleton)."""

    name = AGENT_NAME

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    def investigate(self, query: str, *, context: str) -> IncidentProse:
        """Answer one operational query from the given context, in prose.

        ``context`` is the explicit context block a coordinator would record in
        ``AgentInvocation.context_passed``. It must be non-empty: an empty context means
        the caller assumed inheritance, which is exactly the failure this project exists
        to demonstrate the absence of.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not context.strip():
            raise ValueError(
                "context must be non-empty - the Incident agent inherits nothing; "
                "pass everything it needs explicitly (CONTRACTS.md sec 5, context_passed)"
            )

        prompt = f"<context>\n{context.strip()}\n</context>\n\nOperational query: {query.strip()}"
        resp = self._client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=INCIDENT_SYSTEM_PROMPT,
        )
        text = "".join(block.text for block in resp.content if isinstance(block, TextBlock))
        return IncidentProse(
            text=text,
            model=resp.model,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
