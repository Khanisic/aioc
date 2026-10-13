# Quickstart: from `git clone` to watching four agents diagnose a fault

This guide takes you from a fresh clone to the whole system running on your machine.
Sections 1-4 cost nothing: no API key, no Claude calls.
Section 5 runs the real agents and bills your Anthropic account; every command there says what it costs before you run it.

Everything runs locally in Docker.
There is no hosted copy.

## 1. What you need

- **Docker** with Compose v2 (Docker Desktop on Windows and macOS). Start it before anything else.
- **[uv](https://docs.astral.sh/uv/)**, which fetches Python 3.12 by itself; a system Python is not needed.
- **git**.
- About 3 GB of disk for the images, and ports 5432, 6379, 8001-8003, 8080, and 9090 free.

Commands are shown for bash.
In Windows PowerShell they are the same except for environment variables, shown as `$env:NAME = "value"` where it matters.

## 2. Install and start the stack (about 5 minutes, $0)

```bash
git clone <this repository> aioc
cd aioc
cp .env.example .env       # nothing in it needs filling in for sections 2-4
uv sync --all-groups       # creates .venv with Python 3.12 and every dependency
docker compose up -d --wait
```

That starts Postgres (with `pgvector` and the seeded incident corpus), Redis, the three demo services (`checkout-api`, `payments-api`, `inventory-api`), and Prometheus scraping them.
The first start builds the demo images and initialises the database, so it takes a few minutes; later starts take seconds.

Check that the stack is usable, not merely running:

```bash
make verify
```

Every line should say `OK`.
No `make` on Windows? Each recipe in the `Makefile` is a single command you can paste, or install it with `winget install ezwinports.make`.

**If Postgres refuses the `aioc` user with "password authentication failed"** and you have PostgreSQL installed natively, the native server is answering on 5432 instead of Docker.
Publish Docker's on another port by setting these two lines in `.env`, then `docker compose up -d --wait` again:

```
POSTGRES_PORT=55432
DATABASE_URL=postgresql://aioc:aioc_dev_only@localhost:55432/aioc
```

Exporting the same two variables in your shell works too; an exported variable wins over `.env`.
The integration tests say exactly this when they skip for this reason.

## 3. The offline tour ($0, no key)

**The test suite.** Over a thousand tests, with no network and no API key:

```bash
uv run pytest -q -m "not integration"   # the offline suite
uv run pytest -q -m integration         # the tests that use the stack you just started
make lint                               # ruff and mypy (strict)
```

**Break production and look at it.** The chaos injector puts the demo app into one of the four failure modes the agents are evaluated on:

```bash
make chaos-downstream-latency   # payments-api slows down; checkout-api's p99 follows
```

Open Prometheus at <http://localhost:9090> and graph `histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[1m])))` to watch checkout-api's p99 climb.

![checkout-api and payments-api p99 jumping from tens of milliseconds to about a second when the fault is injected at 22:53](../assets/prometheus-downstream-latency.png)

*The fault injected at 22:53 after a healthy baseline: payments-api slows, and checkout-api, which calls it, follows at once. inventory-api stays flat.*

Then put it back:

```bash
make chaos-reset
```

**Size the eval without running it.** The eval set is 38 items scored against the seeded incidents' recorded truth:

```bash
uv run python scripts/run_evals.py --list      # the cases
uv run python scripts/run_evals.py --dry-run   # every request built and priced, nothing sent
```

The committed results are in [`evaluations/baseline.md`](../../evaluations/baseline.md).

## 4. The service, in a container ($0)

The orchestrator also runs as an HTTP service, built from the repository's `Dockerfile`.
It refuses every request until you give it a token, and refuses live (paid) requests until you also switch them on.

```bash
export AIOC_API_TOKEN=pick-any-long-string     # PowerShell: $env:AIOC_API_TOKEN = "pick-any-long-string"
docker compose --profile api up -d --build --wait
curl -s localhost:8080/readyz                  # database required; the rest is reported
```

Send a `smoke` request.
It runs the real coordinator path - planning, two agents in parallel, the human-in-the-loop gate, the audit log - over scripted agents, so it makes no Claude call:

```bash
curl -si -X POST localhost:8080/v1/requests \
  -H "Authorization: Bearer $AIOC_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "is checkout healthy?"}'
# 202 Accepted, with a Location: /v1/requests/job_...

curl -s localhost:8080/v1/requests/<job id> -H "Authorization: Bearer $AIOC_API_TOKEN"
```

The job comes back `succeeded` with a contract-valid `CoordinatorResponse`, zero tokens, and the gate's decision on the scripted restart: `denied`, because a server has nobody to approve it.
The interactive API docs are at <http://localhost:8080/docs>:

![The service's API docs: index, liveness, readiness, submit a request, read a job](../assets/api-docs.png)

## 5. The live system (costs money)

Fill in `.env` (the keys are already there, commented out where optional; uncomment the ones you set):

| Key | Needed for | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | every agent | [console.anthropic.com](https://console.anthropic.com) |
| `GITHUB_TOKEN`, `GITHUB_REPO` | the GitHub and Deployment agents; `GITHUB_REPO` is `owner/name` of this repository | a fine-grained token with read access to contents and pull requests |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | optional: traces | [langfuse.com](https://langfuse.com); `LANGFUSE_HOST` is `https://us.cloud.langfuse.com` for a US-region project |
| `VOYAGE_API_KEY` | optional: vector search for the Docs agent; without it retrieval is lexical only | [voyageai.com](https://www.voyageai.com) |

Then, in order of cost:

```bash
# One agent, one call: the Incident agent diagnoses live Prometheus metrics (~$0.05; a
# diagnosis in the committed eval baseline cost $0.047 without caching)
make chaos-resource-exhaustion
uv run python scripts/check_day5_checkpoint.py
make chaos-reset

# The whole story in one command: inject a fault, read Prometheus, plan, run the agents,
# write the answer (~4 calls; measured $0.10 a run before the model-written synthesis
# was added, so expect a little more)
uv run python scripts/demo_day10.py

# All four agents through the service container: a real fault, the release that shipped
# PR #11 deployed, one live request (~$0.55)
export AIOC_API_LIVE=true                      # PowerShell: $env:AIOC_API_LIVE = "true"
docker compose --profile api up -d --wait      # restart the service with live requests on
uv run python scripts/check_day25_service.py --deploy
```

Each live script prints a transcript and records its run under `test-results/` (see [running-tests.md](running-tests.md) for reading it back).
`uv run python scripts/cost_review.py` prices every recorded run from its measured tokens.

## 6. Stopping

```bash
make chaos-reset                     # if a fault is still injected
docker compose --profile api down    # stops everything; the seeded data survives
```

`docker compose down -v` also deletes the database volume; the next start re-seeds it.
