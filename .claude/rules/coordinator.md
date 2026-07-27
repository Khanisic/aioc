---
paths:
  - "src/aioc/coordinator/**"
---
# Reasoning Layer - coordinator

The coordinator is the spine. Each behaviour below is graded CCA-F Domain 1 evidence, so it is
enforced by `CoordinatorResponse` (CONTRACTS.md sec 5), not left to prompt wording.

- Dynamic selection: invoke only the agents a query needs. Every non-invoked agent gets a
  `SkippedAgent` entry with a real reason. An empty `skipped_agents` on a typical query means
  dynamic selection is not working - treat it as a bug, not a clean run.
- Explicit context passing: build each subagent's context yourself and record the literal block in
  `AgentInvocation.context_passed`. It must be non-empty. Never rely on a subagent inheriting the
  coordinator's context; that is the exact failure this project exists to demonstrate the absence of.
- `mode` and `depends_on` move together. `parallel` requires `depends_on: []`; `sequential` requires
  a non-empty `depends_on`. Independent agents (Incident + Docs) go parallel in a single response;
  dependent ones (GitHub reads the PR, then Deployment diffs the release) go sequential.
- The refinement loop consumes `Gap.suggested_agent` + `Gap.suggested_query` directly - it does not
  re-derive a query from prose. Stop on `Gap.resolvable: false` and surface the gap in
  `unresolved_gaps` rather than spending another round on it. Bump `refinement_rounds` per round.
- Coordinator evidence ids resolve against the union of the subagents' `evidence[]`. The coordinator
  cites its subagents; it does not duplicate their evidence into its own list.
- `intent` is the one Assessment exempt from the evidence requirement: it is derived from the query
  text alone and may carry `evidence: []` at any confidence.
- When a deterministic workflow would do the job, say so in a design note rather than reaching for an
  agent. The documented workflow-vs-agent tradeoff is itself the Domain 1 evidence.
