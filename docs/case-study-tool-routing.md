# Case study: tool routing before and after the `1.1.0` split

The Domain 2 routing case study, as planned in `BUILD_PLAN.md` Phase 2: build two deliberately overlapping tools, measure how often a model picks the wrong one, split and rename them, re-run the same queries, and write up the before and after.
This is that write-up.
The numbers are recorded runs under `test-results/`; the query sets are hashed into every record so the two measurements are provably over the same forty questions.

**The result is 0/40 misrouted before and 0/40 after.**
The intervention the case study was designed to measure did not change the outcome, because the outcome was already at the floor.
That is a finding, and this document says what it means rather than dressing it up.

## The question

CONTRACTS.md sec 6.5 requires every tool description to carry four parts in order, and says of the last one: "Part 4 is the one that matters."
Part 4 names the competing tool and states the discriminator.
The case study asks whether that is true: given two tools with the same inputs and the same output shape, does a description that withholds part 4 cause a model to misroute, and does a description that states it stop the misrouting?

## The two tools

Both read "what happened to a service in a time window", and the difference is the kind of data, not the shape of the answer.

| | What it reads | Source in this stack |
|---|---|---|
| logs | what the service's process itself wrote - stdout and stderr lines, tracebacks, request entries | the container's buffer through `docker compose logs` (there is no log collector) |
| events | what was recorded as happening *to* the service - deploys, rollbacks, restarts, scale actions, config changes, alerts, threshold crossings | the seeded incident history in Postgres |

Both take `service`, `start`, `end`, `pattern`, and `max_matches`, plus one filter each (`level` for logs, `kind` for events).
Both return `matches`, `total_matched`, and `patterns_detected`.

### Before: `1.0.0`, deliberately weak

`analyze_logs` and `analyze_events` (`src/aioc/tools/incident/analyze_server.py`, the `aioc-analyze` server).
Parts 1-3 of each description are complete and honest: part 1 says what the tool searches, part 2 gives four example queries, part 3 states the limits and what an empty result means.
Part 4 is the contract's v1.0.0 sentence verbatim, and it names no alternative:

- `analyze_logs`: "When to use this vs the alternative: Use this to analyze service output over a time window."
- `analyze_events`: "When to use this vs the alternative: Use this to analyze service activity over a time window."

A test pins these sentences, so the baseline cannot be sharpened by a helpful edit.

### After: `1.1.0`, the pre-authorized split

`search_container_logs` and `search_recorded_events` (`src/aioc/tools/incident/search_server.py`, the `aioc-search` server), made on 2026-09-17 under the CONTRACTS.md sec 0 pre-authorization, with the rationale written first in `docs/design-notes/contract-changes.md`.
Two things changed and nothing else:

1. **The names.** Each name now carries the discriminator: *container logs* versus *recorded events*.
2. **Part 4.** Each now names the other tool and states when to use which, in both directions, and says explicitly that the kind of data decides and the word in the question does not.

Parts 1-3 are the v1 text verbatim, composed from the v1 module at import and pinned by a test; the input schemas are the v1 objects; the implementation is imported from the v1 module.
The independent variable is therefore exactly the name plus part 4.

The v1 definitions stay in CONTRACTS.md struck through, and the v1 server stays runnable, so the "before" can be re-measured on the same wire at any time.

## Method

`scripts/check_tool_routing.py` lists the chosen variant's tools from its real stdio server (the descriptions under test are the shipped ones, not copies), gives a model both tools and one operator question, and forces it to pick exactly one (`tool_choice: any`).
The tool is never executed; the routing decision is read off the `tool_use` block.
Ground truth for each query is the kind of data it needs, decided when the query was written and before any model saw it.
Neither tool name appears in any query.

Two query sets, twenty questions each, ten per ground truth:

- **`plain`** - operator questions phrased the way an operator phrases them ("Show me every ERROR line payments-api printed between 14:00 and 14:15 UTC today", "Which alerts fired on inventory-api on 2026-01-22?").
- **`hard`** - the same twenty ground truths with the surface vocabulary pulling the wrong way: "the deploy log", "the operational output", "the error events it emitted", "analyze its activity", "print the restart history", "ignore stderr; I want the sequence of deploys".
  Written after the plain set came back clean on the baseline and before it was run, because a set that cannot produce a misroute cannot measure an intervention.

Every run record carries the model, the variant, the server module, the tool-name mapping, and the sha256 of each query set.
The ground truth is expressed in the v1 names and mapped onto each variant's names, so the hashes are identical across variants.

