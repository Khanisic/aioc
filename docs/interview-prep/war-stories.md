# War stories

Seven things that went wrong, what the symptom looked like, and what it actually was.
These are the answers to "tell me about a time when..." questions.
Every one is traceable to a recorded run under `test-results/` or to a commit.

The through-line worth naming out loud: **six of the seven looked like the model being unreliable and were not.**
Five were engineering defects on my side and one was my own test being wrong.
That is the most useful thing I learned building this, and it is a better interview answer than any architecture description.

---

## 1. The model wasn't wrong, my token budget was

**Symptom.** The first live run of the Incident agent's structured output failed on Opus with `overall_confidence: Field required`.
Reads unambiguously as a model failure: the model omitted a required field.
The obvious next move is to strengthen the prompt.

**What it actually was.** `stop_reason` was `max_tokens`, and output was exactly 4096 tokens - the harness default.
Opus had written a full incident report and been cut off mid-JSON.
The tool-use block still contained everything that had parsed, so Pydantic reported the first field that never arrived.

**Why it was misleading.** A truncated structured-output call does not look truncated.
It looks like a model that ignored your schema, because the surviving fragment is valid JSON with something missing.
I would have spent an hour on prompt engineering for a problem that was one config value.

**Fix.** Two parts, and the second matters more.
Raised the default to 8192, since a full incident report does not fit in 4096.
Then made `diagnose` check `stop_reason` *before* validating, so truncation reports itself:

> `emit_incident_report output was truncated at the max_tokens limit (4096 output tokens); the report is incomplete. Raise AIOC_MAX_TOKENS or narrow the query.`

**Transferable lesson.** When a structured-output call fails validation, check `stop_reason` before you touch the prompt.
And when you find a misleading error, fix the *diagnosis* as well as the cause - the same trap catches the next person otherwise.
There is a regression test named `test_diagnose_names_truncation_instead_of_blaming_the_model` for exactly that reason.

---

## 2. A generated JSON Schema states shape but not rules

**Symptom.** First live structured-output call failed on **every** model, differently.
Haiku filled seven `*_detail` fields on enums whose value was not `other`.
Sonnet wrapped the entire payload in a `report` key that was not in the schema.

**Diagnosis.** The good news came first: the API *accepted* an 18-`$def`, heavily `$ref`-based schema, retiring a risk I had flagged.
So the problem was elsewhere.

The contract has a cross-field rule: `*_detail` must be non-null exactly when its partner enum is `other`.
`model_json_schema()` emitted every field with only an auto-derived `title` - so the wire advertised `kind_detail: string | null` with nothing saying when it applied.
The rule existed only in the system prompt, and the schema is where a model looks hardest.
The wrapper object had the same root cause from the other end: the top-level description was my developer docstring, talking about "the plumbing a caller fills in" - noise that invited the model to invent structure.

**Fix.** An annotation layer over the generated schema: a model-facing top-level description, plus per-field descriptions carrying the rules. Descriptions only, no shape changes, and `contracts/` untouched, because the contract is frozen and these are prompt affordances rather than data.
The `*_detail` epidemic disappeared on all three models.

**The bit I am most pleased with.** The guidance is keyed by field name, so a rename in `contracts/` would silently drop a rule a model depends on.
It raises at import instead. `test_schema_guidance_fails_loudly_when_a_contract_field_is_renamed` proves it.

**Transferable lesson.** If a rule spans two fields, a generated schema cannot express it - put it in the field descriptions, not only the system prompt.
And the top-level description of a structured-output tool is prompt real estate, not documentation.

---

## 3. One run said Haiku was fine. Three runs said it wasn't

**Symptom.** After the schema fix, all three models returned contract-valid output. 1/1 each.
I was trying to move to the cheapest model that worked, so this looked like the answer: Haiku.

**What changed my mind.** I ran it again with `--repeat 3`.
Haiku scored **1 of 3**. Sonnet scored **3 of 3**.
Haiku's two failures were *different each time* - once a dangling evidence id referencing an entry not in `evidence[]`, once an invalid `suggested_agent` enum value.
Not one fixable weakness; a general difficulty holding several cross-field invariants at once.

