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
uv run pytest -q              # 253 tests, no network, no API key; 10 skip without the Docker stack
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
| `tests/test_tracing.py` | 8 | Day 9 tracing seam: null-object degradation, the Langfuse adapter against a stub client, the `.env` path regression |

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
```

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
Do not un-gitignore `test-results/`.
