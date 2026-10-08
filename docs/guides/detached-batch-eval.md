# The Day 24 eval re-run: checking on a detached batch, and what to do with the result

This guide is for the batch eval submitted on 2026-10-07 and left running.
Nothing is waiting on it: the script submitted the batch and exited.
You check on it whenever you like, and the check is free until the batch has ended.

## 1. What was submitted

| | |
|---|---|
| Batch id | `msgbatch_01CuGsixLyyEL2nUfVLtZFEr` |
| Submitted | 2026-10-07, 16:35 UTC |
| First run directory | `test-results/runs/2026-10-07/20261007T163517Z__llm__evals-batch` |
| Items | all 38 of `evaluations/cases/seeded-incidents.json` (18 diagnoses, 20 recalls) |
| Model | `claude-sonnet-5` (the subagent role) |
| Configuration | batch, prompt caching on, 5-minute TTL - the same as the Day 20 `batch-cached` baseline run |
| Expected cost | about $0.71, what the Day 20 batch-cached run paid; $0.72 projected uncached by `--dry-run` |

The Batch API queue has been slow on this account.
The Day 20 batch-cached run needed three batches - the 38 first attempts, then the reports the contract refused, then the ones refused again - and they took 2 h 40 min, 1 h 42 min, and 45 min.
Expect the same shape here: three passes, possibly over an afternoon.
A batch that has not finished within 24 hours expires, and its unfinished requests are not billed.

## 2. Before you check

The check and the scoring run on your machine, and the recall items read the incident corpus when their answers are scored.
So the Docker stack must be up, exactly as when the batch was submitted:

```powershell
docker compose ps                      # if this fails, start Docker Desktop first
docker compose up -d --wait
```

If Docker Desktop is not running, start it from the Start menu (or `Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"`), wait about 30 seconds, then run `docker compose up -d --wait`.

If you forget, the check stops at the first recall with `ABORTED: ... the corpus database could not be reached` (exit code 2), waiting at most 10 seconds, and nothing is lost: start the stack and run the same command again.
Before 2026-10-08 it did not stop: each recall waited minutes for the database and was then recorded as the Docs agent's failure, which a resumed run would have kept.

Run every command below from the repository root (`D:\AI_Projects\AIOC\aioc`).
In PowerShell, set this once per terminal so model output prints without crashing the console:

```powershell
$env:PYTHONIOENCODING = 'utf-8'
```

## 3. Check on it: one command, as often as you like

```powershell
uv run python scripts/run_evals.py --mode batch --detach --resume test-results/runs/2026-10-07/20261007T163517Z__llm__evals-batch
```

It is always this same command.
Each check makes a new run directory, and `--resume` given any run in the chain continues the newest one (it prints `... was continued by ...; continuing that one`), so you never need to copy a new path.

What it prints, and what to do next:

| It ends with | Exit code | Meaning | Next |
|---|---|---|---|
| `STILL RUNNING: batch msgbatch_... in_progress (N processing, ...)` | 3 | The batch has not ended. Nothing was spent. | Check again later. |
| `SUBMITTED: batch msgbatch_<new id>. Not waiting for it.` | 3 | The first batch ended and its answers were scored. Some reports were refused by the contract or the grounding check, and their retries went out as a second, much smaller batch. | Check again later with the same command. The retry batch is usually faster, and it costs a few cents. |
| A report (`# Eval run: ...`, then `records: test-results\runs\...`) | 0 | Done. Every item is scored. | Go to section 4. |
| `ABORTED: ...` | 2 | Something outside the run failed (a refused key, an empty account, the API unreachable, the corpus database not running). What had been scored is kept. | Read the message, fix the cause, and run the same command again. |

To see the exit code in PowerShell, run `$LASTEXITCODE` right after the command.

Up to three passes are possible: the first attempts, then up to two rounds of retries (`AIOC_MAX_VALIDATION_RETRIES`, default 2).
Day 20's batch run used all three (8 retry calls in total), so expect to see `SUBMITTED` once or twice before the report.

## 4. When it is done

The finished run's directory is the `records:` line at the end of the report.
Call it `<run>` below; it looks like `test-results\runs\2026-10-07\20261007T1...__llm__evals-batch`.

**4a. Compare it with the Day 20 baseline (free, no API calls):**

