# test-results

Structured records of every test and check run in this repo - what ran, what it produced, and
against which commit.
Written by `scripts/runlog.py`; read by whoever is asking "when did this last pass?".

The records are **machine-local evidence, not source**: `runs/` and `index.jsonl` are gitignored.
Only this README and `.gitignore` are committed.
If a particular run ever needs to be shared - an eval result, a case-study measurement - promote
it into `evaluations/`, which the build plan already reserves for committed results.

## Layout

```
test-results/
  index.jsonl                                  one line per run, appended - the query surface
  runs/
    2026-07-29/
      151230Z__pytest__not-integration/
        run.json                               the summary
        events.jsonl                           one line per test or step
      151402Z__llm__structured-output/
        run.json
        events.jsonl
        response.json                          optional raw artifacts
```

Run directories are `<UTC time>__<kind>__<name>`, so they sort chronologically and say what they
are without being opened.
`kind` is the run family - `pytest`, `lint`, `chaos`, `llm`, `command`.

## Why this shape

`events.jsonl` is line-delimited because events are appended one at a time and must survive the
process dying mid-run: a truncated JSONL still parses line by line, whereas a truncated JSON array
is unrecoverable - and the run that crashed is precisely the one worth reading.
`run.json` is a single object because it is written once, at the end, and is meant to be read by a
human.
`index.jsonl` exists so that "find the last failing run" is one `grep`, not a directory walk.

## Record schema

Both files carry `schema_version` (currently `1.0.0`) on the summary.
This is **not** the frozen contract in `docs/CONTRACTS.md` and is versioned separately - it
describes local tooling output, and may change freely.

### `run.json`

| Field | Meaning |
|---|---|
| `run_id` | `<UTC>__<kind>__<name>`, unique per run |
| `kind`, `name` | run family and what it was |
| `outcome` | `passed` / `failed` / `error` - `error` means the run never reached a verdict |
| `exit_code` | the process exit code, when there was one |
| `started_at`, `finished_at` | RFC 3339 with an explicit `Z`, matching the contract's timestamp primitive |
| `duration_ms` | wall clock, measured monotonically |
| `command` | the command line that produced it |
| `totals` | `{events, passed, failed, skipped, error}` |
| `git` | `{branch, commit, dirty}` - a result without a commit is an anecdote, and `dirty` is the line between reproducible and not |
| `env` | `{python, platform, aioc_model, aioc_llm_effort, anthropic_api_key_present}` |
| `metadata` | run-specific extras (marker expression, model matrix, injected failure mode) |

`env` records the *presence* of `ANTHROPIC_API_KEY`, never its value.
Nothing here writes a secret to disk.

### `events.jsonl`

One JSON object per line, uniform envelope regardless of `kind`:

| Field | Meaning |
|---|---|
| `ts`, `seq` | when, and the order within the run |
| `type` | `test`, `step`, `command`, `exception` |
| `name` | pytest nodeid, or the step name |
| `outcome` | `passed` / `failed` / `error` / `skipped` |
| `duration_ms` | for that single test or step |
| `message` | one line, terminal-readable |
| `detail` | the long form - a traceback, a full response body |
| `data` | type-specific: pytest adds `{phase, file, line, markers}`; a step adds whatever it measured |

Passing `setup`/`teardown` phases are not recorded - only the `call` phase.
A *failing* setup is recorded as `error`, so a broken fixture never looks like a run in which
nothing happened.

## Recording a run

Pytest records itself - the hooks live in `tests/conftest.py`:

```bash
uv run pytest -q                    # writes runs/<date>/<time>__pytest__all/
uv run pytest -q -m "not integration"
AIOC_RUNLOG=0 uv run pytest -q      # opt out
```

Any other command can be wrapped:

```bash
uv run python scripts/runlog.py --kind lint  --name ruff  -- uv run ruff check .
uv run python scripts/runlog.py --kind chaos --name smoke -- uv run python demo-app/chaos/inject.py --mode downstream_latency
```

The wrapper passes stdout and stderr straight through and returns the command's own exit code, so
it is safe to put in front of anything, including in CI.
Captured output is kept alongside the run as `stdout.log` / `stderr.log`.

From Python, for anything producing its own results:

```python
from runlog import RunRecorder

with RunRecorder(kind="llm", name="structured-output") as run:
    run.event("diagnose", outcome="passed", duration_ms=1840, data={"model": model})
```

The summary is written on exit even if the body raises, so a crashed run still leaves evidence.

## Querying

```bash
# every failing run, newest last
grep '"outcome": "failed"' test-results/index.jsonl

# which tests failed in a given run
python -c "import json,sys; [print(e['name'], e['message']) for e in map(json.loads, open(sys.argv[1])) if e['outcome']!='passed']" \
  test-results/runs/2026-07-29/151230Z__pytest__all/events.jsonl
```
