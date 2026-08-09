"""Coordinator: intent classification and dynamic agent selection (Day 6).

The coordinator is the spine of the project and every behaviour here is graded CCA-F evidence
(Domain 1, 27%). This module is the *planning* half - deciding which agents a query needs and
what each one gets told. Executing the plan (Task delegation) is Day 7; the refinement loop is
Day 14.

Four behaviours, and each is enforced in code rather than merely prompted, because a prompt
that usually works is not evidence:

- **Dynamic selection.** Every agent not invoked is recorded in `skipped_agents` with a
  reason. `SelectionPlan` rejects a plan that covers no agents or leaves one unaccounted for,
  so "selected everything" and "forgot to record a skip" both fail loudly. An empty
  `skipped_agents` on a typical query means dynamic selection is not working.
- **Explicit context passing.** Each `AgentInvocation.context_passed` must be non-empty, and
  the contract model enforces that. This module additionally rejects a context that merely
  echoes the query, because a plan that passes the query back as context is inheritance
  wearing a disguise - it tells the subagent nothing the coordinator learned.
- **Parallel vs sequential.** `mode: parallel` requires `depends_on: []`; `mode: sequential`
  requires a non-empty `depends_on` whose ids all resolve within the plan. The contract
  checks the first two; the resolution check is here, since it needs the whole plan.
- **Honest intent confidence.** `intent` is an `Assessment`, so a query the coordinator cannot
  classify gets `value: null` and a `Gap`, not a guess at `other`.

Model choice: the coordinator reads `AIOC_COORDINATOR_MODEL` and falls back to the harness
default. Classification plus a short plan is a far smaller job than a full incident diagnosis,
so this is the natural place for a cheaper model - the Day 23 routing experiment measures
exactly that, and this seam is what makes it a config change rather than a rewrite.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from anthropic.types import Message, ToolUseBlock
from pydantic import Field, model_validator

from aioc.contracts import (
    AgentInvocation,
    AgentName,
    Assessment,
    Gap,
    Intent,
    InvocationMode,
    SkippedAgent,
    StrictModel,
)
from aioc.llm import LLMClient, LLMSettings, ToolResult, ToolSpec, Usage

SELECT_TOOL_NAME = "select_agents"

ALL_AGENTS: tuple[AgentName, ...] = (
    AgentName.INCIDENT,
    AgentName.DOCS,
    AgentName.GITHUB,
    AgentName.DEPLOYMENT,
)

# What each agent is for, in the coordinator's own words. This doubles as the routing
# instruction and as the reason text the model has to justify against, so keeping the two in
# one place is what stops the prompt from claiming a capability the agent does not have.
_AGENT_CAPABILITIES = """\
- `incident`: diagnoses live operational problems from metrics, logs, and events. Failure
  modes, root cause, severity, blast radius, remediation. Reads Prometheus and the historical
  incident corpus. Use it when something is broken or degraded now, or when the query asks
  why a system behaved a certain way.
- `docs`: answers from the retrieved runbook and documentation corpus, citing every claim.
  Use it for "how do we...", "what is the procedure for...", policy, and ownership questions.
  It cannot observe live systems.
- `github`: reads repositories, pull requests, commits, and diffs. Use it when the query
  concerns a code change - what shipped, what a PR does, which commit introduced something.
  It cannot see runtime behaviour.
- `deployment`: compares releases and checks rollout health. Use it for "is the rollout
  healthy", "what changed between these versions", and rollback decisions. It reads release
  metadata and rollout status, not source code."""

SELECTION_SYSTEM_PROMPT = f"""\
You are the coordinator of AIOC, an AI operations centre. You do not answer operational
questions yourself. You decide which specialist agents a query needs, and you write the
context each one receives.

The four agents available to you:

{_AGENT_CAPABILITIES}

Call the `{SELECT_TOOL_NAME}` tool exactly once. Do not write prose outside the tool call.

Rules, all of which are validated after you answer:

1. Invoke ONLY the agents the query actually needs. Every agent you do not invoke must appear
   in `skipped_agents` with a specific reason - not "not needed", but why this query does not
   require that agent. Selecting all four on a narrow question is a failure, and so is
   omitting an agent from both lists.
