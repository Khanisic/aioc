# How to run and read the tests

Two kinds of check live in this repo, and the distinction matters because one of them costs money.

| | Offline suite | Live checks |
|---|---|---|
| What | `pytest`, `ruff`, `mypy` | Real Claude API calls, real Docker stack |
| Needs | Nothing but `uv sync` | An API key and/or the stack up |
| Cost | Zero | Real tokens, per call |
| Run it | Constantly | Deliberately |

Everything under "The offline suite" is free.
Everything under "The live checks" bills.

## The offline suite

```bash
uv sync --all-groups          # once, or after a dependency change
uv run pytest -q              # 1056 tests, no network, no API key; 17 skip without the Docker stack
```

Selecting a subset:

```bash
uv run pytest -q -m "not integration"          # skip anything needing Docker (what `make test` does)
uv run pytest -q tests/test_incident_agent.py  # one file
uv run pytest -q -k "truncat"                  # substring match on test names
uv run pytest "tests/test_contract.py::test_worked_example_validates_and_round_trips"
uv run pytest -q -x --lf                       # stop at first failure, then rerun just the failures
```

Lint and types:

```bash
uv run ruff check .           # lint
uv run ruff format .          # apply formatting (use --check in CI)
uv run mypy                   # strict; covers src/aioc only, per pyproject
```

`make test` and `make lint` wrap these, but **GNU Make is not installed on this machine** (`make: command not found`).
Every recipe in the `Makefile` is a single pasteable command; install Make with `winget install ezwinports.make` if you would rather use it.

Note the mypy scope: `pyproject.toml` sets `packages = ["aioc"]`, so `demo-app/`, `scripts/`, `examples/`, and `tests/` are linted by ruff but **not** type-checked.
A type error in `scripts/runlog.py` will not fail `mypy`.

### In CI

`.github/workflows/ci.yml` runs the same three steps on every push to `main` and every pull request: `ruff check` and `ruff format --check`, `mypy`, and `pytest -q -m "not integration"`.
It needs no secrets, because the suite makes no network calls, and it deselects the 17 `integration` tests rather than letting them wait on a database that is not there.
A test that passes here and fails there is most likely reading a file only this machine has; the workflow was rehearsed in a fresh clone with no `.env` before it was committed, and that is the way to reproduce a CI failure locally.

Every scripted fake client that stands in for `messages.create` calls `tests.wire.check_conversation` on what it is sent, so a test fails where the real API would answer 400.
A new fake should do the same.

### What each test file covers

