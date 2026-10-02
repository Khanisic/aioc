# Tuning the PR review prompt

The Claude Code review (`.github/workflows/claude-review.yml`) reads its instructions from `.github/claude/review.md`.
This note is the record of how that prompt is tuned, and the rule is that it changes only on evidence: a review whose findings were scored.

## How a review is scored

A review is run locally in a Claude Code session, as a fresh subagent with none of the session's context, against a merged pull request's code diff (documentation left out), with `review.md` as its only instructions.
That costs no API credit, which is the project's rule for Days 22-30 (HANDOFF sec 1).
Each finding is then checked against the code by hand and scored:

- **True positive**: the defect is real, whether or not the PR introduced it.
- **False positive**: the code is right, or the "failure scenario" cannot happen.
- **Noise**: true but not a defect by the prompt's own definition (style, preference, praise).

A review that finds nothing is scored too: it is a claim that the diff has no defect, and the record says what the reviewer said it did not read.

## Runs

| Date | Prompt | PR | Diff | Findings | True | False | Noise | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-10-01 | v1 (as written on Day 21) | #25, Day 21 | 1,563 lines | 1 | 1 | 0 | 0 | `CommitRef.touched_paths` written as `[]` for "not asked". Real, older than the PR, and a contract limit: recorded as anticipated item 2 in `contract-changes.md` |
| 2026-10-01 | v1 | #24, Day 20 | 2,926 lines | 0 | - | - | - | Said itself that it read ~1,000 of the 1,051 lines of one test file and skipped another test file's diff |
| 2026-10-01 | v1 | #25 with two defects planted | 1,563 lines | 1 | 1 | 0 | 0 | **Recall 1 of 2.** Caught the planted config-value leak from `diff_release`; missed the planted budget bug (`remaining -= len(clipped)` deleted, so the 8,000-character patch budget is declared and never spent). Did not repeat the `touched_paths` finding this time |
| 2026-10-01 | **v2** | #25 with two defects planted | 1,563 lines | 2 | 2 | 0 | 0 | **Recall 2 of 2.** Both planted defects, each with the failure scenario and the existing tests it would break |
| 2026-10-01 | v2 | #25 | 1,563 lines | 2 | 2 | 0 | 0 | `touched_paths` again, and a real bug nobody had planted: `patch_paths: []` with `include_patch: true` silently returned no patches. Fixed on Day 22 (an empty list is now a `validation` error) |

Every run used Sonnet 5 as a read-only subagent, one pass each.

## What the runs say

**False positives are not the problem.** Five reviews, six findings, every one of them real.
The prompt's demand for a concrete failure scenario per finding, and its statement that silence is a valid review, are doing what they were written to do.

**Recall was, and one paragraph fixed the case that was measured.** v1 caught the planted defect of a kind the prompt lists (a configuration value leaving a keys-only tool) and missed an ordinary one of a kind it does not: a bound the change declares in its docstring and description, and never enforces.
v2 adds one paragraph - check the change against its own words: for every limit, cap, budget, counter, ordering, or rule the change states, find the code that enforces it.
On the same planted diff it caught both, and on the clean diff it found a real bug of exactly that shape (a `patch_paths` that "implies include_patch" and, given empty, did the opposite) without inventing anything.

**What this does not show.** One run per prompt per diff, on diffs from one author; a reviewer's output varies run to run (v1 found the `touched_paths` issue once and not the second time).
Two planted defects is a smoke test of recall, not a rate.
The next planted set should hold defects the prompt names nowhere, so the measurement is not of the prompt reading its own checklist.

**v2 is what runs.** `.github/claude/review.md` is v2; v1 is this note's first three rows, and the change between them is the one paragraph quoted above.
