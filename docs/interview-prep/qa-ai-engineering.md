# Q&A - AI / LLM engineering

Answers grounded in this project. Every number is in [`numbers.md`](numbers.md).
The war stories are in [`war-stories.md`](war-stories.md); this file is the shorter, question-shaped version.

---

## Structured output

**Q: How do you get reliable structured output from an LLM?**

Forced tool use, with the schema generated from the same models that validate the response. `tool_choice={"type": "tool", "name": ...}` guarantees the model emits something matching a JSON Schema, and generating that schema from the Pydantic models means the wire shape cannot drift from the contract.

But structural conformance is the easy half. The schema guarantees *shape*; it cannot express cross-field rules, and those are what models actually get wrong. My contract requires `*_detail` to be non-null exactly when its partner enum is `other`. `model_json_schema()` emitted `kind_detail: string | null` with only an auto-derived title, so the wire said nothing about when it applied. That rule was in my system prompt, and on the first live run **every model tested violated it** - Haiku seven times in one response.

The fix was per-field descriptions carrying the rule, on the fields they constrain. Descriptions only, no shape change. The `*_detail` failures vanished across all three models.

So: forced tool use for shape, field descriptions for rules, and a validator for the invariants that span the whole response.

**Q: Why not JSON mode or a response-format parameter?**

Forced tool use was the right fit here because the schema is large (18 `$defs`) and generated from existing models, and because I wanted the *tool* framing - the agent's job is "emit a report", which reads naturally as a tool call. For a small flat extraction I would reach for structured outputs directly.

The real answer is that the mechanism matters less than what you do about the rules the mechanism cannot express. Both approaches leave you needing field-level guidance and post-validation.

**Q: What do you do when the model returns something that fails validation?**

Right now it raises, loudly, on purpose - the retry loop is scheduled work and I did not want a half-built one masking failures during the phase where I am still learning what fails.

The one thing I *did* build early is distinguishing failure *kinds*, because they need different responses. A truncated response is not a schema violation, and conflating them cost me time: Opus failed with `overall_confidence: Field required`, which reads as a model ignoring the schema. `stop_reason` was `max_tokens` at exactly 4096 tokens. The model had written a full report and been cut off mid-JSON; the surviving fragment was valid JSON with a missing tail. So `diagnose` now checks `stop_reason` before validating and says "truncated at the max_tokens limit" instead.

The retry design that follows from this: format errors are retry-resolvable and worth re-sending with the specific validation error attached. Genuinely-absent information is not - that should become a `Gap` with `resolvable: false` rather than another attempt at the same impossible answer. Tracking those two separately is the interesting metric.

---

## Model selection and cost

**Q: How did you choose which model to use?**

By measuring, and the measurement changed my mind twice.

I wanted the cheapest model that worked. A single run had all three models returning contract-valid output, which pointed at Haiku. Then I ran `--repeat 3`: **Haiku 1/3, Sonnet 3/3.** Haiku's two failures were different each time - a dangling evidence id, then an invalid enum - so it was not one fixable weakness but general difficulty holding several cross-field invariants at once.

The second surprise was the arithmetic. Haiku is $1/$5 per Mtok against Sonnet's $3/$15, so "use Haiku" looks like 3x cheaper. Sonnet is currently $2/$10 introductory, so it is really 2x. And at 1-in-3 validity you need ~3 Haiku attempts per usable answer - which costs **the same or more** than one Sonnet call that works. Cheap-model-plus-blind-retry was not cheaper.

Default is Sonnet. Haiku is not unusable, it is *un-retried*: a retry that re-sends with the validation error attached should land in ~1.5 attempts, which does beat Sonnet. So it is a decision that gets revisited when the retry loop exists, not a closed one.

**Q: A single pass showed all models passing. How do you decide n?**

n=1 on a non-deterministic system is an anecdote, and I nearly shipped a decision on one. n=3 was enough to separate 1/3 from 3/3 unambiguously - that gap does not need statistics.

What matters more than the exact n is recording *per attempt* rather than in aggregate. Haiku's two failures being different invariants is the finding; a "33%" summary would have hidden it and I would have concluded "add one retry" instead of "this model struggles with cross-field constraints generally."

**Q: What else would you measure that you have not?**

Token spend per call, in the run records. I have durations and outcomes but not cost, so the Haiku-versus-Sonnet arithmetic was done by hand off published pricing. That is the input to every cheap-model question and it should be automatic.

Also prompt caching. My system prompt plus tool schema is byte-identical on every call and sits at the front of the prefix - textbook cacheable, and not yet done.

---

## Agent architecture

**Q: How does your coordinator decide which agents to invoke?**

An LLM call with a forced tool that returns a validated plan: classified intent, the agents to invoke with the context each receives, and every agent *not* invoked with a reason.

The design decision worth talking about is that the graded behaviours are enforced by validators rather than requested in prompt text:

- Every agent must appear in `selected_agents` or `skipped_agents` exactly once. A missing agent is indistinguishable from one nobody considered, so accounting for all four makes the selection auditable.
- `parallel` requires empty `depends_on`; `sequential` requires non-empty, and every id must resolve inside the same plan - otherwise the executor deadlocks waiting for an invocation that never runs.
- `context_passed` must be non-empty.

A prompt that usually produces the right behaviour is not evidence of the behaviour. A response that fails to validate is.

**Q: What stops the coordinator just selecting everything?**

Nothing structural, and that is deliberate - some queries genuinely need all four. Over-selection is a *quality* signal, not an invariant, so making it an error would be wrong.