| File | Tests | Covers |
|---|---|---|
| `tests/test_contract.py` | 26 | The Pydantic models against the CONTRACTS.md §8 worked example, plus one negative test per validated invariant |
| `tests/test_llm_harness.py` | 13 | `LLMClient.complete` / `stream_text` / `run_tool_loop` against a scripted fake client |
| `tests/test_incident_agent.py` | 19 | Both Incident agent paths - Day 3 prose and Day 4 `diagnose` - including payloads that must be rejected |
| `tests/test_chaos_inject.py` | 4 | The chaos injector's failure-mode to knob mapping, offline |
| `tests/test_seed_corpus.py` | 13 | The Day 5 incident corpus SQL against the contract enums, offline |
| `tests/test_prometheus_context.py` | 15 | Metric reads and context rendering (fake httpx transport), including the `chaos_knob_value` leak guards |
| `tests/test_coordinator.py` | 29 | Day 6 selection planning; mostly negative tests, one per enforced orchestration rule |
| `tests/test_executor.py` | 23 | Day 7 delegation (exact context passing, honest gaps) plus Day 9 concurrency (a barrier proves overlap), per-runner usage accounting, and the fake-tracer span assertions |
| `tests/test_timeline_tool.py` | 28 | Day 6 MCP tool: wire envelope, error taxonomy, description template (4 need the stack) |
| `tests/test_correlate_tool.py` | 30 | Day 7 MCP tool: validation, chaos gate, correlation math, all four error classes distinctly (3 need the stack) |
| `tests/test_docs_agent.py` | 18 | Day 8 Docs agent: rendering, grounding rejections, stamped coverage |
| `tests/test_retrieval.py` | 27 | Day 8 retrieval: stale detection, RRF fusion, Voyage client offline (3 need the stack) |
| `tests/test_tracing.py` | 10 | Day 9 tracing seam: null-object degradation, the Langfuse adapter against a stub client, the `.env` path regression |
| `tests/test_github_agent.py` | 20 | Day 11 GitHub agent: the two-phase tool loop, fact stamping, grounding rejections, error-class recording |
| `tests/test_github_tool.py` | 41 | Day 11 GitHub tools against an httpx `MockTransport`: all four error classes, keys-only redaction, output bounds, the template |
| `tests/test_mcp_toolset.py` | 9 | Days 11-12 MCP client seam over the real stdio wire (server subprocesses with blanked credentials, so no network) |
| `tests/test_deployment_agent.py` | 24 | Day 12 Deployment agent: fact stamping, grounding rejections, the not-looked-is-a-gap rules, `requires_approval` stamped |
| `tests/test_deployment_tool.py` | 73 | Day 12 deployment tools against a fake two-ref repository and a scripted Prometheus: the structural keys-only diff, every sec 7.3/7.4 code, the status rule, null-never-zero |
| `tests/test_retry_loop.py` | 24 | Day 17 validation-retry loop: the error attached as an error `tool_result` on the refused `tool_use`, format and grounding feedback told apart, the cap and the identical-rejection stop, what is never retried, tokens charged per attempt, the record and its summary, the executor's gap keeping the loop's note |
| `tests/test_audit_log.py` | 17 | Day 17 audit log: every decision written before it is returned, a release the log refused coming back denied, `respond(gate=...)` on its own span, the replay and read scripts, and the Postgres store's round trip and append-only triggers (3 need the stack) |
| `tests/test_confidence.py` | 26 | Day 18 field-level confidence: the band table pinned to the contract's text and the agents' prompt, band boundaries, every judgement read with its path (assessments and Docs claims), the three flags each way, the intent's exemption, the request profile, rendering, and the digest line |
| `tests/test_provenance.py` | 16 | Day 18 Docs provenance: the worked example's claim traced to its evidence and retrieval call, chunk match over document match, a source with no evidence shown as none, non-document evidence kept out, coverage gaps by index then in order with `None` for a question no gap reports, rendering, the digest pairing, and the free report script |
| `tests/test_prompt_caching.py` | 27 | Day 19 caching and pricing: where the cache marker goes and where it does not, the prefix byte-identical across requests that differ in everything else, no system prompt carrying a date or an id, the three-part prompt count put back together, the executor's cost carrying the counters, and the price of a read, a write, and a batch |
| `tests/test_batch.py` | 22 | Day 19 Batch API against a scripted batch endpoint: request keys, the wire (polling on an injected clock, results read by id from a reversed list), `DeferredClient`, and the shipped Incident agent run unchanged through a batch, including a refused report retried as a second batch |
| `tests/test_evals.py` | 98 | Day 19 eval harness: the seed parser, the committed set and eleven case files that must be refused, the leak guard, scoring each way with the denominators checked, calibration per band, the runner realtime and batched, the report and its three prices (1 needs the stack) |
| `tests/test_baseline.py` | 51 | Day 20 baseline, against a scripted model that bills like the real one: the cache verdict each way, runs side by side with the two cost deltas computed by hand, the refusals, a later run compared, and `check_day20_baseline.py`, `eval_baseline.py`, and `run_evals.py --smoke` end to end |
| `tests/test_eval_resume.py` | 57 | Day 20, after the first live run: whose failure it was, what stops a run and what does not, continuing a stopped run, the progress file and what is read back from it, an excerpt joined from verbatim lines, both scripts stopped by an account that runs dry and then resumed with every token counted once, and a batch outliving its process (failed polls tolerated, the id written before the wait, read back by a later run instead of resubmitted) |
| `tests/test_eval_scripts.py` | 23 | Day 19 dev tooling (and, since Day 20, two runs in one second keeping their own records): `run_evals.py` end to end with a scripted model, corpus, and batch endpoint, a recorded run re-scored from disk, a revoked key aborting with no item blamed, and the cost review pricing cache and batch usage |