| Set | `queries_sha256` |
|---|---|
| `plain` | `b0da59605dcd382a42d73e2c9d9817369bd7ad5ce134a515456c511b6879c54a` |
| `hard` | `9e31bedbe689cdcef308c75c774ff06fb8352388732d8d8a52b596894a136550` |

Model: `claude-sonnet-5`, the harness default, on both runs.

## Results

| Variant | Set | Misrouted | Input tokens | Output tokens | Run |
|---|---|---|---|---|---|
| `v1` (before) | `plain` | **0 / 20** | 57,305 | 2,478 | 2026-09-13 |
| `v1` (before) | `hard` | **0 / 20** | 57,334 | 2,474 | 2026-09-13 |
| `v1_1` (after) | `plain` | **0 / 20** | 62,925 | 2,475 | 2026-09-17 |
| `v1_1` (after) | `hard` | **0 / 20** | 62,954 | 2,522 | 2026-09-17 |

Per ground truth, in every cell: 0/10 for the logs tool and 0/10 for the events tool.
No call errored.
Mean latency of a routing call on the after run: 1.8 s.

The one measurable effect of the split is cost: the longer part 4 adds about 280 input tokens to every call that carries the two descriptions, a 10% increase on this prompt.

## What the numbers mean

**The premise did not materialise under these conditions.**
With parts 1 and 3 stating what each tool reads - "the output a service's container printed" against "the recorded operational events for a service" - and part 3 of each saying outright what it is *not* ("a deploy, a restart, or a config change is not something a process prints about itself"; "this is the incident history, not the service's own output"), Sonnet never needed part 4.
It routed forty questions correctly with a part 4 that said nothing, including twenty written to pull it the wrong way.

**So part 4 is insurance, not the load-bearing wall - for this model, with descriptions written to the template.**
That is a narrower claim than the contract makes, and it is the honest one.
The sec 6.5 template still earns its keep: the discriminator was present in the baseline, in parts 1 and 3, and a model good enough to read a paragraph found it.
What part 4 buys is a place where the discriminator is stated in one sentence, next to the name of the competitor, for a model or a prompt that will not read the paragraph.

**The split is still the right shape.**
The names now say what the tools read, part 4 says when to use which in both directions, and a description that names its alternative is what the contract requires of every tool.
The refactor cost 280 tokens per call and a paragraph of paperwork; it did not cost a measurement, because the baseline is preserved and re-runnable.

## What would have produced a non-zero baseline

Two levers, in the order they would be pulled.
Neither was pulled unasked: the standing rule on this project is no model matrix without a request, and each is one flag or one module away.

1. **A cheaper router.** `scripts/check_tool_routing.py --model claude-haiku-4-5-20251001` runs the same forty queries through Haiku, at a fraction of Sonnet's price.
   Haiku is also the Day 23 question (a cheaper coordinator model), so a Haiku baseline would serve two measurements.
2. **Parts 1-3 written as loosely as part 4.** A second `v1` module with a one-line part 1, generic examples, and no "what this is not" sentence in part 3 is what a hastily written real tool looks like, and it is the condition under which part 4 would be the only discriminator.
   `VARIANTS` in the script is the one place to register it.

A third, weaker lever: more tools in the list.
With only two candidates and a forced choice, a model that can tell the two apart at all will score perfectly; the routing check could list the other four servers' tools alongside these two.

## What this proves for Domain 2

Tool *design*, not tool consumption: the four-part template, the discipline of stating what a tool is not, a frozen contract with a pre-authorized change made through its own process, and a measurement whose before and after are provably over the same inputs.
And the part a skeptical reviewer should weigh most: a case study built to show an intervention working, which instead showed the baseline already at the floor, and reported that.

## Reproduce

```bash
uv run python scripts/check_tool_routing.py --dry-run                 # free: both sets, both hashes
uv run python scripts/check_tool_routing.py --variant v1              # 40 calls: the baseline
uv run python scripts/check_tool_routing.py --variant v1_1            # 40 calls: the split
uv run python scripts/check_tool_routing.py --variant v1_1 --set hard # 20 calls
```

Records: `test-results/runs/2026-09-13/*tool-routing-v1*` (before) and `test-results/runs/2026-09-17/*tool-routing-v1_1*` (after); each holds `queries.json`, `descriptions.json`, and `routing.json` with the per-query decisions.
