# AIOC — Enterprise AI Operations Center

A coordinator that dynamically routes operational questions to four deep subagents —
**Incident, Docs, GitHub, Deployment** — each producing schema-validated,
confidence-scored output through custom MCP tools.

> **Status: Day 1 scaffold.** The stack comes up and the contracts are frozen.
> Nothing is wired yet. See [`EXECUTION_PLAN.md`](EXECUTION_PLAN.md) for what lands when.

---

## Start here

| Document | What it answers |
|---|---|
| [`BUILD_PLAN.md`](BUILD_PLAN.md) | What we're building, and why each piece exists |
| [`EXECUTION_PLAN.md`](EXECUTION_PLAN.md) | Who builds it, on which day, and how we know it's done |
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | **Frozen.** Every data shape crossing between the two layers |

`docs/CONTRACTS.md` is frozen at `1.0.0`. Changing anything in it requires both
engineers to agree in writing, a version bump, and a changelog row.

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

That checks three things: both containers healthy, the `vector` extension
installed in Postgres, and Redis answering. The middle one matters — a plain
`postgres` image starts perfectly happily and then fails much later, inside the
retrieval code.

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
```

---

## Layout

```
src/aioc/
  contracts/       jointly owned — the executable form of docs/CONTRACTS.md
  coordinator/     intent classification, dynamic agent selection, refinement loop
  agents/          incident · docs · github · deployment
  tools/           custom MCP servers
  memory/          redis (working) · postgres (episodic) · pgvector (semantic)
  observability/   Langfuse tracing
  hitl/            human-in-the-loop approval gate and audit log
demo-app/          containerized services to break, plus chaos/ injection scripts
infrastructure/    Kubernetes manifests — documentation, not a deployment path
evaluations/       eval sets and committed results
```

`src/aioc/contracts/` is the one package both engineers import. Note that the
MCP boundary is JSON Schema, not Pydantic — a tool server must not depend on the
reasoning layer's models. See §6 of the contract.

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