The house rule from `.claude/rules/tests.md`: every validated invariant gets a negative test asserting the violation is rejected.
A test that only proves the happy path does not prove the invariant is enforced.

Several tests are deliberately adversarial rather than confirmatory, and those are the ones worth reading if you change the contract or the agent:

- `test_diagnose_rejects_a_payload_that_violates_the_contract` feeds a null analytic value with its `Gap` removed. If it passes, `diagnose` is not actually validating.
- `test_schema_guidance_fails_loudly_when_a_contract_field_is_renamed` proves the agent's schema-annotation layer breaks at import on drift instead of silently dropping a rule.
- `test_diagnose_names_truncation_instead_of_blaming_the_model` covers a real measured failure: a report cut off at the token ceiling used to surface as a bogus "field required" error.

## The live checks (these cost money)

All of these need `ANTHROPIC_API_KEY` in `.env` or the shell.
None runs under `pytest`, on purpose - the suite must stay free and offline.
Each records itself under `test-results/`, so a result is diagnosable after the fact rather than only in scrollback.

| Script | Calls | What it proves |
|---|---|---|
| `check_structured_output.py` | 1 per model per repeat (default 3) | `diagnose()` output validates against the frozen contract, per model |
| `check_day5_checkpoint.py` | 1 | Chaos injected -> agent JSON naming the right failure mode, scored against ground truth |
| `check_agent_selection.py` | 2 by default, 5 with `--all` | The coordinator routes each sample query to the right agents |
| `check_day7_delegation.py` | 2 (one plan, one diagnose) | Plan -> execute end to end: the agent's prompt is exactly `context_passed` + query, and nothing leaks by inheritance |
| `ingest_embeddings.py` | 0 Claude; Voyage per new/stale row (`--dry-run` free) | The corpus vectors exist and re-ingestion is idempotent |
| `check_day8_docs.py` | 1 (+1 Voyage query embed with a key) | The Docs agent answers from the seeded corpus with verbatim citations |
| `check_day9_trace.py` | ~3, or **0** with `--fake-agents` | One Langfuse trace shows two agents running concurrently (needs the Langfuse keys; `--fake-agents` proves executor concurrency with scripted agents for free) |
| `demo_day10.py` | ~4 per run (plan, the agents, the model-written synthesis; more if a refinement round runs) | The whole system, live: inject chaos, build the situation from Prometheus, `respond()` end to end with tracing; records a timed transcript that `render_demo_gif.py` (free) turns into the demo GIF |
| `check_day11_github.py` | ~3-5 | The GitHub agent reads a real PR over the MCP wire; every PR, commit, and excerpt traces back to a tool reply |
| `check_day12_deployment.py` | ~3-5 | The Deployment agent diffs two real refs of this repository and reads the live rollout over the wire; `--deploy` recreates the demo containers at the release under test first (needs the stack) |
| `check_day13_sequential.py` | ~8-15 | The sequential path end to end through `respond()`: the coordinator plans Deployment after GitHub on its own, the executor hands GitHub's digest to Deployment, and the recorded `context_passed` shows it; since Day 14 the refinement loop re-delegates the gaps the agents leave (`--max-rounds`, default 2; `0` reproduces the Day 13 form) and the model writes the synthesis (`--deterministic-synthesis` skips that call); `--deploy` recreates the demo at the PR's head commit first (needs the stack and the GitHub token) |
| `check_day15_integration.py` | ~12-20 (**~$1.10 measured**, 343-348k input tokens) | The whole system on one query: all four agents, the parallel *and* the sequential path in one request. Deploys the demo at PR #11's merge commit (`--deploy`), injects a real fault, builds the situation from live Prometheus metrics, and asserts all four agents are planned and answer, that a parallel group of two or more overlapped in measured wall-clock time, and everything `check_day13_sequential.py` asserts about the sequential half; `--max-rounds` caps the refinement loop, `--deterministic-synthesis` skips the synthesis call; resets chaos itself and records a transcript `render_demo_gif.py` can replay |
| `cost_review.py` | **0** | Free: prices every recorded live run by check and by day against the spend alert (`--alert`, `--since`, `--json`); a floor, and it says what it cannot see |
| `gate_recorded_run.py` | **0** | Free: puts every recorded `respond()` response through the Day 16 HITL approval gate (fail-closed `DenyAll` by default; `--approver console --identity <who>` asks at the terminal, `--run` picks one run, `--json`); the gate exercised against what the agents actually recommended live. `--persist` (Day 17) writes the decisions to `hitl_audit_log` on the stack's Postgres instead of the in-memory log |
| `audit_log.py` | **0** | Free: reads the append-only audit log back (`--request`, `--decision`, `--since`, `--limit`, `--json`); needs the stack |
| `confidence_report.py` | **0** | Free: every recorded `respond()` response read field by field - each judgement with its band and flags, and each Docs report's claim -> source chain and coverage gaps (`--run`, `--json`); the calibration floor the Day 19 eval starts from |
| `run_evals.py` | 38 on the full set, one per item, plus one per validation retry (+1 Voyage query embed per recall with a key); **0** with `--list`, `--show`, `--dry-run`, `--rescore`, `--recorded-tools` | The Day 19 eval: the shipped Incident and Docs agents scored against the seeded incidents' recorded truth - accuracy, hallucination rate, tool success, calibration per band - and the run priced three ways from its measured tokens. `--mode batch` sends the same requests through the Batch API (half price, usually minutes, up to an hour); `--no-cache` is the uncached baseline; `--tasks diagnose` needs no stack. Projected ~$1.13 for a full realtime run before caching (`--dry-run` prints it). A run stops itself with exit 2, and says how to continue, when the key or the account is refused or three items in a row fail on the API. `--resume <run-dir>` continues a stopped run, asking only for what it did not finish |
| `run_evals.py --smoke` | 4 | Day 20: the first four diagnoses and a verdict. Fails when an item got no response, or when prompt caching is on and the later requests did not read the prefix the first one wrote. About $0.12. Run it after anything changes the wire: a new key, a prompt edit, an SDK upgrade |
| `check_day20_baseline.py` | 118: the smoke test, then 38 for each of three configurations (**~$3.79 projected before caching**, re-calibrated from measured requests); fewer with `--resume`; **0** with `--plan` | The Day 20 checkpoint: the whole eval set realtime and uncached (the reference), realtime and cached, and batch and cached, and `evaluations/baseline.md` with `baseline.json` written from what was measured. Stops after the smoke test if it fails. `--limit 6` is a 22-call rehearsal that writes nothing under `evaluations/`; `--configs` chooses the runs; `--resume` continues from the recorded runs of each configuration, asking only for what is not done; the batch run waits on the Batch API - **45 min to 2 h 40 min per batch on the day the baseline was made** - and a batch it submitted is read back by `--resume` if the process dies. About 28 seconds an item realtime, so start it detached |
| `eval_baseline.py` | **0** | Free: recorded eval runs side by side (`--latest` picks the newest complete run of each configuration), `--write PATH.md` with `PATH.json` beside it, and `--against baseline.json <run-dir>` to read a later run against a committed baseline |
| `check_tool_routing.py` | 20 per query set, 40 for both (`--dry-run` free) | The Domain 2 routing case study: a forced single-tool choice between the two overlapping tools over 20 plain and 20 adversarially worded queries; `--variant v1` lists the `1.0.0` `analyze_*` server (the baseline), `--variant v1_1` the `1.1.0` `search_*` server (the split), the same ground truth mapped onto each server's names; prints and records the misrouting rate per set with the query-set hash, so before and after compare like with like; `--model` swaps the router |