There was a second signal I only saw because it was recorded. Haiku reported `overall_confidence: 0.82` where Sonnet and Opus said 0.62 and 0.63 on the same input, and found half as many gaps.
Overconfident *and* less thorough - and confidence calibration is a thing the eval harness will score.

**The cost trap, which is the interesting part.** Haiku is $1/$5 per Mtok against Sonnet's $3/$15, so "use Haiku" looks like a 3x saving.
Two corrections. Sonnet is currently $2/$10 introductory, so the real gap is 2x. And at 1-in-3 validity you need about three Haiku attempts per usable answer - which costs *the same or more* than one Sonnet call that works.
**Naive retry on the cheap model was not cheaper.**

**Where that leaves it.** Default is Sonnet. Haiku is not unusable, it is *un-retried*: a retry loop that re-sends with the validation error attached should fix it in ~1.5 attempts rather than re-rolling blind, and that genuinely beats Sonnet. So Haiku is a Day 17 decision, not a Day 4 one.

**Transferable lesson.** A single pass on a non-deterministic system is an anecdote.
Model selection needs n>1 and a per-attempt record, and cost-per-*valid*-output is the metric, not cost-per-call.

---

## 4. Every credential matched and the database still refused me

**Symptom.** The MCP tool's integration tests failed with `password authentication failed for user "aioc"`.
Classic wrong-password error.

**The hunt, which is the story.** I checked the obvious things and they were all fine.
`DATABASE_URL` was set in `.env`. The container's `POSTGRES_PASSWORD` was the same length.
Since I could not read `.env` (settings deny it, correctly), I compared **SHA-256 prefixes** of every copy of the password - the one pydantic-settings loaded, the one inside `DATABASE_URL`, the compose default, and the container's environment.

All four hashes were identical.
The password was right everywhere, and authentication still failed.

That is when it stopped being a credentials problem. `netstat -ano | grep :5432` showed **two** processes LISTENING: Docker's proxy, and a native Windows PostgreSQL service.
Whichever won the race got the connection, and the native one has no `aioc` role.

**The irony.** The project's own `.env.example` says: *"Set them if 5432 or 6379 are already taken on your machine - a port collision is the most common Day 1 failure."*
I had written that warning and still lost time to it, because the error message pointed at credentials.

**Fix.** Republished Postgres on 55432 (a container recreate, so the named volume and the seeded corpus survived).
Then made the integration tests **skip with the diagnosis** rather than fail, with the full checking order in the skip reason - stack up? two PIDs on the port? - so the next person reads it instead of finding it.

**Transferable lesson.** When every input to a check is provably identical and the check still fails, stop verifying inputs and question *what is answering*.
Also: comparing hashes is a clean way to prove two secrets are equal without either one reaching a log.

---

## 5. Zero and "unknown" are different, and I shipped the wrong one

**Symptom.** Nothing failed. The Day 5 checkpoint passed, and the agent correctly diagnosed the injected fault.
But reading the context it had been handed, every service showed `5xx ratio not measured`.

**What it actually was.** The error-ratio query divided 5xx rate by total rate.
For a service with traffic and no errors, the numerator matches **no series** - so the division returns nothing, and my formatter rendered absent as "not measured".
The truth was "zero errors". I was telling the agent nobody had looked.

This is precisely the distinction the project's contract is built around: `null` means not determined, `[]`/`0` means looked and found nothing, and an agent is required to tell them apart and emit a `Gap` for the former.
I had enforced that rule in the models and then broken it in the data going *in*.

**Fix.** The PromQL idiom `... or <denominator> * 0`, which supplies an explicit zero for any service that has traffic while leaving a genuinely unscraped service absent.
Verified against live Prometheus: 0 series before, 3 series reading `0` after.

**Why it matters beyond the bug.** The agent had hedged to `0.55` confidence, partly because it could not see error rates. Feeding it "unknown" instead of "zero" makes it *correctly* less certain - so the defect degraded output quality invisibly, with no error anywhere.

