# Decisions

The architectural calls, why they were made, and what I would change.
Each one is a "why did you do it that way?" answer.

---

## 1. Freeze the integration contract on day one

**Decision.** `docs/CONTRACTS.md` defines every shape crossing between the two layers and is frozen at `1.0.0`. Changing anything frozen needs written agreement, a `schema_version` bump, and a changelog row. There is exactly one pre-authorised exception, documented in advance.

**Why.** Two engineers building a reasoning layer and a platform layer in parallel have one dominant failure mode: the interface drifts, and every integration attempt breaks for a new reason. Freezing the interface is what converts "we integrate at the end and lose a week" into "we integrate continuously."

**What makes it real rather than aspirational.** The contract is *executable* - `src/aioc/contracts/` is the same shapes as Pydantic v2 models with `extra="forbid"`, and `tests/test_contract.py` validates them against the worked example read directly out of the Markdown. A prose contract nobody can run is a wish.

**The uncomfortable part, and the honest answer.** Freezing on day one means freezing while you are at your most ignorant. It has already cost something: `get_incident_timeline`'s error list names `PROMETHEUS_TIMEOUT`, written when I assumed timelines would be metric-derived, but the events actually live in Postgres. Emitting `PROMETHEUS_TIMEOUT` for a Postgres timeout would put a false value in a field the contract says is matched programmatically, so the tool emits `TIMELINE_STORE_TIMEOUT` and the deviation is flagged in the module docstring as a changelog candidate rather than done silently.

That is the tradeoff working as designed, not failing: the cost of a frozen contract is occasional friction like this, and the benefit is that friction is *visible* instead of being a silent divergence someone finds in week four.

**What I would do differently.** Freeze the *envelope* and the error taxonomy on day one - those held perfectly - and leave per-tool error code lists explicitly unfrozen until the tool exists. The generic structure was knowable up front; the specific codes were not.

---

## 2. Structured output via forced `tool_use`, not JSON mode or parsing

**Decision.** `IncidentAgent.diagnose` forces a single tool with `tool_choice={"type": "tool", "name": ...}` and reads the `ToolUseBlock.input` directly. The tool's JSON Schema is *generated* from the frozen Pydantic models.

**Why forced tool_use.** It guarantees the output matches a schema, and generating that schema from the contract models means the wire shape cannot drift from the contract. Hand-writing the schema would create two sources of truth that agree until they don't.

**Why not "return JSON" plus a parser.** Because then the schema lives in prose and the failure mode is a parse error with no field-level information. Forced tool use gives structural conformance for free and lets validation focus on the interesting rules.

**Why generate rather than hand-write.** The schema has 18 `$defs`. Maintaining that by hand alongside the models is a guaranteed drift source.

**The catch, and the interesting part.** A generated schema states *shape* but not *cross-field rules*. The contract requires `*_detail` to be non-null exactly when its partner enum is `other` - and `model_json_schema()` emits only auto-derived titles, so the wire said `kind_detail: string | null` with no hint when it applied. Measured: every model tested filled those fields regardless. The rule was in the system prompt, and the schema is where a model looks hardest.

So there is an **annotation layer** over the generated schema - a model-facing top-level description plus per-field rule text. Descriptions only, never shape, and `contracts/` is untouched, because these are prompt affordances rather than data. A guard raises at import if an annotated field disappears.

**What I would do differently.** Nothing structural, but I would write the annotation layer *first* rather than discovering the need from three failed live calls. "Generated schema plus hand-written field guidance" is the pattern; I arrived at it empirically instead of by design.

---

## 3. Enforce the graded orchestration behaviours in the schema, not the prompt

**Decision.** Dynamic selection, explicit context passing, and the parallel/sequential distinction are enforced by validators, not requested in prompt text.

- `AgentInvocation.context_passed` must be non-empty - an empty value means context was assumed inherited.
- Every agent must appear in `selected_agents` or `skipped_agents` exactly once - a missing agent is indistinguishable from one nobody considered.
- `parallel` requires empty `depends_on`; `sequential` requires non-empty, and every id must resolve inside the plan.

**Why.** A prompt that usually produces the right behaviour is not evidence of the behaviour. A validator that rejects the wrong shape is. This project exists to demonstrate that context is passed explicitly rather than inherited, so "the model is told to do it" is a weaker claim than "a response that fails to do it does not validate."

**The one I am most pleased with.** The contract catches *empty* `context_passed`. It cannot catch the subtler failure: a coordinator that satisfies the check by echoing the user's question back as context. That is inheritance with extra steps - the subagent learns nothing it would not have had. So the planner additionally rejects a `context_passed` that only restates the query. Measured output: 37-75 words of real facts per agent, not a restatement.

**Where the enforcement stops, deliberately.** Selecting all four agents is structurally *legal* - some queries genuinely need everything. Making it an error would be wrong. It is a signal to watch rather than a rule to enforce, and that distinction is itself a decision: not every quality property should be a hard constraint.

---

## 4. The MCP boundary is JSON Schema, and tool servers may not import the models

**Decision.** No tool server imports `aioc.contracts`. Enum members and input schemas are written out longhand in each server. A test asserts the longhand copies still match the Python enums.

**Why accept the duplication.** Because the alternative couples the two halves the wire format exists to separate. A tool server that imports the reasoning layer's models cannot be deployed independently, and a Pydantic-only refactor can break a tool. The duplication is the price of independence, and the drift guard is what makes it affordable - a copy that cannot silently diverge is a very different thing from a copy.

**Why a test rather than discipline.** Discipline does not survive a hurried afternoon. `test_the_server_does_not_import_the_contract_models` parses the module's *import statements* with `ast` rather than grepping the text - because the docstring explaining the rule contains the very string a substring search looks for. (I wrote the naive version first and it failed on its own explanation.)