```bash
# One call per model. Validates diagnose() against the frozen contract.
uv run python scripts/check_structured_output.py

# Cheapest useful form: one model, one call.
uv run python scripts/check_structured_output.py --models claude-sonnet-5

# Stability check. --repeat 3 across 2 models is SIX billed calls.
uv run python scripts/check_structured_output.py --models claude-haiku-4-5-20251001 --repeat 3

# Coordinator routing: 2 discriminating cases, or all 5.
uv run python scripts/check_agent_selection.py
uv run python scripts/check_agent_selection.py --all

# Delegation end to end. 2 calls. Needs no Docker stack.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day7_delegation.py

# Docs agent against the seeded corpus. 1 call. Needs the stack.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day8_docs.py

# One traced request with parallel agents. ~3 calls live; zero with --fake-agents.
# Needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY either way.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day9_trace.py --fake-agents

# Prints a full validated response. One call.
uv run python examples/incident_structured_demo.py

# The whole system: inject chaos, read Prometheus, respond() end to end, traced. ~4 calls.
# Needs the stack. --skip-inject reuses active chaos; --query overrides the canonical query.
PYTHONIOENCODING=utf-8 uv run python scripts/demo_day10.py
uv run scripts/render_demo_gif.py --run test-results/runs/<date>/<run-dir>   # free

# The GitHub agent reads a real PR over the MCP wire. ~3-5 calls. Needs GITHUB_TOKEN + GITHUB_REPO.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day11_github.py --pr 12

# The Deployment agent diffs two real refs and reads the live rollout. ~3-5 calls.
# --deploy recreates the demo containers at --to first; needs the stack and the token.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day12_deployment.py --deploy

# The sequential path through respond(): GitHub, then Deployment with GitHub's digest in its
# context, then the refinement loop and the synthesis. ~8-15 calls. Needs the stack and the
# token; --deploy recreates the demo at the PR head; --max-rounds 0 is the Day 13 form.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day13_sequential.py --deploy

# All four agents on one query: Incident + Docs + GitHub in parallel, Deployment after GitHub,
# the loop, the synthesis. ~12-20 calls, $0.74 measured with caching on (Day 21). Needs the stack and the token.
PYTHONIOENCODING=utf-8 uv run python scripts/check_day15_integration.py --deploy --max-rounds 1

# What every recorded live run has cost, by check and by day. Free.
uv run python scripts/cost_review.py

# How big each commit-bearing tool reply is on the four-agent run's own calls. Free: GitHub reads only.
uv run python scripts/measure_tool_replies.py

# Every recorded live recommendation through the HITL approval gate. Free. With --persist the
# decisions are written to the append-only audit log on the stack's Postgres; audit_log.py reads it.
uv run python scripts/gate_recorded_run.py
uv run python scripts/gate_recorded_run.py --persist
uv run python scripts/audit_log.py --limit 20

# Every recorded judgement by band, the flags, and each Docs report's claim -> source chain. Free.
uv run python scripts/confidence_report.py

# The eval set. Free: list it, see what one case shows each agent, size every request,
# re-score a recorded run, read tool success off the recorded live runs.
uv run python scripts/run_evals.py --list
uv run python scripts/run_evals.py --show case_04
uv run python scripts/run_evals.py --dry-run
uv run python scripts/run_evals.py --rescore test-results/runs/<date>/<run-dir>
uv run python scripts/run_evals.py --recorded-tools

# The eval set, live. One call an item: 38 on the full set. Recalls need the stack.
# Start small; --write puts the report under evaluations/ where it can be committed.
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py --tasks diagnose --limit 4
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py --no-cache
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py --mode batch --cache-ttl 1h

# Is the wire working, and is the cache reading? 4 calls, a verdict.
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py --smoke

# The Day 20 baseline. --plan is free and prints the calls and the projected price.
# Live it is 118 calls: a smoke test, then the whole set three ways. Needs the stack.
uv run python scripts/check_day20_baseline.py --plan
PYTHONIOENCODING=utf-8 uv run python scripts/check_day20_baseline.py --limit 6   # a rehearsal
PYTHONIOENCODING=utf-8 uv run python scripts/check_day20_baseline.py

# A run that was stopped - no credit, a revoked key, a closed terminal - is continued, not
# repeated. Both forms ask only for what is not done, and --plan --resume says what that is.
uv run python scripts/check_day20_baseline.py --plan --resume
PYTHONIOENCODING=utf-8 uv run python scripts/check_day20_baseline.py --resume --skip-smoke
PYTHONIOENCODING=utf-8 uv run python scripts/run_evals.py --resume test-results/runs/<date>/<run-dir>

# Recorded eval runs side by side, and a later run against the committed baseline. Free.
uv run python scripts/eval_baseline.py --latest
uv run python scripts/eval_baseline.py --against evaluations/baseline.json test-results/runs/<date>/<run-dir>

# The routing case study. 20 calls per set, 40 for both; --dry-run lists the queries for free.
# --variant v1 is the 1.0.0 baseline, --variant v1_1 the 1.1.0 split.
uv run python scripts/check_tool_routing.py --dry-run
uv run python scripts/check_tool_routing.py --set hard
uv run python scripts/check_tool_routing.py --variant v1_1
```

