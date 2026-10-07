# Deployment: the orchestrator as a service, the schema as migrations (Day 24)

Day 24 asked for a deploy to Railway or Render.
The owner decided on 2026-10-07 to build the deployable thing and choose the host later.
So everything a host needs exists and is proven in a container on this machine, and nothing is hosted yet.
This note records what was built, the decisions in it, and what a host still has to provide.

## What exists

| Piece | Where | Proven by |
|---|---|---|
| The HTTP service | `src/aioc/api/` (`create_app`, run with `uvicorn --factory aioc.api.app:create_app`) | `tests/test_api.py`, and the container rehearsal below |
| The zero-cost smoke pipeline | `src/aioc/api/smoke.py` (moved from `scripts/check_day9_trace.py`, which now imports it) | the same |
| The schema applier and its ledger | `src/aioc/db/schema.py`, run as `python -m aioc.db status / migrate / baseline` | `tests/test_schema_migrations.py`, eight of them against scratch databases |
| The image | `Dockerfile`, `.dockerignore` | built and run locally, 535 MB, non-root, no `.env` inside |
| The local rehearsal | `docker compose --profile api up -d --build --wait` (the `api` service, opt-in) | section "Verified" |
| Kubernetes form | `infrastructure/k8s/` (documented, never applied) | `test_the_kubernetes_manifests_match_the_service` pins the probes, the migrate command, and the settings names; `diff_release` already diffs manifests under `infrastructure/` |

## Decisions

### Requests are jobs, not long HTTP calls

A live `respond()` takes minutes: the Day 21 four-agent run was about four.
A platform's proxy can cut a request long before that, and the client would lose the answer to a timeout it did not cause.
So `POST /v1/requests` returns `202` with an id and a `Location`, the request runs on a bounded worker pool, and `GET /v1/requests/{id}` returns its status and then its result.
Nothing durable is lost by keeping jobs in memory: the durable records of a request are its trace and its audit-log rows, written while it runs.
The cost of that choice is one replica (`infrastructure/k8s/aioc.yaml` says so): a second replica would be asked for jobs it never ran.

### Closed by default, opened twice

A public URL that runs `respond()` spends about a dollar a request.
So nothing but `/`, `/healthz`, and `/readyz` answers until `AIOC_API_TOKEN` is set, a blank token counts as unset, and a `live` request also needs `AIOC_API_LIVE=true`.
Requests run one at a time by default and at most four may be waiting or running (`429` after that), and refinement rounds are capped at zero, because every one of those is spend that can be committed before anyone looks.
What the server caps it refuses rather than trims: a request for more rounds than allowed is a `400` that names the setting, never a quieter run than was asked for.

### A smoke request is the real path with scripted agents

`mode: smoke` (the default) runs `respond()` exactly as a live request does - the plan span, the parallel group on the real `Executor`, the deterministic synthesis, the HITL gate, the audit log, the tracer - with a scripted plan and two scripted agents.
Zero Claude calls.
The scripted Incident report carries one production write (a restart) so the gate has something to deny and the audit log has a row to prove it; every scripted summary and action starts `SCRIPTED (smoke test)`.
This is HANDOFF sec 1's "smoke test: fake agents", made a property of the deployed service rather than a script, so Day 25's checkpoint can be checked for free before the one paid run.

### The gate's approver on a server is `DenyAll`

A deployed service has nobody at a console.
Every production write an agent recommends is withheld and recorded as withheld; approving one is a human act this service does not offer.
The decisions come back beside the response (`approvals`), never inside the frozen `CoordinatorResponse`.

### The migration tool is a ledger over the files that already exist

`docker/postgres/init/` runs only on a fresh local volume, never on a hosted database, and `02-incidents.sql` fails on a second run on purpose.
The options were Alembic (needs SQLAlchemy, which nothing here uses, and would re-express SQL files as Python), yoyo or sqitch (a new tool and a second copy of the schema in its format), or treating the existing numbered files as the migrations.
The last won: `python -m aioc.db migrate` keeps a `schema_migrations` table of each file's name and sha256, applies only files the ledger lacks, in filename order, each in one transaction with its ledger row, and refuses - before applying anything - when an applied file's bytes have changed or an applied file has disappeared.
The one rule it asks of authors is the rule every migration tool asks: a change to an applied file goes in a new, later-numbered file.
The files keep working as `docker-entrypoint-initdb.d` scripts unchanged, so the local stack still initialises itself; a database `init/` built is recorded once with `baseline`, which refuses unless the tables are there.
No rollback is offered, deliberately: the seeded corpus is the eval's answer key, and a down-migration that drops it is a footgun rather than a feature.

### One image carries every tool server

The agents launch their MCP servers as subprocesses (`aioc.llm.mcp`), so the servers are not separate services and the image needs nothing extra.
The two things a hosted deployment cannot reach are the demo app's containers (`docker compose logs`, which no agent consumes yet) and a Prometheus scraping them.
The Day 24 deployment note in `docs/EXECUTION_PLAN.md` already chose the honest split: the live URL demonstrates the orchestrator; the incident flow is demonstrated against the local stack.
`/readyz` reports Prometheus as unreachable rather than failing, and the Deployment agent's health tool answers `PROMETHEUS_UNAVAILABLE` rather than guessing.

## What a host has to provide

- A container runtime that sets `$PORT` (Render, Railway, Fly, Cloud Run, and Kubernetes all do).
- Postgres 17 with the `vector` extension available (Neon, Supabase, Render Postgres, Railway's pgvector template); `01-extensions.sql` fails loudly if it is not.
- A pre-deploy step: `python -m aioc.db migrate` in the same image.
- The environment: `DATABASE_URL`, `AIOC_API_TOKEN`, and for live requests `ANTHROPIC_API_KEY` and `AIOC_API_LIVE=true`; optionally `VOYAGE_API_KEY`, the three `LANGFUSE_*` (the host must be the US one for this account, HANDOFF sec 4), `GITHUB_TOKEN` and `GITHUB_REPO`, and `PROMETHEUS_URL`. `infrastructure/k8s/secret.example.yaml` is the list.
- Health checks: `/healthz` for liveness, `/readyz` for readiness.

## Verified (2026-10-07, on this machine, $0 of Claude calls)

- The image builds, runs as uid 10001, and holds no `.env`.
- `python -m aioc.db migrate` on a fresh scratch database applied all five files: 18 incidents, 65 timeline events, the audit table with its triggers; a second run applied nothing; `status` left the database untouched; `baseline` recorded the stack's own database, and the same command inside the image then reported it up to date.
- `/readyz` from the container: the database (18 incidents), the Anthropic key configured, Langfuse configured, Prometheus answering by service name.
- The refusals: no token and a wrong token `401`, `live` with the switch off `403`, rounds over the cap `400`, an unknown field `422`.
- A smoke request: `202` with its `Location`, `succeeded` 2.4 s later with both 1.5-second scripted agents run concurrently, a contract-valid `CoordinatorResponse` at zero tokens, the scripted restart denied and written to the Postgres audit log, and the trace in Langfuse with five observations (the request, `plan`, both agents overlapping, `hitl_gate`), each carrying `input_tokens` / `output_tokens` in its metadata - the first read-back since the Day 23 fix (HANDOFF item 49).
  Langfuse took more than two minutes to make the trace readable; a check that reads a trace back has to wait for it.
- Found by the rehearsal and fixed before anything was committed: an authorisation dependency declared as a type alias inside the app factory could not be resolved under postponed annotations, so FastAPI read it as a required query parameter and every request was a `422`. The routes now declare it as a route dependency.
