# War stories

Ten things that went wrong, what the symptom looked like, and what it actually was.
These are the answers to "tell me about a time when..." questions.
Every one is traceable to a recorded run under `test-results/` or to a commit.

The through-line worth naming out loud: **eight of the ten looked like the model or the orchestration being unreliable and were not** - five were engineering defects on my side, and three were my own test or scenario being wrong.
The other two looked like bad credentials and were the environment naming the wrong cause.
The one genuine model failure in the set (the nested synthesis argument in #10) was fixed by changing the schema's shape, not by arguing with the prompt.
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

## 8. The keys were valid and Langfuse still said they were not

**Symptom.** The first run with real Langfuse keys printed `Failed to export span batch code: 401, reason: Unauthorized` from somewhere in the background - and then the check script crashed with `UnauthorizedError` *after* printing a passing-looking report.
The 401 body said: "Invalid credentials. Confirm that you've configured the correct host."

**The hunt.** I could not read `.env` (settings deny it, correctly - same constraint as story 4, same tools).
Loading the settings object and printing only prefixes and lengths showed both keys the right shape in the right slots: `pk-lf-` and `sk-lf-`, 42 characters each.
SHA-256 hashes proved public and secret were different values, ruling out a double-paste.
So the keys were fine, and the credentials error was lying about something.

The probe that ended it: the same basic-auth request against both regional hosts.
The default EU host (`cloud.langfuse.com`) returned 401; the US host (`us.cloud.langfuse.com`) returned 200 and the project.
The account was US-region.
The message said credentials; the cause was geography.

**What it exposed besides the config.** Two real defects in my own Day 9 code, both invisible until real keys failed.
Span export runs on a background thread, so the 401 was logged-and-swallowed while the run reported success - the trace this script exists to produce was silently lost.
And `get_trace_url`, a pure UI nicety, needed an API round-trip and raised after the real work had finished, taking the whole script down with it.

**Fix.** `LANGFUSE_HOST` pinned in `.env`, with the trap recorded in `.env.example` and the handoff (it returns the moment `.env` is regenerated).
`auth_check()` now runs before any work, so bad credentials fail loudly, early, and with the region hint attached; `trace_url()` never raises.
Both are regression-tested against a stub client.

**Transferable lesson.** An error that names the wrong cause costs more than one that says nothing - this is the second time in this project (story 4), and both times the fix included making the *next* failure say the right thing.
And any failure path that lives on a background thread needs one synchronous check at startup, or it will fail silently forever.

---

## 9. The coordinator refused to run my sequential demo sequentially, and it was right

**Symptom.** The first live run of the Day 13 sequential path - GitHub reads the PR, then Deployment diffs the release with GitHub's digest in its context - came back with both agents planned `parallel`, no `depends_on` edge, no handoff, and the Deployment invocation failed with a validation error whose text was "1 validation error for DeploymentAgentResponse" and nothing more.
Every offline test of the handoff was green.

**The hunt.** The plan the coordinator returned was in the run record, and its reasons were plain: Deployment's context said "currently deployed release a921f4c, previous release 9de137a".
I had put both SHAs in the situation block so Deployment would have something to diff.
Given the SHAs, Deployment needed nothing from GitHub, and the coordinator - whose graded job is to serialise only when one agent genuinely needs another's output - did not serialise.
Dynamic selection had beaten the demo author for the second time in this project (the Day 10 demo was the first).

The Deployment failure was a separate defect, and the response could not say which.
The executor's failed-invocation gap kept only the first line of the exception, and pydantic puts the rule on the second line.
The full text was on the Langfuse span: `status 'complete' is invalid when a findings Assessment.value is null`.
The model had left one judgement honestly null with a gap against it and then written `complete` on the envelope - a value the runtime could have settled itself.

**Fix.** Three, in three places.
The scenario now tells the coordinator the last recorded release and that a PR shipped since, but not the new release's identity - that is only reachable through the PR, so the dependency is real, and the coordinator planned `sequential` with a `depends_on` edge on the next run without a prompt change.
The gap keeps the whole exception text, whitespace-collapsed and capped, so the next refusal names its rule in the response.
And every agent settles `complete` over a null judgement to `partial` in the runtime, in that direction only.

**Transferable lesson.** A test of an orchestration behaviour has to present the orchestrator with a problem that actually has that shape; hand it the answer and it will, correctly, skip the work.
And when a failure message is truncated by your own code, the first thing to fix is the truncation - the second bug was diagnosable in one read once the message was whole.

## 10. The refinement loop's first live run retried the right agent, then failed on my scenario again - and the synthesis came back as XML

**Symptom.** The first live run of the Day 14 refinement loop (`check_day13_sequential.py --deploy`, the same sequential scenario as war story #9) came back `partial` after two rounds with three unresolved gaps, Deployment had produced no response, and the model-written synthesis had fallen back to the deterministic form.
Every offline test of the loop and the synthesiser was green.

**The hunt.** The run record laid the rounds out in order.
Round 0: GitHub failed its own grounding rule - the model had paraphrased an evidence excerpt, and the agent refuses a paraphrase - so Deployment, which depended on it, was not run.
Round 1: the loop consumed the executor's own failure gap (`suggested_agent: github`, the original query) and retried GitHub with the planner's block plus a refinement block naming the failure; GitHub returned a report, `partial`, with one gap pointing at Deployment: "whether the release changed configuration or images, and whether the rollout is healthy, requires `diff_release` and `check_rollout_health`."
Round 2: the loop consumed that gap and ran Deployment with GitHub's digest in its context - 4,303 characters, the planner's block first - and Deployment failed its own grounding rule: "no `check_rollout_health` reply covers `to_version`, so `health_signals` are unknown; the report must carry a gap."
The cap is two, so the run ended there.

So the loop had done exactly what it was built to do, twice, and both agents had refused their own reports for honest reasons.
The second refusal was mine.
GitHub's digest said the merge commit on main was "the identity of the release now running on checkout-api", which is what shipping a merged PR means.
The check had deployed the demo at the PR's *head* commit, because that is what the Day 13 version of the script resolved.
Deployment diffed and health-checked the merge commit GitHub named, the health tool honestly reported that version was not what was running, and the agent's stamping rule refused a report whose health signals nothing covered.
The agents reasoned correctly about a world the demo author had set up inconsistently - war story #9 with the roles reversed.

The synthesis fallback was a third thing.
The executor's reasoning field carried the reason: the model had written the nested `answer: Assessment[str]` argument as XML-style parameter text inside a string, and pydantic refused a string where an object was required.
The prompt is full of `<handoff>` blocks and `<query>` tags; the model, primed by them, wrote the one nested tool argument the way it writes tool calls.

**Fix.** Three, and only one of them touched the loop, which did not need fixing.
The check now resolves the release under test to the PR's merge commit, found in the local clone's history, and the situation block says "main as it stood after that merge has been rolled out".
Not from a new `merge_commit_sha` field on `get_pull_request` - that was the second attempt, and it failed in a third way: handed the SHA in the PR reply, the model reported the merge commit as a commit it had never fetched with `list_commits`, and the agent's own grounding rule refused it twice (232.7k input tokens to learn that a convenience field is a trap when the consumer must ground every fact in a fetch).
The tool reply stays as it was; the agent finds the merge commit the way it did on Day 13.
The synthesis tool's schema is flat - `synthesis`, `answer`, `confidence`, `evidence`, `reasoning` as top-level scalars, the `Assessment` assembled by the runtime - so there is no nested object to mis-serialise; a one-call re-run over the recorded responses came back grounded, six real evidence ids, 0.55.
And the check evaluates the invocation that stands for each agent - the last one that produced a response - rather than the plan's, since a refinement round may have replaced it.

**Transferable lesson.** When a loop's first live run ends badly, read the rounds before touching the loop: here every re-delegation was the right one, and both failures were the agents being honest about inputs the scenario had made inconsistent.
And a structured-output schema should be as flat as the data allows; a model that has just read a page of XML-shaped context is one nesting level away from answering in it.

---

## 11. The coordinator skipped the Docs agent, and the reason it gave was a sentence I had written

**Symptom.** The first run of all four agents on one query (`check_day15_integration.py --deploy`) failed its check on "the plan did not select docs", and the model-written synthesis had fallen back to the deterministic form.
The query had a part that was plainly the Docs agent's: "what do our past incidents say about handling this kind of failure?"
Every offline test was green, and coordinator selection had been measured at 5/5.

**The hunt.** The plan's `skipped_agents` entry gave the reason in full: the past-incidents question "is served by the incident agent's access to the historical incident corpus, not the runbook/docs corpus."
That sounded like a model inventing a capability, so I went looking for where the Incident agent touches the corpus.
It does not.
It has no tools at all; it reasons over the context block it is handed, and its own schema guidance says `similar_incidents` stays empty "unless the context actually names prior incidents".
The corpus belongs to the Docs agent, and it is a corpus of past incidents, not of runbooks.
Then I read the planner's roster, the one block of text the coordinator has about its agents.
`incident`: "Reads Prometheus and the historical incident corpus."
`docs`: "answers from the retrieved runbook and documentation corpus."
Both sentences dated from Day 6 (2026-07-29) - before the Docs agent or its corpus existed, and when the Incident agent's corpus access was still a plan - and described the intention rather than the build.
The comment above the roster says keeping it in one place "is what stops the prompt from claiming a capability the agent does not have."
The coordinator had reasoned correctly from a description that was wrong, and said so in writing.
The five-case selection check never caught it because none of its cases asks for precedent alongside a live diagnosis.

The refinement loop had quietly repaired the plan: the Incident agent raised a gap ("no historical incident corpus data was provided") pointing at Docs, and round 1 ran Docs.
That is the loop working, and it is also how a wrong roster could have stayed hidden for good - at the price of a round.

The synthesis fallback was a separate defect with the same shape.
The rejection read: "answer cites evidence id(s) ['doc_0005', 'doc_0009', 'doc_0017'] that no agent response carries."
Correct by the contract: the coordinator cites evidence ids, and those are document ids.
But the model had not invented them; it had copied them from the Docs digest, where every claim line read `claim_x @0.85 [doc_0005]` - document ids in the bracket slot that every other digest line uses for evidence ids.
And the real evidence list, the last section of the digest, had been cut by the 4,000-character ceiling after two of its eleven entries.
The synthesiser cited the only ids it could see, in the slot where ids go.
This was the first synthesis over a Docs report; Day 14's had GitHub and Deployment only, and neither is long enough to reach the ceiling.

**Fix.** The roster now says what the agents are: Incident reasons over the observations it is handed and has no tools; Docs answers from the corpus of past incidents and is the agent for precedent.
A test pins both sentences, because the roster is a capability claim and nothing else was checking it.
The digest keeps its evidence list out of the cut - the body gives up the room, the ceiling still holds - and Docs claim lines say `docs=doc_0005`, so brackets mean evidence ids everywhere.
Before paying for another full run, one call replayed the synthesis over the recorded reports: grounded, 0.62, 25 real evidence ids across all four agents.
The second full run passed: Incident, Docs, and GitHub overlapping from 0.0 s, Deployment after GitHub with its digest, a model-written answer at 0.60 that told the on-call their suspect release was not the cause.
A third complaint in the first run was the check's own bug: it took "the planner's block" from the last invocation that answered, so an agent re-delegated in two rounds had its round-1 context compared with round 2's.

**Transferable lesson.** A model that gives a wrong reason in plain words is handing you a grep string; the false sentence was mine, in the prompt, nearly verbatim.
Descriptions of a system written before the system exists go stale silently, and a prompt is a place where stale documentation executes.
And when a bounded summary has to lose something, decide what it may not lose: the ids a reader is allowed to cite are the one part of a digest that everything downstream validates against.

---

## 12. The eval harness's first live run scored five failures, and none of them was the agents'

**Symptom.** The first live run of the Day 19 eval harness finished in two seconds and reported `Answered: 0/5`.
Every item had failed, each with its own row in the report, its own `failed` mark, and its own entry under "what was refused or ungrounded".
The accuracy table was a column of `n/a`.

**What it was.** `401 API key is invalid`, five times.
The key in `.env` had been revoked since the last live run; a free `models.list` call was refused the same way, no shell variable shadowed it, and its hash was the hash `LLMSettings` loaded.
The harness had done exactly what I had designed it to do with an agent that raises: score the item as failed, keep its reason, carry on.
It had no way to tell an agent that gave up on its report from an environment in which no agent could have done anything.

**Why it matters more in an eval than anywhere else.** A check that fails on a bad key fails, and you look.
An eval that records a bad key as thirty-eight wrong answers produces a report, with a score in it, and the score is zero.
Committed as a baseline, that number would have been compared against every later run.

**Fix.** A refused credential aborts the run on the first item (`EvalAborted`), with one message that names the item it stopped at, says how many were scored before it, and points at the HANDOFF entry for replacing the key.
The run is recorded as an error with no item blamed.
An agent that raised is still a scored item - that is a result of the thing under test, and the two are now different types.

**Transferable lesson.** Before counting failures, ask whose they are.
A harness that turns every exception into a data point will faithfully measure your environment and label it your model.

---

## 13. The fix for the last failure was a list, and the next failure was not on it

**Symptom.** With the key rotated, the Day 20 checkpoint passed its smoke test, scored five items of its first full run, and then printed `FAILED` thirty-three times in under a minute.
It moved on to the second run and started failing that one too.

**What it was.** `400 Your credit balance is too low to access the Anthropic API`.
The account had less than a dollar in it.
The day before, war story #12 had taught the harness to stop on a refused credential, and I had taught it with a tuple of two exception classes: `AuthenticationError` and `PermissionDeniedError`.
An empty balance arrives as a `BadRequestError`, so the harness did what it does with anything not on the list: scored it as the agent's failure and carried on.
I had fixed the instance and called it the rule.

**What it cost.** The refused calls were free.
What was not free was the run I had stopped by hand twenty minutes earlier to restart it detached: six answered items, about 27 cents, kept nowhere, because a run held its results in memory until it finished.
That is a small number and it was the whole lesson.
Every item is one call that was already paid for the moment it returned.

**Fix.** The question is whose failure it was, and that has an answer that does not need a list: if the API client raised it, the call failed, not the agent.
A refusal about the caller stops the run at once; anything else the API fails on is recorded and tolerated until three in a row.
Every item is written to disk as it is scored, and `--resume` continues a stopped run, asking only for what it did not finish.
The five answers from the interrupted run and the four from the smoke test are the first nine items of the baseline.

**Transferable lesson.** When a failure surprises you twice, the first fix was a special case.
Ask what the two have in common, and test for that.
And find out what your process holds in memory that somebody has already paid for.

## 14. A thousand tests passed on a conversation the API refuses

**Symptom.** Day 21's first four-agent run passed its checkpoint, and one line of its synthesis read: `Invocation inv_4bdd2f64 (deployment) failed with BadRequestError: messages.8: tool_use ids were found without tool_result blocks immediately after`.
It was the round-1 re-delegation, 152 seconds of work, and every token of it paid for.

**What it was.** The harness, not the model.
`run_tool_loop` returns when a round stops for any reason other than `tool_use`.
One reason is `max_tokens`, and a round can hit it part-way through writing a tool call.
The loop handed back a conversation whose last assistant turn held a cut-off `tool_use` that nothing had answered, the agent appended its "now emit your report" turn to it, and the API refused the request as malformed.
The truncation guard the agents already had was on the forced emit, one step later, and never got to run.

**Why nothing caught it.** The suite has eleven scripted fake clients, and every one of them returned its next scripted reply whatever it was sent.
A fake that accepts any conversation tests what the harness does with replies and nothing about what it sends.
Over a thousand tests exercised the loop, and none of them could have failed on this.

**Fix.** The fake first, then the code.
`tests/wire.py` states the rule the 400 named - every `tool_use` answered in the next message, every `tool_result` answering the turn before - and every fake that stands in for `messages.create` checks it on each request.
The reproduction then failed with the API's own sentence, and the other 25 Deployment tests passed under the stricter fake.
The loop now answers a cut-off call with an error `tool_result` saying it was not run, and never runs it: its input is whatever was written before the cut.

**Transferable lesson.** A test double encodes a belief about its counterparty.
If it accepts everything, the belief is that the counterparty accepts everything, and that is never true of an API.
When a live run fails on a rule, put the rule in the fake before you fix the code, so the whole suite starts checking it rather than one new test.

---

## How to tell these in an interview

Lead with the symptom, not the answer.
"A required field was missing from the model's output" invites the interviewer to guess along with you, and the reveal - it was truncated at exactly 4096 tokens - lands.
Opening with "I had a max_tokens misconfiguration" throws the story away.

Have one sentence ready for what you changed *besides* the fix.
Every story above has one: the truncation check, the import-time drift guard, the `--repeat` flag, the skip-with-diagnosis, the query idiom, the comment explaining a deliberate absence, the fail-fast auth check, the gap that keeps the whole error, the test that pins the roster's capability claims, the run that stops on a refused credential instead of scoring it, the progress file that keeps what a stopped run paid for.
Fixing the bug is table stakes. Making the failure legible next time is the part that reads as seniority.
