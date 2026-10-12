# Workflow or agent: where the model decides and where code does (Day 27)

`BUILD_PLAN.md` Phase 1 asks for one short note on a path where a deterministic workflow was chosen over an agent.
This is that note.
The path is the refinement loop; the rule it follows is the same one that drew every other boundary in the system, listed at the end.

## The rule

A step gets a model when its input is open-ended and the step is a judgement: which agents does this question need, which tool answers this, what does this metric mean.
A step gets code when its input is already structured and the step is a function of it: what to hand the next agent, when to stop, what the run cost.
Asking a model for a value the code can derive adds a way to be wrong and no way to be right (decision #21), and a stopping rule that is a judgement call is a cost the system cannot predict (decision #22).

## The path: the refinement loop

**What the plan said.** "Coordinator checks synthesis for gaps and re-delegates with targeted queries until coverage is sufficient."
Read literally that is an agent: a model reads the answer, decides whether coverage is sufficient, and writes the follow-up queries.

**What was built.** A workflow in `src/aioc/coordinator/executor.py` (`_refine`).
It reads `Gap` records, not prose.
Every open gap that is `resolvable` and names a `suggested_agent` becomes a `round: 1+` invocation whose query is the gap's `suggested_query` verbatim; gaps for one agent share one invocation; the context is composed the Day 13 way and recorded verbatim.
It stops on four rules and none of them is a judgement: nothing left to ask, the round cap (default 2), an agent that is not registered, or the same agent asked the same query twice.

**Why a workflow.**

- **The judgement had already been made, by the right party.** The contract gives every gap `resolvable`, `suggested_agent`, and `suggested_query`, and says they "exist for a machine, not a reader".
  The agent that hit the wall is the one that knows what would get past it.
  A second model reading its prose to re-derive the same query would be a lossy copy of a field that is already there.
- **Every round is a full agent run, so the stop has to be predictable.** The Day 13 two-agent run was 264k input tokens before any refinement.
  A loop that runs "until coverage is sufficient" has no ceiling a reader can compute; a loop with a cap and an identical-gap rule does.
  The API service caps rounds at zero by default for the same reason (`docs/design-notes/deployment.md`).
- **It can be tested without a model.** `tests/test_refinement.py` proves each stopping rule, the composed context, and the gap bookkeeping with scripted runners; an agentic loop's stopping behaviour could only be sampled live.
- **It is legible after the fact.** Every re-delegation is an `AgentInvocation` with its round, its query, and its composed context in the response, so "why did it ask that?" is answered by a field, not by reconstructing a model's reasoning.

**What it costs.** The loop is only as good as the gaps.
It cannot tell a gap that matters from one that does not, and the live runs showed exactly that failure.
On Day 15 every agent received the whole four-part query, raised gaps for its siblings' parts, and the loop dutifully re-asked all four: round 1 was about half the bill and closed almost nothing.
The fix did not add judgement to the loop; it fixed the gaps at their source.
Since Day 16 each agent is told who its siblings are and that their parts are not its gaps (`handoff.roster_block`), and the Day 21 re-run made one re-delegation instead of four, with 6 unresolved gaps instead of 14, at $0.74 against about $1.16.
That is the pattern this project keeps choosing: when a deterministic step produces a bad result, make its input better before making the step smarter.

**What would move the boundary.** If honest, well-scoped gaps still leave answers the eval scores as incomplete, a model check of "is this answered?" belongs *before* the loop, emitting gaps the loop then consumes - the loop itself stays code.

## The same call, made elsewhere

| Step | Who decides | Why |
|---|---|---|
| Which agents a query needs (`planner.py`) | model | open-ended natural language; the validators then enforce the shape (every agent accounted for, `depends_on` resolvable, `context_passed` non-empty and not the query restated) |
| Which tool to call and with what (GitHub, Deployment tool loops) | model | the investigation path depends on what each reply says |
| Every `Assessment` in a report | model | judgements, scored against the bands |
| When an invocation runs, and what it is handed (`executor.py`, `handoff.py`) | code | a function of the plan and the dependencies' responses; the digest is built, never summarised by a model |
| Facts in a report: SHAs, file counts, config keys, health signals, `round`, `requires_approval` (Deployment) | code, stamped from tool replies | the runtime already knows them (decisions #17, #19) |
| `complete` over a null judgement | code settles it to `partial` (`agents/_status.py`) | derivable; the first live sequential run was refused for leaving it to the model |
| A rollout's status (`check_rollout_health`) | code: a fixed rule over measured signals | the same metrics must give the same status every time, and an unmeasured signal is `null`, not a guess |
| Whether an action needs approval (`hitl/policy.py`) | code, ORed with the agent's own flag | fail-closed policy cannot depend on a model remembering to ask |
| Whether to retry a refused report, and when to stop (`agents/_retry.py`) | code | the cap and the identical-rejection rule, the same shape as the loop's |
| The answer (`synthesis.py`) | code by default, model opt-in | below |

**The synthesis is the one boundary deliberately left switchable.**
The deterministic form adopts the highest-confidence report and cites its evidence, so it never invents an id and never costs a call.
Its limit is real and was seen live: on Day 25 it adopted GitHub's report (0.85) and so did not name payments-api, which the Incident agent had.
`ModelSynthesiser` combines the reports at the price of one more call, grounded in code (an evidence id no agent carries, or a confident answer that cites nothing, is refused and falls back to the deterministic form with the reason in `answer.reasoning`).
It is off by default because it is one more paid call on every request, and opt-in at the entry point exactly as tracing is; the Day 13 and Day 15 checks turn it on (`--deterministic-synthesis` turns it off), and the Day 25 service run left it off.