**A related call worth mentioning.** The MCP library validates tool input against the schema by default and returns a **plain-text** error on failure. The contract requires a structured `validation` error carrying `details.field` and `details.expected`. Those are incompatible, so framework validation is off and validation is done by hand. That is a case where the library's convenient default was wrong for the contract, and noticing it required reading the library's source rather than its docs.

---

## 5. Never show the agent the answer key

**Decision.** The demo app publishes every injected chaos knob as a Prometheus gauge - which makes it the eval's ground truth - and `chaos_knob_value` is excluded from any agent's context by an enforced guard, at both the query level and the rendered-output level.

**Why enforce rather than intend.** Because the failure is *silent and it looks like success*. If that metric leaked into context, the agent would read the fault off the gauge and eval scores would jump to near-perfect. Nothing would error. The only symptom of a broken eval would be excellent results, which is the hardest kind of bug to notice and the easiest to want to believe.

**Why publish it at all.** Because the alternative - a manifest file the injector writes - can drift from what actually happened to the app. Reading ground truth out of Prometheus means the eval scores against the app's real state rather than against what a script believed it requested. Same reason `check_day5_checkpoint.py` recovers the injected mode from the gauges instead of from the injector's return value.

**Transferable shape.** Any eval with injected ground truth has this hazard. The rule I would carry forward: ground truth and model input should be *structurally* separated, with a guard on the boundary, not merely kept apart by convention.

---

## 6. Absolute timestamps in the seed corpus

**Decision.** The 18 seeded incidents use fixed literal timestamps, not `now() - interval`.

**Why.** The corpus is both the RAG corpus and the eval set. If it shifts between reseeds, two eval runs score different data - and the Day 24 token-reduction comparison against the Day 20 baseline would measure the corpus drifting rather than the context work. Deterministic beats fresh-looking.

**The cost, acknowledged.** The dataset ages. In six months the incidents look stale, and any "last 7 days" query finds nothing. That is the right trade for a fixed 30-day project with a baseline comparison in it; for a long-lived system I would generate relative timestamps from a seeded PRNG with a pinned epoch, which keeps determinism without the staleness.

---

## 7. Enum values as CHECK constraints, not Postgres ENUM types

**Decision.** The corpus schema uses `CHECK (col IN (...))` rather than `CREATE TYPE ... AS ENUM`.

**Why.** Adding an enum member is a minor version bump under the contract's change process, so it *will* happen. `ALTER TYPE ... ADD VALUE` cannot run inside a transaction on older Postgres and cannot be reverted; `ALTER TABLE ... DROP/ADD CONSTRAINT` is one reviewable statement either way. Optimise for the change you know is coming.

**Bonus.** The contract's `other`-plus-detail pairing is expressible as a `CHECK` too, so a bad seed row is rejected at insert rather than surfacing later inside a Pydantic validator where it reads as an agent bug.

---

## 8. Schema in `docker/postgres/init/`, migration tool deferred

**Decision.** Schema and seed live in `init/`. No migration tool yet.

**Why.** The volume was empty, so a destructive reset cost nothing, and a migration tool on day five is scaffolding that delays the checkpoint it is meant to support.

**What this contradicted.** `01-extensions.sql` said table creation must *not* live in that directory - the stated concern being two engineers racing in one shared file. A numbered file per concern avoids that, so the comment was revised rather than quietly violated.

**The limitation that actually matters** - and it is not the one the original comment named. `init/` runs *only* on first initialisation of an empty local volume. It never runs against hosted Postgres. So the Day 24 deployment has to apply these files by hand, every file is written to be runnable standalone, the seed is idempotent, and the Day 24 plan entry carries the commands. Adopt a real migration tool the moment a schema change must survive an already-populated database, because `init/` cannot express that at all.

---

## 9. Test-run records as structured JSON

**Decision.** Every `pytest` run and every live check writes a run summary plus per-event JSONL under `test-results/`, gitignored. Events are line-delimited; the summary is one object.

**Why JSONL for events.** They are appended one at a time and must survive the process dying mid-run. A truncated JSONL still parses line by line; a truncated JSON array is unrecoverable - and the run that crashed is the one worth reading.

**Whether it earned its keep.** Yes, immediately and more than once. The per-model failure diagnosis in `numbers.md` came out of `events.jsonl`, not scrollback: three models, distinct failures, each with the individual validation errors attached. That is what made "the `*_detail` rule is missing from the schema" visible as one pattern rather than three unrelated bugs.

**What I would add.** Token counts and cost per live call. The records have durations and outcomes; they should have spend. That is the input to every "is the cheaper model actually cheaper" question, and I had to do that arithmetic by hand.

---

## 10. Cost as a design constraint, not an afterthought

**Decision.** The 141-test suite makes **zero** API calls. Everything model-facing is driven by scripted fake clients. Live verification lives in four separate opt-in scripts, and the model-matrix script defaults to one call per model.

**Why.** A test suite that costs money per run stops being run, and a suite that is not run is not a suite. Splitting free-and-fast from costed-and-deliberate is what keeps the fast one honest.

**What it does not buy.** Fakes prove plumbing, never model behaviour. Every fake-driven test in this repo could pass while the live agent fails - which is exactly what happened on the first live run, when three models failed a path with 14 green offline tests. The fakes were not wrong; they were answering a different question. That is why the live scripts exist and why their results are recorded rather than glanced at.

**The honest framing for an interview.** Offline tests prove I did not break the wiring. Live scripts prove the model can do the task. Conflating the two is how you get a green build and a broken product.