After a `--deploy` run the demo services report the release under test; put them back with
`docker compose up -d --wait`. After a demo, `make chaos-reset` (or
`uv run python demo-app/chaos/inject.py --reset`) - injected chaos persists.

Default with no arguments for the model matrix is three models, one call each.
`--repeat N` multiplies by N per model, so `--models a b c --repeat 3` is nine calls.

A single pass proves nothing about stability.
The Haiku result went from 1/1 valid to 1/3 valid once `--repeat 3` ran, which is why the default model is Sonnet.
Spend the repeats when choosing a model; skip them when you only want to know the code still works.

**Run `check_day7_delegation.py` after changing the coordinator's prompt or the select schema.**
It is the only check that exercises a model-written plan against a real agent, and it is what caught `round` being asked of the model when the coordinator already knew it (war story #7) - a failure the whole offline suite was structurally blind to, because every fixture had the field filled in by hand.

Stack checks need Docker rather than a key, and are free:

```bash
docker compose up -d --wait                              # bring the stack up
uv run python demo-app/chaos/inject.py --status          # current knob state
uv run python demo-app/chaos/inject.py --mode downstream_latency
uv run python demo-app/chaos/inject.py --reset
```

## Reading the results

Every `pytest` run records itself as structured JSON under `test-results/`.
`scripts/runlog.py` writes it; hooks in `tests/conftest.py` trigger it.
The records are gitignored - they are machine-local evidence, not source.

```
test-results/
  index.jsonl                                    one line per run, appended
  runs/2026-07-29/184127Z__pytest__not-integration/
    run.json                                     the summary
    events.jsonl                                one line per test
  runs/2026-07-29/183754Z__llm__structured-output/
    run.json
    events.jsonl
    claude-sonnet-5.response.json                raw artifacts
```

Set `AIOC_RUNLOG=0` to turn recording off for a run.

### Start at the index

`index.jsonl` is the query surface: one line per run, newest last.

```bash
# Every failing run
grep '"outcome": "failed"' test-results/index.jsonl

# The last five runs, readably
tail -5 test-results/index.jsonl | python -c "
import json,sys
for line in sys.stdin:
    r=json.loads(line)
    print(f\"{r['started_at']}  {r['outcome']:<7} {r['kind']:<7} {r['name']:<20} {r['totals']}\")"
```

Each entry carries `run_id`, `kind`, `name`, `outcome`, `started_at`, `duration_ms`, `totals`, `path`, `commit`, and `model`.

### Then the run summary

`run.json` answers "what was true when this ran":

- `outcome` - `passed` / `failed` / `error`. `error` means the run never reached a verdict, which is different from a failing assertion.
- `git` - `{branch, commit, dirty}`. A result without a commit is an anecdote, and `dirty: true` means it is not reproducible.
- `env` - Python version, platform, `aioc_model`, `aioc_llm_effort`. Records only the *presence* of `ANTHROPIC_API_KEY`, never the value.
- `totals` - event counts by outcome.
- `command` - the exact command line.

One field reads wrong at a glance: `anthropic_api_key_in_shell_env` is `false` when the key lives in `.env`, because pydantic-settings loads it without touching `os.environ`.
False there does not mean "no key".

### Then the events

`events.jsonl` is one JSON object per test or step, same envelope regardless of `kind`.

```bash
# Which tests failed in the most recent pytest run, and why
PYTHONIOENCODING=utf-8 uv run python -c "
import json,glob
p=sorted(glob.glob('test-results/runs/*/*pytest*/events.jsonl'))[-1]
for line in open(p,encoding='utf-8'):
    e=json.loads(line)
    if e['outcome']!='passed':
        print(f\"{e['outcome'].upper():<7} {e['name']}\")
        print(f\"        {e['message']}\")"

# The slowest ten tests
PYTHONIOENCODING=utf-8 uv run python -c "
import json,glob
p=sorted(glob.glob('test-results/runs/*/*pytest*/events.jsonl'))[-1]
ev=[json.loads(l) for l in open(p,encoding='utf-8')]
for e in sorted(ev,key=lambda e:-(e['duration_ms'] or 0))[:10]:
    print(f\"{e['duration_ms']:>8.1f}ms  {e['name']}\")"
```

`PYTHONIOENCODING=utf-8` is not optional on Windows.
The console defaults to cp1252 and a model-written summary containing a Unicode arrow will crash the print with `UnicodeEncodeError`.

Fields per event: `ts`, `seq`, `type` (`test` / `step` / `command` / `llm_call` / `exception`), `name`, `outcome`, `duration_ms`, `message` (one line), `detail` (the full traceback), and `data` (type-specific).
For a pytest event, `data` holds `{phase, file, line, markers}`.
For an `llm_call` event, `data` holds the model, status, confidence, counts, and - on failure - the individual contract violations.

Passing `setup` and `teardown` phases are not recorded, only `call`.
A *failing* setup is recorded as `error`, so a broken fixture cannot masquerade as a run in which nothing happened.

### The failure log is the point

The reason to read `events.jsonl` rather than scrollback is that a failed `llm_call` records each contract violation as structured data:

```bash
PYTHONIOENCODING=utf-8 uv run python -c "
import json,glob
p=sorted(glob.glob('test-results/runs/*/*llm*/events.jsonl'))[-1]
for line in open(p,encoding='utf-8'):
    e=json.loads(line)
    print(f\"{e['name']:<30} {e['outcome']}\")
    for err in e['data'].get('errors',[]):
        print(f\"      {err['field']}: {err['message']}\")"
```

That output is what identified the `*_detail` pairing failure as a schema-affordance problem rather than a model-capability one, across three models, without re-running anything.

## Promoting a run

`test-results/` is gitignored by design: per-machine, per-clock, unbounded growth.
When a run needs to be shared - an eval result, a case-study measurement, evidence for the Day 26 domain table - copy it into `evaluations/`, which the build plan reserves for committed results.
A scoring rule that changes does not need a new run: `scripts/run_evals.py --rescore <run-dir> --save` scores the kept responses again and leaves `eval.rescored.json` beside the original.
For an eval run that is one flag: `scripts/run_evals.py --write evaluations/results/<name>.md`, or the same with `--rescore <run-dir>` for a run already made.
Do not un-gitignore `test-results/`.