2. For each selected agent, write `context_passed`: the facts, constraints, and scope that
   agent needs to do its job. The agent inherits NOTHING - it sees only this text and its own
   query. Restating the user's question is not context. Include what you know that the agent
   does not: the services involved, the time window, what has already been ruled out, what
   specifically you want back.
3. Set `mode` to `parallel` for agents that can run independently, and leave `depends_on`
   empty for those. Set `mode` to `sequential` only when an agent genuinely needs another's
   output first, and list the `invocation_id` values it waits on in `depends_on`. The common
   sequential case is github reading a pull request before deployment can diff the release it
   belongs to. Do not serialise work that could run in parallel.
4. Classify `intent` with a confidence in [0, 1]. If the query is genuinely ambiguous between
   two intents, use `mixed`. If you cannot classify it at all, set `intent.value` to null and
   add a gap explaining what would disambiguate it - do not guess `other`.
5. `reason` on each selected agent states what that agent will contribute to the answer, not
   what the query says.

Prefer fewer agents. An unnecessary invocation costs a full agent run and dilutes the
synthesis with material nobody asked for."""


class SelectionPlan(StrictModel):
    """The plan the coordinator model produces - the routing subset of `CoordinatorResponse`.

    Not the full envelope: `agent_responses`, `synthesis`, `answer`, and `cost` are filled in
    after the agents actually run (Day 7 onward). Splitting the plan out means the model is
    asked for exactly the decisions it is competent to make, and the parts that are facts
    about execution are never model-authored.
    """

    intent: Assessment[Intent]
    selected_agents: list[AgentInvocation] = Field(default_factory=list)
    skipped_agents: list[SkippedAgent] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> SelectionPlan:
        selected = [inv.agent for inv in self.selected_agents]
        skipped = [s.agent for s in self.skipped_agents]

        duplicates = {a for a in selected if selected.count(a) > 1}
        if duplicates:
            raise ValueError(f"agent invoked more than once: {sorted(a.value for a in duplicates)}")

        overlap = set(selected) & set(skipped)
        if overlap:
            raise ValueError(f"agent both selected and skipped: {sorted(a.value for a in overlap)}")

        # Every agent must be accounted for. This is what makes dynamic selection auditable:
        # a missing agent is indistinguishable from an agent nobody considered.
        unaccounted = set(ALL_AGENTS) - set(selected) - set(skipped)
        if unaccounted:
            raise ValueError(
                "every agent must be either selected or skipped with a reason; missing: "
                f"{sorted(a.value for a in unaccounted)} (CONTRACTS.md sec 5)"
            )

        if not self.selected_agents:
            raise ValueError("a plan must invoke at least one agent")

        # depends_on must resolve inside this plan, or the executor deadlocks on Day 7
        # waiting for an invocation that will never run.
        ids = {inv.invocation_id for inv in self.selected_agents}
        for inv in self.selected_agents:
            unknown = [dep for dep in inv.depends_on if dep not in ids]
            if unknown:
                raise ValueError(
                    f"{inv.agent.value}.depends_on references unknown invocation_id(s) "
                    f"{unknown}; it must name an invocation in this same plan"
                )
            if inv.invocation_id in inv.depends_on:
                raise ValueError(f"{inv.agent.value} depends on itself")

        # No cycles. Parallel invocations have empty depends_on by contract, so a cycle can
        # only form among sequential ones - and a cyclic plan is one the Day 7 executor could
        # never start, since every member waits on another. Kahn's algorithm: repeatedly
        # release invocations whose dependencies are all released; a leftover is a cycle.
        released: set[str] = set()
        pending = list(self.selected_agents)
        while pending:
            runnable = [inv for inv in pending if set(inv.depends_on) <= released]
            if not runnable:
                stuck = sorted(inv.invocation_id for inv in pending)
                raise ValueError(
                    f"circular depends_on among {stuck}; no execution order can satisfy it"
                )
            released.update(inv.invocation_id for inv in runnable)
            pending = [inv for inv in pending if inv.invocation_id not in released]

        # A null intent must be explained, mirroring the envelope's null-needs-a-Gap rule.
        if self.intent.value is None and not self.gaps:
            raise ValueError(
                "intent.value is null but no gap explains it - a null must never be an "
                "unexplained guess (CONTRACTS.md sec 1)"
            )
        return self

    @property
    def parallel_group(self) -> list[AgentInvocation]:
        """Invocations with no dependencies - the ones Day 9 fires in a single response."""
        return [i for i in self.selected_agents if i.mode is InvocationMode.PARALLEL]

    @property
    def sequential_chain(self) -> list[AgentInvocation]:
        return [i for i in self.selected_agents if i.mode is InvocationMode.SEQUENTIAL]


class CoordinatorError(RuntimeError):
    """The model did not return a usable plan (no forced tool call, or truncated output)."""


def _select_never_runs(_args: dict[str, Any]) -> ToolResult:
    # Forced via tool_choice and read directly off the tool_use block, so this never fires.
    raise CoordinatorError(f"{SELECT_TOOL_NAME} is a structured-output tool; it is never executed")


_ROOT = "$root"

# Same technique as the Incident agent's emit tool: a generated schema states shape but not
# cross-field rules, and the rules are what the model gets wrong. See
# aioc.agents.incident._apply_guidance for the measurement that motivated this.
_FIELD_GUIDANCE: dict[str, dict[str, str]] = {
    _ROOT: {
        "intent": "Your classification of what the query is asking for.",
        "selected_agents": "Only the agents this query needs. Prefer fewer.",
        "skipped_agents": (
            "Every agent NOT in `selected_agents`, each with a specific reason. All four "
            "agents must appear across the two lists exactly once."
        ),
        "gaps": "Required if `intent.value` is null. Explain what would disambiguate it.",
    },
    "AgentInvocation": {
        "invocation_id": (
            "Opaque id starting `inv_`. Other invocations reference this in `depends_on`."
        ),
        "reason": "What this agent will contribute to the answer, not what the query says.",
        "mode": (
            "`parallel` when this agent can start immediately, `sequential` when it needs "
            "another invocation's output first."
        ),
        "depends_on": (
            "Empty for `parallel`. For `sequential`, the `invocation_id` values this agent "
            "waits on - they must be invocations in this same plan."
        ),
        "context_passed": (
            "The facts this agent needs. It inherits nothing and sees only this text plus its "
            "query. Restating the user's question is not context: name the services, the time "
            "window, what is already ruled out, and what you want back."
        ),
        "round": "0 for the initial plan. The refinement loop increments it.",
    },
    "SkippedAgent": {
        "reason": (
            "Why this specific query does not need this agent. Not 'not needed' - say what it "
            "would have contributed and why that is irrelevant here."
        ),
    },
    "Assessment_Intent_": {
        "value": "Null only if you genuinely cannot classify; then add a gap.",
        "confidence": "Your confidence in the classification, 0 to 1.",
        "evidence": "Leave empty. Intent is derived from the query text, not from evidence.",
        "reasoning": "One line: which words in the query drove the classification.",
        "detail": "Null unless `value` is exactly `other`.",
    },
    "Gap": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "resolvable": "False if no agent or extra data could close this gap.",
    },
}


def _apply_guidance(schema: dict[str, Any]) -> dict[str, Any]:
    """Attach cross-field guidance to the generated schema, failing at import on drift."""
    from copy import deepcopy

    annotated: dict[str, Any] = deepcopy(schema)
    annotated["description"] = (
        "Your routing decision for this query. Every property below is a top-level argument "
        "of this tool - pass them directly, do not nest them in a wrapper object.\n\n"
        "Validated after you answer, and a violation rejects the whole plan:\n"
        "1. All four agents appear exactly once across `selected_agents` and `skipped_agents`.\n"
        "2. `parallel` invocations have an empty `depends_on`; `sequential` ones do not, and "
        "every id they name must exist in this plan.\n"
        "3. `context_passed` is non-empty and is not just a restatement of the query."
    )

    missing: list[str] = []
    for def_name, fields in _FIELD_GUIDANCE.items():
        target = annotated if def_name is _ROOT else annotated.get("$defs", {}).get(def_name)
        properties = (target or {}).get("properties", {})
        for field, text in fields.items():
            if field not in properties:
                missing.append(f"{def_name}.{field}")
                continue
            properties[field]["description"] = text
    if missing:
        raise RuntimeError(
            "coordinator select schema guidance is out of sync with aioc.contracts; "
            f"no such field: {', '.join(sorted(missing))}"
        )
    return annotated


_SELECT_SCHEMA: dict[str, Any] = _apply_guidance(SelectionPlan.model_json_schema())

_SELECT_TOOL = ToolSpec(
    name=SELECT_TOOL_NAME,
    description=(
        "Emit the routing plan for this query as structured data. Call this exactly once; it "
        "is the only way to answer. Every agent must be either selected or skipped with a "
        "reason, and each selected agent needs the explicit context it will receive."
    ),
    input_schema=_SELECT_SCHEMA,
    handler=_select_never_runs,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _coordinator_settings() -> LLMSettings:
    """`AIOC_COORDINATOR_MODEL` overrides the harness default for the coordinator only.

    The seam the Day 23 routing experiment measures: coordinator and subagents can run
    different models without either half of the code knowing about it.
    """
    override = os.environ.get("AIOC_COORDINATOR_MODEL")
    return LLMSettings(model=override) if override else LLMSettings()


class Coordinator:
    """Plans which agents a query needs. Executing the plan lands on Day 7."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient(_coordinator_settings())

    def plan(
        self, query: str, *, situation: str | None = None, usage: Usage | None = None
    ) -> SelectionPlan:
        """Classify the query and choose agents.

        `situation` is anything the coordinator already knows that should inform routing - a
        live metrics summary, the current on-call state. It is passed to the model but is not
        automatically forwarded to the agents: the model must decide what each agent needs and
        write it into that agent's `context_passed`. That asymmetry is the whole point of
        explicit context passing, so it is deliberate rather than an oversight.

        `usage` is the Day 7 cost seam: pass the request's `Usage` accumulator and the
        planning call's tokens are added to it, so `CoordinatorResponse.cost` covers planning
        as well as the agent runs. Tokens are added even when the plan fails validation -
        a rejected plan still cost real tokens.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")

        prompt = f"Operational query: {query.strip()}"
        if situation and situation.strip():
            prompt = f"<situation>\n{situation.strip()}\n</situation>\n\n{prompt}"

        resp = self._client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=SELECTION_SYSTEM_PROMPT,
            tools=[_SELECT_TOOL],
            tool_choice={"type": "tool", "name": SELECT_TOOL_NAME},
        )
        if usage is not None:
            usage.input_tokens += resp.usage.input_tokens
            usage.output_tokens += resp.usage.output_tokens
        if resp.stop_reason == "max_tokens":
            raise CoordinatorError(
                f"{SELECT_TOOL_NAME} output was truncated at the max_tokens limit "
                f"({resp.usage.output_tokens} output tokens); the plan is incomplete. "
                "Raise AIOC_MAX_TOKENS."
            )
        payload = _extract_tool_input(resp, SELECT_TOOL_NAME)
        plan = SelectionPlan.model_validate(payload)
        _reject_echoed_context(plan, query)
        return plan

    @staticmethod
    def new_request_id() -> str:
        return _new_id("req")


def _reject_echoed_context(plan: SelectionPlan, query: str) -> None:
    """Fail a plan whose `context_passed` merely restates the query.

    The contract enforces non-empty, which catches an omission but not the subtler failure: a
    coordinator that "passes context" by echoing the question has told the subagent nothing it
    would not have had anyway, which is implicit inheritance with extra steps.
    """
    normalised = " ".join(query.lower().split())
    for inv in plan.selected_agents:
        passed = " ".join(inv.context_passed.lower().split())
        if passed == normalised or (len(passed) < len(normalised) * 1.2 and normalised in passed):
            raise ValueError(
                f"{inv.agent.value}.context_passed only restates the query, which is not "
                "context - the subagent must be told what the coordinator knows that it does "
                "not (CONTRACTS.md sec 5)"
            )


def _extract_tool_input(resp: Message, tool_name: str) -> dict[str, Any]:
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == tool_name:
            if not isinstance(block.input, dict):
                raise CoordinatorError(f"{tool_name} input was not a JSON object")
            return dict(block.input)
    raise CoordinatorError(
        f"model did not call {tool_name} (stop_reason={resp.stop_reason!r}); no plan returned"
    )


def utcnow() -> datetime:
    """Single clock for the coordinator, so `received_at`/`completed_at` are comparable."""
    return datetime.now(UTC)
