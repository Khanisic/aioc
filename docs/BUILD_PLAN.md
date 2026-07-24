# AIOC — CCA-F Evidence Build Plan

A phased plan for building the **Enterprise AI Operations Center (AIOC)** as portfolio
evidence for the **Claude Certified Architect – Foundations (CCA-F)** credential.

The goal is not "impressive README" — it is **legible proof** that each of the five
CCA-F domains is implemented and documented, while remaining a strong real-world
portfolio piece.

---

## Unifying idea

**Four deep agents are the spine, and every other decision hangs off them.**

A coordinator dynamically routes to four real subagents — **Incident, Docs, GitHub,
Deployment** — each producing schema-validated, confidence-scored output through
custom MCP tools, with the multi-agent context flow deliberately engineered to stay
lean. Heavy infrastructure (Kubernetes, Terraform, Kafka, multi-region) remains a
**documented deployment stage** shown but not over-invested in: portfolio-positive,
but off-blueprint, so it must not consume build time.

---

## Decisions locked

| Domain | Weight | Decision |
|---|---|---|
| 1 — Agentic Architecture & Orchestration | 27% | **Four deep subagents** (Incident + Docs + GitHub + Deployment) |
| 2 — Tool Design & MCP Integration | 18% | **Both** — custom MCP tools **+** a routing-refactor case study |
| 3 — Claude Code Configuration & Workflows | 20% | **Both, balanced** — team config depth **+** Claude Code in CI |
| 4 — Prompt Engineering & Structured Output | 20% | **Harden agent outputs** — schema-validated `tool_use` across all four |
| 5 — Context Management & Reliability | 15% | **Context-window engineering** (+ small provenance shore-up) |

---

## Phases

### Phase 0 — Repo + Claude Code config layer
*Set up the configuration half of Domain 3 first; it governs everything else.*

- `CLAUDE.md` hierarchy: user / project / directory scopes.
- `.claude/rules/` with glob patterns — e.g. `agents/**`, `tools/**`, `**/*.test.*`.
- Custom slash commands for common repo tasks.
- A project skill using `context: fork` and `allowed-tools` restrictions.

**Proves:** Domain 3 (configuration half).
**Portfolio:** signals you run Claude Code like a team lead, not a solo tinkerer.

---

### Phase 1 — Orchestrator + four subagents  (Domain 1, 27%)
*The core. Demoable spine as early as possible.*

- Coordinator with **dynamic selection** — invoke only the agents a query needs, not
  the full pipeline. `allowedTools` includes `Task`.
- Each subagent receives its context **explicitly in its prompt** — no automatic
  inheritance. (The single most-tested orchestration fact.)
- **Parallel** Task calls where agents are independent (Incident + Docs at once);
  **sequential** where dependent (GitHub reads the PR → Deployment diffs the release).
- **Refinement loop:** coordinator checks synthesis for gaps and re-delegates with
  targeted queries until coverage is sufficient.
- One short **"workflow vs. agent" design note** for a path where deterministic was
  chosen over agentic — the documented tradeoff *is* the certified evidence.

**Proves:** Domain 1.
**Portfolio:** a working multi-agent ops loop, not a diagram.

---

### Phase 2 — Tools layer  (Domain 2, 18%)
*"Both" = two workstreams.*

**Custom MCP tools** for the four agents, e.g.:
- Incident → `get_incident_timeline`, `correlate_events`
- Deployment → `diff_release`, `check_rollout_health`

Each tool description carries: input formats, example queries, edge cases, and a
when-to-use-vs-alternative line. Structured `isError` responses across all four
failure classes: **transient / validation / business / permission**.

**Routing-refactor case study:** deliberately build two overlapping tools that
misroute (e.g. `analyze_logs` vs `analyze_events`), capture the misrouting, then
split/rename to fix it, and write up the before/after.

**Proves:** Domain 2 (tool *design*, not just consumption).
**Portfolio:** the case study is disproportionately persuasive to a skeptical reviewer.

