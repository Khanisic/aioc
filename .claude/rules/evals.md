---
paths:
  - "src/aioc/evals/**"
  - "evaluations/**"
  - "scripts/run_evals.py"
---
# Reasoning Layer - evals

The eval harness scores the shipped agents against recorded truth. A score is only worth
something if the question did not contain the answer and the scoring can be argued with, so
the rules below are about those two things.

- One answer key: `docker/postgres/init/03-seed-incidents.sql`. Expected values are read from the
  seed when a case file loads. Never restate a failure mode or a severity in a case file - a second
  copy is a copy that can disagree.
- A case selects, it never authors. What an agent is shown about an incident is verbatim seed
  lines, chosen by summary-sentence index and event id. The recall question is the one authored
  thing, and it carries no answer.
- The leak guard (`cases.check_leaks`) is enforced at load, for the reason `chaos_knob_value` is
  excluded in code: a leaked answer does not fail an eval, it passes it. If you add a field to the
  rendered context, add it to the guard and to its test.
- Scoring is pure: no model call, no database, no clock. A recorded run must score the same
  tomorrow (`run_evals.py --rescore`), which is what lets a scoring rule change without a re-run.
- An abstention is not a wrong answer and is not a right one. The contract asks for a null over a
  guess; count the two apart or the score rewards guessing.
- `null` is not zero here either. A rate over an empty denominator is `n/a`, not 0%.
- An agent that raised is a scored item. A refused credential is not - it aborts the run
  (`EvalAborted`), because every remaining item would repeat it.
- Score the shipped code path. The runner calls `IncidentAgent.diagnose` and `DocsAgent.answer`
  with an explicit context, exactly as the executor does. Do not build an eval-only prompt.
- Every live run costs money. Say the call count and quote `--dry-run` before running one; do not
  run a model matrix unasked.
- Runs are comparable only on the same set, the same model, and the same items. Refuse the rest
  (`baseline.build_baseline`); an average over runs that differ is a number about the difference.
- Two cost deltas, never mixed: a run's own tokens priced both ways is the lever, one run's bill
  against another's is the lever plus what the model happened to write. Say which one a number is.
- A baseline on part of the set is a rehearsal. Nothing on a selection is written under
  `evaluations/`, because a committed baseline is compared with as if it were the whole.