**Transferable lesson.** An absent series and a zero series mean different things, and most metric code conflates them.
If your system distinguishes "no data" from "no problem" - and an ops system must - that distinction has to survive the query layer, not just the schema.

---

## 6. The failing test was the thing that was wrong

**Symptom.** I wrote a check asserting every timeline event falls inside its incident's time window. One event failed it.

**What it actually was.** The event was a deploy at 15:11 for an incident whose window opens at 15:12.
The trigger preceded the damage it caused - by a minute.

The data was right and my assumption was wrong. That one-minute gap is the *most* diagnostic fact in the record: connecting a deploy to the error spike that followed is exactly the inference an incident agent has to make.
Forcing containment would have deleted the best evidence in the corpus to satisfy an invariant nobody had asked for.
I checked the contract: it validates ascending order only. Containment was never required.

**Fix.** Kept the data, deleted the check, and left a comment at the bottom of the test file saying *why* containment is deliberately not tested - so nobody "fixes" it later.

**Transferable lesson.** When a new assertion fails on data you believe is correct, the assertion is a hypothesis too.
Check the spec before you change the data. And record the decision where the next person will trip over it, not in a commit message they will never read.

---

## 7. Asking the model for a field I already knew the answer to

**Symptom.** The very first live run of the Day 7 delegation check died before the agent was ever invoked:

> `1 validation error for SelectionPlan / selected_agents.0.round / Field required`

Sonnet had produced a well-formed routing plan - right agent, three real skip reasons, 103 words of genuine context - and omitted `round`, an integer the schema marked required and whose field guidance said, in plain English, "0 for the initial plan."

**What made it interesting.** Nothing was wrong with the contract or the code.
`round` is required in CONTRACTS.md §5, the Pydantic model enforced it correctly, and 186 offline tests were green.
They were green because every fixture I had written by hand included `round` - the field is trivially easy to remember when you are a human filling in a dict.

**What it actually was.** A design error one layer up: `round` was in the model-facing schema at all.
It is not a routing decision. It is bookkeeping the coordinator owns - 0 on the initial plan, incremented by the refinement loop - so the coordinator always knows it and the model can only ever agree or be wrong.
I had already solved this exact problem on Day 4, where `IncidentReport` deliberately excludes `request_id`, `invocation_id`, and `generated_at` because they are the caller's plumbing.
I just did not notice the coordinator had the same shape.

**Fix.** A `PlannedInvocation` type: `AgentInvocation` minus `round`, used only to generate the tool schema.
The planner stamps the value after the model answers, overwriting rather than defaulting, so a model that volunteers a round number does not get to be authoritative about it.
`Coordinator.plan` grew a `round_number` argument, which is what the Day 14 refinement loop will pass.
The frozen contract did not change: `AgentInvocation` still requires `round`, and there is now a test asserting exactly that, so the narrower ask cannot be mistaken for a relaxation.

**Transferable lesson.** Every field in a structured-output schema is a chance for the model to be wrong, so a field whose value you already know is pure downside - remove it from the ask and stamp it yourself.
And the sharper one: **fake-driven tests inherit the assumptions of whoever wrote the fixtures.**
Mine encoded "of course `round` is present" and could not have caught this. Two API calls could, and did, on the first attempt.
That is the clearest argument for the opt-in live scripts that I have found so far - the offline suite was not wrong, it was answering a question that did not include this one.

---

## How to tell these in an interview

Lead with the symptom, not the answer.
"A required field was missing from the model's output" invites the interviewer to guess along with you, and the reveal - it was truncated at exactly 4096 tokens - lands.
Opening with "I had a max_tokens misconfiguration" throws the story away.

Have one sentence ready for what you changed *besides* the fix.
Every story above has one: the truncation check, the import-time drift guard, the `--repeat` flag, the skip-with-diagnosis, the query idiom, the comment explaining a deliberate absence.
Fixing the bug is table stakes. Making the failure legible next time is the part that reads as seniority.