```powershell
uv run python scripts/eval_baseline.py --against evaluations/baseline.json <run> --heading "Day 24 re-run against the Day 20 baseline" --write evaluations/day24-vs-baseline.md
```

It picks the baseline run of the same configuration (batch, cached) by itself, prints the comparison, and writes `evaluations/day24-vs-baseline.md` and `.json`.
It refuses if the run is on another set, model, or configuration - that would be a number about the difference, not about the system.

**4b. Keep the run's own report beside the Day 20 ones (free):**

```powershell
Copy-Item <run>\report.md evaluations\results\day24-batch-cached.md
```

**4c. See what it cost (free):**

```powershell
uv run python scripts/cost_review.py --since 2026-10-07
```

The detached checks show as runs with no tokens of their own; their batch's tokens are counted once, by the run that read it back.

**4d. Hand it back.** Tell the next session "the Day 24 batch finished" and point it at `<run>`.
It will read the comparison, record the numbers in `docs/interview-prep/numbers.md` and `HANDOFF.md`, and commit the two `evaluations/` files.
You can also commit them yourself: they are generated reports, not hand-written ones.

## 5. How to read the comparison

What changed in these two agents since the Day 20 baseline decides what the numbers can mean:

- **The 18 diagnoses send byte-identical requests to Day 20.** The Incident agent's prompt, schema guidance, and retry loop have not changed, and the eval context is built from seed lines, not from the live-metrics block Day 22 reordered.
  Any difference on a diagnosis is the model's run-to-run variation.
  That is useful: it is a second measurement of the noise floor (Day 20's three runs agreed on 35 of 38 items).
- **The 20 recalls changed in one way: Day 22 moved the documents ahead of the context** in the Docs agent's prompt.
  A difference on a recall item is the first measurement of that reordering, within the same noise.
- **The Day 21 trimming is not in this set.** It trims GitHub and Deployment tool replies, and the eval set runs only the two forced-emit agents.
  Its measured effect is -34% on the replies themselves (`scripts/measure_tool_replies.py`); its effect on a bill needs a four-agent run.
  So expect the token change here to be close to zero; a large token change would be worth a look before it is believed.

The rows to read first:

| Row | Day 20 batch-cached | What a change means |
|---|---|---|
| Paid | $0.7065 | Same requests, so mostly retries and output length. |
| Input tokens read from the cache | 269,046 of 383,829 (70%) | Below that, the cache marker or the prefix moved - check `cache_health` in the report. |
| Failure mode correct | 17/18 | Diagnoses are unchanged, so a move here is noise. |
| Recall cites its post-mortem | 18/18 | The reordering's main check. A drop is a real finding. |
| No-precedent probes declined | 2/2 | The reordering's second check; Haiku lost both on Day 23. |
| Ungrounded statements | 1/291 | Should stay within 1-2. |
| Retry calls | 8 | A rise on recalls would point at the reordering. |
| Batches | 3 | Fewer means fewer refused reports. |

The comparison also lists every item scored differently from the baseline run, which is where to start reading.

## 6. If something goes wrong

- **`continuing ...: 0 item(s) kept, 38 to run, 1 submitted batch(es) to read back` and then the same request is submitted again as a new 38-request batch.**
  That would mean the requests rebuilt on your machine no longer match the ones submitted (the code changed between submission and the check, or retrieval returned different documents).
  It costs another $0.71. Stop (Ctrl+C) before the batch is submitted if you see `pass 1: 38 request(s) submitted` on a check, and hand it to a session to look at.
  Do not change `src/`, `evaluations/cases/`, or the corpus while the batch is out.
- **The batch expired** (`expired` in the counts after 24 hours): the expired requests were not billed.
  Run the same command; it submits only what is missing, as a new batch.
- **You lost the path.** Every run is listed in `test-results/index.jsonl`; the chain is the `evals-batch` runs from 2026-10-07 on.
  `--resume` with any of them works.
- **You want to see the batch outside this repository.** The Messages Batches API answers `GET https://api.anthropic.com/v1/messages/batches/msgbatch_01CuGsixLyyEL2nUfVLtZFEr` (with your key in the `x-api-key` header and `anthropic-version: 2023-06-01`) with the same counts the check prints.
