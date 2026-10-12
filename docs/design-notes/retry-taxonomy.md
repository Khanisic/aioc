# What the retry loop's error taxonomy revealed (Day 27)

Since Day 17 every forced emit runs inside one validation-retry loop (`src/aioc/agents/_retry.py`, decision #30).
A refused report is answered in the same conversation with the rendered error and the emit forced again, and every refusal is classed as one of three kinds:

- **`format`** - the contract (pydantic) refused the shape or a cross-field rule. The information is in the report, mis-shaped; re-emit it.
- **`grounding`** - the agent's own rule refused a claim about data the model was not given: a paraphrased quote, a SHA no tool returned. The honest fix is to remove the claim and record a gap, not to paraphrase closer.
- **no kind** (`kind=None`) - truncation, a model that never called the tool, a fault in the tool data. A retry cannot change these, so they are raised on the first attempt.

Every emit is recorded, accepted-first-try included, so the rates below have a real denominator.
This note reads every recorded run back and says what the classes showed.

## The numbers

The four Sonnet 5 eval runs (Day 20's three configurations and the Day 24 re-run), 38 emits each, from each run's `eval.json`:

| Agent | Emits | Accepted first try | Recovered by a retry | Exhausted | Refused attempts: `format` / `grounding` |
|---|---:|---:|---:|---:|---|
| Incident (diagnose) | 72 | 58 | 14 | 0 | 14 / 0 |
| Docs (recall) | 80 | 62 | 17 | 1 | 22 / 0 |
| **Both** | **152** | **120 (79%)** | **31** | **1** | **36 / 0** |

31 of the 32 refused reports were recovered, at 35 extra calls on 152 emits (23% more calls).
The Haiku 4.5 run (Day 23, the same set) for contrast: 38 emits, 27 accepted first try (71%), 9 recovered, 2 exhausted, 15 `format` and 1 `grounding` refusals.

On the `respond()` path, the Day 21 four-agent run (the only live run since Day 17 whose `RetryLog` left the process; the Day 25 run's stayed inside the service container): 4 emits, 3 accepted, 1 Docs report recovered from one `format` refusal ("evidence is required when value is set and confidence >= 0.5").

## What it revealed

**1. The kind of refusal follows the shape of the agent.**
The eval agents make one forced call over a context they were handed, and every Sonnet refusal there was `format`: zero `grounding` in 36.
The live refusals before the loop existed, on the tool-driven agents, were mostly the other kind: a GitHub excerpt paraphrased (three times across Days 14-15), a Deployment report naming a `from_version` no tool had returned (twice), a merge commit reported as a commit the agent never fetched (Day 14).
An agent that reads its evidence from a tool reply mid-conversation misquotes it; an agent handed its evidence up front mostly mis-shapes the report.
That is why the feedback differs by kind, and why one generic "try again" message would have been wrong for half the system.

**2. An informed retry is cheap and nearly always enough.**
97% of refused reports were recovered, almost all in one retry, for 23% more calls.
The model-selection arithmetic before Day 17 (`numbers.md`) found that a cheap model plus blind retries was not cheaper than Sonnet, at Haiku's measured 1-in-3 contract validity; the difference now is the error attached.
The model is not guessing what was wrong, it is told the field and the rule, and the conversation it already paid for is kept.

**3. What does not recover is abstention.**
Every exhausted report in every run is the same act: a Docs report on a question with no precedent, answering `null` without the `Gap` the contract requires (sec 2.1: a null judgement needs a gap whose `blocks_field` names it; sec 4.2: every unanswered sub-question needs its own).
Sonnet failed it once in 80 Docs emits; Haiku failed both no-precedent probes, three attempts each.
The contract makes "I don't know" a structured act with its own evidence trail, and that is the rule models find hardest to satisfy - harder than any shape rule, and not fixed by being told.
It is the reason the subagent model stayed Sonnet 5 after Day 23 (`evaluations/routing/haiku-vs-sonnet.md`): the cheaper model answers where there is an answer and cannot say there is none.

**4. The no-kind class found the harness's bugs, not the model's.**
Because truncation and tool-data faults are never retried, they surface on the first attempt with their own error, and two of them were harness defects.
Day 21: a tool-loop round cut off at `max_tokens` mid-`tool_use` left the call unanswered and the API refused the next request; `run_tool_loop` now answers a cut-off call with an error `tool_result` (war story #14).
Day 24: a report emitted twice in one turn broke the retry's pairing; forced emits now send `disable_parallel_tool_use` and a retry answers every `tool_use`.
Had these been retried like a refusal, each would have cost a full re-send and failed identically.

**5. A retry is a whole conversation, and it shows in the bill.**
The Day 24 re-run cost $0.87 against Day 20's $0.71 on the same requests; the difference is mostly two more retry calls (10 against 8), each re-sending its item's conversation.
Retries are where an eval's run-to-run cost noise comes from, and the cap of 2 plus the identical-rejection stop are what bound it.

**6. The record kept the kind and lost the reason.**
Eval items recorded each refusal's kind, not its message, so "which rule refuses Sonnet's diagnoses most?" had no answer for the 33 refusals in recovered reports; the exhausted ones survived only because a failed item keeps its error.
From Day 27 every eval item keeps each refusal's rendered message (`RetryStats.messages`), and each report and `eval.json` groups the run's refusals by the rule that fired (`refusal_rules`, "What refused them, one row a rule").
The next live eval run answers the question; until then it is open, and this note does not guess.

## What changed because of it

- The feedback text is per kind (`render_feedback`): `grounding` says to drop the claim and record a gap, because the measured failure was closer paraphrase.
- The Docs emit guidance puts an unsupported claim's confidence below 0.25 (Day 18) and states that every unanswered sub-question needs its own gap in the emit schema's descriptions as well as the prompt (Day 16). Abstention still exhausts, so the rule is stated where the model reads it and enforced where it cannot be skipped; it is not loosened.
- The subagent model stays Sonnet 5 (Day 23).
- Eval records keep the reason with the kind (Day 27).
