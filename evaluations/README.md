# evaluations

Committed eval sets and committed eval results.
`test-results/` is where every run lands on the machine that made it, and it is gitignored; this directory is for what has to be shared.

## What is here

| Path | What it is |
|---|---|
| `cases/seeded-incidents.json` | The eval set: 20 cases, 38 items. One case for each of the 18 seeded incidents, each with a diagnose task and a recall task, plus two no-precedent recall probes. |
| `baseline.md`, `baseline.json` | The Day 20 baseline (2026-09-30): the whole set run once for each configuration of the two cost levers, side by side. The Markdown is for reading; the JSON is what a later run is compared against. |
| `results/` | One report for each run in the baseline, written with it. |

## The one rule: a case selects, it never authors

The answer key is `docker/postgres/init/03-seed-incidents.sql` and nothing else.
A case names an incident and says which of its recorded lines the agent is shown: summary sentences by index, timeline events by id.
Every line an agent reads about an incident is therefore verbatim from a seeded row, so the eval data cannot drift from the corpus, and whether an answer leaked into a question is a string comparison.
`aioc.evals.cases.check_leaks` makes that comparison at load and refuses the file when it fails.

What a diagnosis is never shown: the incident's title, its root cause, its resolution, the events that record the finding and the fix, every recorded severity, and the incident id.
The expected failure mode and severity are not in the case file either.
They are read from the seed when the set is loaded, so there is one answer key and no second copy to disagree with it.

The recall question is the one authored thing in a case.
It is a question, it carries no answer, and the document it should lead to is derived from the incident.

## Changing the set

Add a case, or change what a case shows, by editing the JSON.
`uv run python scripts/run_evals.py --show case_04` prints exactly what each agent will be handed, and costs nothing.
`uv run pytest -q tests/test_evals.py` holds the committed set to the rules above.

Two runs are comparable only on the same set.
Every report names the set's `sha256`, and changing the file changes it; bump `version` when the change is meant to start a new baseline.

The record and report formats are not frozen (`CLAUDE.md`: eval record formats churn freely).

## Running it

```bash
uv run python scripts/run_evals.py --list       # free
uv run python scripts/run_evals.py --dry-run    # free: every request sized, a projected price
uv run python scripts/run_evals.py              # live, 38 calls
uv run python scripts/run_evals.py --mode batch # live, the Batch API at half price
```

`docs/guides/running-tests.md` has the rest, including what each mode costs.

## The baseline

```bash
uv run python scripts/check_day20_baseline.py --plan   # free: what it will do and cost
uv run python scripts/check_day20_baseline.py          # live, 118 calls
uv run python scripts/check_day20_baseline.py --resume # live, only what is not yet done
uv run python scripts/eval_baseline.py --against evaluations/baseline.json <run-dir>   # free
```

The checkpoint writes `baseline.md`, `baseline.json`, and `results/` only when it ran the whole set.
A run on a selection is a rehearsal and leaves this directory alone.

A baseline is replaced, not edited.
When the case file changes, its `sha256` changes, every comparison against the old baseline is refused, and the checkpoint is run again.