---

### Phase 3 — Harden agent outputs  (Domain 4, 20%)
*Dovetails with Phase 2: tools produce raw data, schemas constrain synthesis.*

- Every agent returns schema-validated output via `tool_use` + `tool_choice`.
- **Nullable fields** so absent data returns `null` instead of a fabricated value.
- Enums using the `"other"` + detail-string pattern.
- Field-level **confidence scores**.
- **Validation-retry loop:** on schema/Pydantic failure, re-request with the specific
  error attached; track retry-resolvable errors (format) vs. not (info absent).

**Proves:** Domain 4.
**Portfolio:** outputs are automation-ready and hallucination-resistant.

---

### Phase 4 — Context-window engineering  (Domain 5, 15%)
*Natural home: four agents pull huge tool outputs — logs, Prometheus series, PR diffs, k8s events.*

- **Trim verbose tool outputs**; structured fact extraction *before* content hits context.
- **Position-aware ordering** — freshest signals and the incident query where the model
  attends most.
- **Structured digests across handoffs** — pass Incident's digest to Deployment, not the
  raw dump.
- Maps onto the Redis / PostgreSQL / vector memory tiers.

**Proves:** Domain 5 (context-window sub-skill).

---

### Phase 5 — Claude Code in CI (Domain 3 workflow half) + evidence packaging

- Wire Claude Code into GitHub Actions for automated **PR review** and **test
  generation** on this repo — the real-world cousin of the GitHub agent.
- Review prompt tuned for actionable, **low-false-positive** feedback.
- **Evidence packaging:** a README section per domain — *here's the domain, here's the
  module that implements it, here's the design note.* For portfolio evidence, packaging
  matters as much as code.

**Proves:** Domain 3 (workflow half).

---

## Coverage against the CCA-F blueprint

| Domain | Status after plan | Notes |
|---|---|---|
| 1 — Orchestration (27%) | **Strong** | Dynamic routing, explicit context passing, parallel/sequential, refinement loop |
| 2 — Tool Design & MCP (18%) | **Strong** | Custom tools + structured errors + routing case study |
| 3 — Claude Code (20%) | **Strong** | Config hierarchy + CI integration (was the biggest original gap) |
| 4 — Prompt & Structured Output (20%) | **Strong** | Schema-validated `tool_use`, nullable fields, retry loop |
| 5 — Context & Reliability (15%) | **Partial → Solid** | Context-window done; shore up provenance (below) |

### Domain 5 shore-up (highest evidence-per-hour item)
The context-window pick leaves two D5 sub-skills unaddressed. Close them cheaply:
- **Provenance:** the Docs agent already does citations — add **claim → source
  mapping** and **coverage-gap reporting** there. Small lift, closes most of the gap.
- **Reliability/HITL:** let the **existing Human-in-the-Loop** design carry the
  human-review sub-skill as-is.

This moves Domain 5 from partial to solid **without opening a new workstream.**

---

## Realism & sequencing

This is the ambitious end of what could have been chosen — four deep agents with
hardened outputs and custom tools is a large build for one person. Sequencing is the
safeguard:

- **Phases 0–1** produce a demoable spine fast.
- **Every later phase ships independently** — nothing sits half-built looking like a stub.
- **If time compresses:** ship **Phases 0–3 deep**, treat **4–5 as stretch** — but keep
  the **Domain 5 shore-up**, the single best evidence-per-hour item in the plan.

---

## Out of scope (deliberately)

Kept in the repo as a documented deployment stage, **not** a build priority — these are
portfolio-positive but earn nothing on the CCA-F blueprint (the sibling exam explicitly
lists infrastructure / hosting as out of scope):

- Kubernetes, Helm, ArgoCD, Terraform
- Kafka, RabbitMQ, multi-region deployment
- NGINX / gateway tuning

---

*Plan scope: CCA-F evidence + real-world portfolio strength. Blueprint weights per the
CCA-F Foundations exam guide (Domains 1–5).*
