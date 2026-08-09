# AIOC — Enterprise AI Operations Center

A coordinator that dynamically routes operational questions to four deep subagents —
**Incident, Docs, GitHub, Deployment** — each producing schema-validated,
confidence-scored output through custom MCP tools.

> **Status: Day 7.** The coordinator now **plans and delegates**: it selects agents
> (dynamic selection and explicit context passing enforced by validators, not prompts),
> hands each one exactly the context it wrote into the plan, and assembles a contract
> `CoordinatorResponse` - with honest `Gap`s, never fabricated responses, for the three
> agents that do not exist yet. Two MCP tools run as real stdio servers
> (`get_incident_timeline`, `correlate_events`), all four error classes return
> distinctly, and chaos ground-truth signals are permission-gated at every tool.
> The Docs agent and retrieval are next.
> See [`EXECUTION_PLAN.md`](docs/EXECUTION_PLAN.md) for what lands when.

---

## Start here

| Document | What it answers |
|---|---|
| [`BUILD_PLAN.md`](docs/BUILD_PLAN.md) | What we're building, and why each piece exists |
| [`EXECUTION_PLAN.md`](docs/EXECUTION_PLAN.md) | Who builds it, on which day, and how we know it's done |
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | **Frozen.** Every data shape crossing between the two layers |
| [`docs/guides/`](docs/guides/) | How-to guides: [running the tests](docs/guides/running-tests.md), [the incidents table](docs/guides/incidents-table.md) |
| [`docs/interview-prep/`](docs/interview-prep/README.md) | War stories, decisions, and the measured numbers behind them |

`docs/CONTRACTS.md` is frozen at `1.0.0`. Changing anything in it requires a written
rationale recorded before the code changes, the superseded text struck through rather
than deleted, a version bump, and a changelog row.

---

## Running it locally

**Prerequisites:** Docker, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
cp .env.example .env      # nothing needs filling in for the steps below
uv sync --all-groups
docker compose up -d --wait
```

Verify the stack is actually usable, not merely running:

```bash
make verify
```

That checks the stack is *usable*: containers healthy, the `vector` extension
installed, Redis answering, all three demo services exposing metrics, Prometheus
actually scraping them, and the incident corpus seeded and covering every failure
mode. The extension check matters because a plain `postgres` image starts perfectly
happily and then fails much later, inside the retrieval code — and the coverage check
matters because a failure mode with no seed rows cannot be scored by the eval at all.

No `make` on Windows? Every recipe in the [`Makefile`](Makefile) is a single
command you can paste directly, or install it with
`winget install ezwinports.make`.

### Useful commands

```bash
make up            # start the stack, wait for health
make down          # stop it, keep the data
make db-reset      # DESTRUCTIVE: drop volumes, re-run docker/postgres/init
make psql          # postgres shell
make lint          # ruff + mypy
make test          # unit tests only (skips those needing the stack)

make chaos-downstream-latency   # inject a failure mode (four exist; see the Makefile)
make chaos-reset                # return the demo app to a healthy baseline

# live checks (need ANTHROPIC_API_KEY; these cost real tokens, one call each)
uv run python scripts/check_structured_output.py    # does diagnose() hold the contract, per model?
uv run python scripts/check_day5_checkpoint.py      # chaos injected -> valid JSON, scored vs truth
uv run python scripts/check_agent_selection.py      # coordinator routing (2 of 5 cases by default)

# the custom MCP tools, over stdio
uv run python -m aioc.tools.incident.timeline_server
uv run python -m aioc.tools.incident.correlate_server
```

### Test results

Every `pytest` run records itself under [`test-results/`](test-results/README.md) as JSON: what
ran, what failed, how long it took, and against which commit. Any other command can be wrapped:

```bash
uv run python scripts/runlog.py --kind lint --name ruff -- uv run ruff check .
grep '"outcome": "failed"' test-results/index.jsonl   # every failing run, newest last
```

The records are gitignored - they are machine-local evidence, not source. `AIOC_RUNLOG=0` opts
out. See that directory's README for the record schema.

---

## Layout

```
.claude/           rules, slash commands, skills, and the shared permission layer
src/aioc/
  contracts/       jointly owned — the executable form of docs/CONTRACTS.md
  llm/             Claude API harness - messages, streaming, the tool_use loop
  coordinator/     planner (selection) + executor (delegation), refinement loop later
  agents/          incident · docs · github · deployment
  tools/           custom MCP servers — envelope, chaos policy gate, incident/ servers
  memory/          redis (working) · postgres (episodic) · pgvector (semantic)
  observability/   prometheus reads (live) · Langfuse tracing (Day 9)
  hitl/            human-in-the-loop approval gate and audit log
demo-app/          containerized services to break, plus chaos/ injection scripts
infrastructure/    Kubernetes manifests — documentation, not a deployment path
evaluations/       eval sets and committed results
scripts/           dev tooling — run logging, live API checks
test-results/      structured records of every run (gitignored; schema in its README)
```

`src/aioc/contracts/` is the one package both layers import. Note that the
MCP boundary is JSON Schema, not Pydantic — a tool server must not depend on the
reasoning layer's models. See §6 of the contract.

---

## Working on this repo with Claude Code

`.claude/` is committed on purpose - it's the Domain 3 evidence, not local scratch.
Path-scoped rules in `.claude/rules/` load only when you open a file they cover, so the
per-area conventions cost nothing on sessions that don't touch that area. `/contract`
surfaces the governing rules before you change part of the contract, and `/validate-schema`
checks a JSON payload against the Pydantic models.

`.claude/settings.json` is the shared permission layer: the inner dev loop runs without
prompting, the three destructive commands (`make db-reset`, `docker compose down -v`,
`git push`) prompt, and `.env`/`secrets/`/`*.pem`/`*.key` are unreadable. Your own
overrides go in `.claude/settings.local.json`, which is gitignored.

See [`docs/design-notes/domain-3-config-layer.md`](docs/design-notes/domain-3-config-layer.md)
for why each piece sits where it does.

---

## CCA-F domain evidence

<!-- Day 26: domain → implementing module → design note. -->
*Filled in on Day 26, once there is something to point at.*

| Domain | Weight | Implemented in | Design note |
|---|---|---|---|
| 1 — Agentic Architecture & Orchestration | 27% | — | — |
| 2 — Tool Design & MCP Integration | 18% | — | — |
| 3 — Claude Code Configuration & Workflows | 20% | — | — |
| 4 — Prompt Engineering & Structured Output | 20% | — | — |
| 5 — Context Management & Reliability | 15% | — | — |
