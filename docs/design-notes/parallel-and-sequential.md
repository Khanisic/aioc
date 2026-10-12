# Parallel here, sequential there (Day 27)

Incident and Docs run at the same time; Deployment runs after GitHub.
This note says why, what decides it on a given request, and what the scheduler does with the decision, including the Day 27 change that removed a wait no dependency required.

## What decides it: a data dependency, nothing else

An invocation is `sequential` when its context has to contain something another invocation produces.
It is `parallel` when nothing it needs comes from another agent in the request.
The agents' domains do not decide it, and neither does the order a human would investigate in.

- **Incident + Docs: parallel.** The Incident agent reads the live metrics the executor renders into its context; the Docs agent reads the incident corpus it retrieves itself.
  Neither reads the other's output, so running them together costs nothing but threads.
  The Day 10 showcase query ran both in one group: two agents for the wall-clock price of one.
- **GitHub then Deployment: sequential.** Deployment diffs a release, and on the scenarios that need both agents the release is identified by the PR: its merge commit and touched paths are in GitHub's report and nowhere else.
  Until GitHub has returned, Deployment's context does not exist.
  Starting it early would mean starting it on the planner's block alone and letting it assume the rest, which is the inherited-context failure this project exists to show the absence of.

The planner makes this call per request, and it is right not to invent a dependency.
On the first Day 13 live run the situation block gave the coordinator both release SHAs, so Deployment needed nothing from GitHub, and the coordinator planned them in parallel.
The scenario was changed, not the coordinator (war story #9): a sequential demo has to carry a real data dependency.

## How it is enforced

- **In the contract.** `mode` and `depends_on` move together: `parallel` requires `depends_on: []`, `sequential` a non-empty list that resolves inside the plan, acyclically (`SelectionPlan` validators).
  A plan that says "sequential" without naming what it waits for does not validate.
- **In the handoff.** A dependent's `context_passed` is the planner's block plus a bounded digest of each direct dependency's response, composed when the dependency returns and recorded verbatim (`coordinator/handoff.py`, decision #20).
  On Day 13 Deployment's context was 3,706 characters: 642 of the planner's block, then GitHub's 24-line digest, from which it took the merge commit it diffed to.
  The dependency is visible in the response, not only in the timing.
- **In the scheduler.** Below.

## What the scheduler does

The agents block on the synchronous Anthropic SDK, so the executor runs them on a thread pool with one `Usage` accumulator per invocation, merged on the calling thread after each returns (decision #15).
Results are settled in plan order, never completion order, so the response does not depend on which agent was fastest.

**Until Day 27 a dependent waited for the whole parallel group, not for its dependency.**
The round-zero loop ran the group, joined it, and only then started the chain.
The Day 25 trace showed the price: Deployment depends on GitHub only, and in run 1 it sat idle for 34 s after GitHub had finished, waiting for Docs.
In run 2 GitHub happened to be the slowest of the group and nothing waited, which is why the defect survived every earlier run: on Days 15 and 21 GitHub was also the slowest, and "Deployment started 0.0 s after GitHub ended" was true by coincidence.

**Now each invocation starts the moment its own dependencies have settled** (`Executor._run_plan`).
The parallel group starts at once; a dependent is composed and submitted as soon as the invocations it names have returned, whatever its siblings are still doing; an invocation whose dependency failed, or whose agent is not registered, is decided without running and gets its honest `Gap`.
Composition and every merge stay on the calling thread, and the settle order is the old one, so responses, gaps, and cost are assembled exactly as before; only start times moved, and every existing executor, refinement, and tracing test passed unchanged.

`tests/test_executor.py` pins it the way the Day 9 barrier test pinned concurrency, by making the wrong schedule fail rather than merely slow: Docs cannot finish until Deployment has started, and Deployment depends only on GitHub.
Under the old scheduler Docs timed out after 5 s, and the refinement loop's retry of the failed Docs then rescued the run's status, so the test asserts no refinement round ran as well as the outcome.
Two dependents of one dependency now run concurrently with each other, each with the digest; nothing in the contract asks for them to be serialised, because `sequential` means "waits for its dependencies", not "waits for everyone".

**Not yet measured live.** Under the $0 rule (HANDOFF sec 1) the change is proven offline only.
On the Day 25 run 1 shape it would have taken 34 s off 154 s; on run 2's, nothing.

## Rounds are sequential for the same reason

A refinement round's invocations run in parallel with each other: different agents, each closing its own gaps.
Rounds run one after another, because a round's input is the previous round's gaps and digests; round *n+1* cannot be composed before round *n* has returned.
Same rule, one level up.

## What parallelism costs, and what sequencing costs

Parallelism costs no tokens: the same calls are made either way, only the wall clock changes.
Sequencing costs tokens, because the digest goes into the dependent's context and a tool-driven agent re-sends its context on every tool-loop round (about 3,000 characters of handoff on Day 13, against a 7k-token diff reply re-sent the same way).
That is why the digest is bounded with a hard ceiling (decision #25) and why a dependent gets its direct dependencies' digests only, never a chain of them (decision #20).