What I do instead: the skip list makes over-selection visible (an empty `skipped_agents` on a narrow query is the tell), the prompt states that selecting four on a narrow question is a failure, and the live check's cases are chosen so that over-selection *fails the case*. `narrow_incident` expects exactly one agent and three skips. Measured: it selected `[incident]` and skipped three with specific reasons.

Not every quality property should be a hard constraint. Knowing which to enforce and which to measure is the actual design work.

**Q: Explain explicit context passing. Why does it need enforcing?**

Each subagent receives everything it needs in its own prompt, with no automatic inheritance from the coordinator. The schema enforces `context_passed` non-empty.

But non-empty is a weak bar, and the subtler failure is a coordinator that satisfies it by echoing the user's question back as context. That passes the check and tells the subagent nothing it would not have had anyway - inheritance with extra steps. So the planner also rejects a `context_passed` that only restates the query.

There is a deliberate asymmetry that makes the point: the coordinator receives a `situation` block (live metrics, on-call state) which is **not** automatically forwarded. The model has to decide what each agent needs and write it into that agent's context. Measured output: 37-75 words of real facts per agent - service names, the time window, what has been ruled out.

**Q: When would you not use an agent?**

When a deterministic workflow would do the job. Most of this project's routing could be keyword rules; I used a model because dynamic selection with justified skips is the graded capability, and because the reasons in the skip list are genuinely useful output that rules could not produce.

The honest general answer: agents earn their cost when the task is multi-step and hard to fully specify in advance, the outcome justifies the latency, and errors are recoverable. My four failure modes are recoverable and the diagnosis is genuinely open-ended, so it fits. A fixed extract-and-classify pipeline would not.

---

## Prompting

**Q: What is the most useful prompting lesson from this project?**

That the schema is part of the prompt, and it is the part models weight most heavily.

I had the `other`/`detail` rule stated clearly in my system prompt. Every model ignored it, because the schema said `kind_detail: string | null` and that is where the model looked. Moving the rule to the field description fixed it. Same words, different location, different outcome.

The corollary bit me too: the top-level description of my structured-output tool was my *developer* docstring, talking about "the plumbing a caller fills in." That is noise-to-signal in the highest-attention position in the whole request, and Sonnet responded by inventing a `report` wrapper object. Replacing it with a model-facing description - "every property below is a top-level argument, do not nest them" - fixed that.

**Q: How do you keep prompts from drifting apart?**

Compose them from shared constants. The Incident agent has a prose path and a structured path, and both build from one `_GROUND_RULES` string containing the SRE persona, the evidence-citation rule, and the confidence-band table quoted from the contract. The band table in particular is scored by the eval harness against those exact ranges, so a divergence between the two prompts would be a silent scoring error.

For the schema guidance I went further: it is keyed by field name, and a rename in the contract models would silently drop a rule the model depends on. It raises at import instead, with a test proving it.

---

## Evaluation

**Q: How will you evaluate this, and what have you already got?**

The eval set is the seeded corpus: 18 synthetic incidents with recorded ground truth in `true_failure_mode` columns. Coverage is deliberate - four rows for each real failure mode - because a mode with zero rows **cannot be scored at all**; the agent could never be right or wrong about it.

What exists now is one case end-to-end, which is the harness in embryo: inject a known fault, read the resulting metrics from Prometheus, hand the agent only those metrics, compare its `failure_mode` to what was injected. It got `downstream_latency` right at 0.55 confidence and named both affected services.

**Q: What is the biggest threat to that eval's validity?**

Leaking the answer key, and it would look like success.

The demo app publishes every injected chaos knob as a Prometheus gauge - that is what makes ground truth readable. If `chaos_knob_value` reached the agent's context, the agent would read the fault off the gauge and scores would jump to near-perfect with nothing erroring anywhere. The only symptom of a broken eval would be excellent results.

So the exclusion is enforced in code at two levels - the query battery is checked before it runs, and the rendered context is checked before it is returned - with tests for both, including one that simulates someone adding the metric later. Ground truth and model input are structurally separated rather than kept apart by convention.

**Q: Confidence scores - are they meaningful?**

Not yet verified, and I have one data point suggesting they need verifying. The contract defines normative confidence bands and the prompt quotes them verbatim. On identical input, Haiku reported 0.82 where Sonnet said 0.62 and Opus 0.63 - and Haiku found half as many gaps. Overconfident and less thorough.

That is exactly why calibration is a scored dimension rather than an assumed property. A confidence number nobody checks is decoration.

---

## Context management

**Q: How do you keep context from bloating?**

Structurally, so far, rather than by trimming - trimming work is scheduled later, and doing it before there is a baseline would be optimising blind.

What is in place: every MCP tool response carries a required `meta` block with `truncated`, `returned`, `total_available`, and `token_estimate`. That is deliberately there *before* any reduction work, because without it there is no baseline to reduce against. The tool also caps results and reports the real total when it truncates, so an agent knows it is looking at a subset rather than silently trusting a short list.

The other structural choice is that subagent context is *composed*, not inherited. The coordinator writes each agent a purpose-built block instead of forwarding everything it knows. That bounds context by construction rather than by cleanup.

**Q: Anything you would do differently?**

Enable prompt caching from the start. My system prompt and tool schema are byte-identical across every call and sit at the front of the prefix, which is the ideal case. I left it out to keep the first implementation simple and it is now a retrofit rather than a default - small, but it is free money I have not collected.
