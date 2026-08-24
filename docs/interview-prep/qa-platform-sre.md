# Q&A - platform / SRE / DevOps

The platform half of the project: observability, chaos engineering, MCP tool design, Postgres, Docker.
Every number is in [`numbers.md`](numbers.md).

---

## Chaos engineering

**Q: Walk me through your chaos setup.**

Three containerised services with a fan-out topology - checkout-api calls payments-api and inventory-api and returns 502 if either fails - scraped by Prometheus every 5 seconds. Each service exposes a `/_chaos` endpoint with three knobs: `extra_latency_ms`, `error_rate`, `leak_mb_per_request`, all healthy at zero.

Four named failure modes are *compositions* of those knobs against specific services, each with a distinct `(service, knob)` fingerprint:

| Mode | Target | Observable |
|---|---|---|
| `downstream_latency` | payments-api latency | 8/8 succeed, mean **812ms** |
| `code_regression` | checkout-api errors | **500s** - fails itself |
| `bad_config_deploy` | payments-api errors | **502s** - fault is downstream |
| `resource_exhaustion` | checkout-api leak | RSS **273 -> 599 MB** / 30 requests |

**Q: Why those four, and why does the split matter?**

They map 1:1 to a `FailureMode` enum in the frozen contract, which is what lets the eval score agent output against injected truth. The mapping is *structural*, not documented - the injector imports the enum and raises at import if a mode is missing, so the two cannot drift.

The split that matters is `code_regression` versus `bad_config_deploy`. Both are "error rate up", but one has checkout-api failing itself (**500**) and the other has it returning **502** because a downstream failed. That is exactly the attribution call an incident agent must make, and it is why the topology line is in the agent's context - without it, a 502 rate reads as checkout-api being broken.

**Q: How do you know the injection actually worked?**

Three ways, and I would not trust fewer.

The injector reads each knob back after setting it and raises if the value did not take. It then probes the front service to show user-visible impact. And the app publishes every knob as a `chaos_knob_value` gauge, so the injected state is verifiable from Prometheus independently of anything the script believes.

That third one is load-bearing. The Day 5 check recovers the injected mode *from the gauges* rather than from the injector's return value, because what is in Prometheus is what actually happened to the app - a stronger claim than what a script requested.

**Q: What about reversibility?**

A failure mode that needs a stack rebuild to clear will not survive a live demo, so every mode clears with a single `--reset`. Verified: latency back to 9ms, all 200s, and RSS drops **599 -> 53 MB** - the app frees the leaked ballast rather than just stopping new leaks.

I deliberately did *not* add a container memory limit to make the leak OOM-kill. An OOM restart clears the in-memory knob, so the fault would fix itself mid-demo - a self-healing chaos mode is worse than no chaos mode.

---

## Observability

**Q: How does the agent get metrics?**

A thin PromQL client plus a fixed query battery - error ratio, request rate, p50/p99 from the histogram, RSS, CPU, `up` - rendered into a text context block. Deliberately not a general-purpose Prometheus client: it returns floats keyed by label, because what the caller wants is "the p99 for payments-api", not a result envelope.

One wrinkle worth knowing: `prometheus_client`'s process collectors carry `instance` (`checkout-api:8000`), not the app's custom `service` label. Rather than adding relabeling to the Prometheus config, the battery keys those queries on `instance` and maps back - keeps the scrape config simple.

**Q: You mentioned a bug in that. What was it?**

The best question about this code, because nothing failed.

The error-ratio query divided 5xx rate by total rate. For a service with traffic and *no* errors, the numerator matches no series - so the division returns nothing, and my formatter rendered absent as "not measured." I was telling the agent nobody had looked, when the truth was zero errors.

That is precisely the distinction the project is built on: `null` means not determined, `0` means looked and found nothing, and an agent must emit a `Gap` for the former. I enforced that in the schemas and then broke it in the data going in.

Fix is the PromQL idiom `... or <denominator> * 0`, which supplies an explicit zero for any service with traffic while leaving a genuinely unscraped service absent. Verified live: 0 series before, 3 reading `0` after.

And the impact was invisible: the agent had hedged to 0.55 confidence partly because it could not see error rates. Feeding it "unknown" instead of "zero" makes it *correctly* less certain, so a defect degraded output quality with no error anywhere.

**Q: Does `make verify` just check the containers are up?**

No, and that distinction is the point of the target. A container reporting healthy while the thing it provides is missing is the exact failure it exists to catch - a plain `postgres` image starts perfectly happily and then fails much later inside retrieval code, which reads as a bug in the ingestion pipeline rather than an image choice.

So `verify` asserts the `vector` extension is installed, Redis answers, all three services expose metrics, Prometheus is actually scraping them, and - since the corpus landed - that the incident corpus is present, is 15-20 rows, and covers all five failure modes. That last one is there because a mode with zero rows cannot be scored by the eval at all.

**Q: How is tracing wired, and why is it opt-in?**

One Langfuse trace per coordinator request: a `plan` span carrying the planning call's own tokens, one `agent:<name>` span per invocation, tool calls as child events, and the trace id on the contract response.
The executor talks to three small protocols (`Tracer` / `RequestTrace` / `AgentSpan`); the default implementation everywhere is a null object, and only entry points that explicitly pass `default_tracer()` ever emit a span.

Opt-in is the design decision worth defending.
The 323-test offline suite must make zero network calls, and it must keep that property on a machine whose `.env` carries real Langfuse keys - a tracer that self-activated from the environment would break that silently the day the keys landed.
Same shape as retrieval's honest degradation: configuration decides *which* tracer, but the entry point decides *whether*.

Two implementation details that earn their keep: agent spans open and close in the worker thread that runs the agent, so span timing is real wall clock and a parallel plan shows visibly overlapping spans (that overlap was the Day 9 checkpoint artifact); and the adapter's `auth_check()` runs before any work in the live scripts, because span export happens on a background thread where a 401 is otherwise logged-and-swallowed while the run reports success (war story #8 - the keys were valid, the account was US-region, and the default EU host called it a credentials error).

---

## MCP tool design

**Q: What makes a good tool description?**

The contract mandates a four-part template, in order: (1) what it does plus input formats, (2) at least three example queries, (3) edge cases and limits, (4) when to use this versus the named alternative.

Part 4 is the one that matters, and it is a measured intervention rather than a style preference - there is a planned case study that runs 20 queries against two deliberately overlapping tools, records the misrouting rate, adds part 4, and re-runs. The template is the treatment.

There is a test asserting all four parts are present *and in order*, and another asserting part 4 names `correlate_events` explicitly. A description that silently loses part 4 would invalidate the case study's baseline, so it is worth a test rather than a review comment.

**Q: Talk me through your error taxonomy.**

Four classes, and the useful property is that exactly one is retryable:

| Class | Means | Retryable |
|---|---|---|
| `transient` | Upstream unavailable, timeout, rate limit | **yes**, with a required `retry_after_ms` |
| `validation` | Caller's input is malformed | no - must include `details.field` and `details.expected` |
| `business` | Well-formed but cannot be satisfied | no - must offer an alternative |
| `permission` | Missing scope | no - must name `details.required_scope` |

The point is that retrying a validation, business, or permission error will fail *identically*. The agent must change the request, escalate, or record a `Gap` with `resolvable: false`. Collapsing these into "error" is what produces agents that retry the same impossible call five times.

The per-class requirements are asserted at construction, not trusted - a transient error with no retry delay invites a hot retry loop against an upstream that is already struggling, so building one raises.

**Q: What is the most important thing you did in that envelope?**

Making `isError` and `ok` always agree, and never encoding a failure as prose inside a success payload. That is the failure mode the taxonomy exists to prevent: an agent cannot retry, escalate, or record a gap against an error it cannot see.

That drove a real implementation decision. The MCP library sets `isError` by catching exceptions, which turns your structured error into a plain-text message. Returning a `CallToolResult` directly is what lets the tool set `isError` itself while keeping the payload - which required reading the library's source rather than its docs.

**Q: And an empty result?**

A success. `ok: true`, empty collection, `meta.returned: 0`. A `business` error means the request *cannot be computed*, not that it computed to nothing.

The tool distinguishes them explicitly: no events for a known service is an empty success; no events for a service that does not exist is `UNKNOWN_SERVICE`. Conflating those tells the agent nothing happened when the truth is it asked the wrong question - and it cannot tell those apart unless the tool says so. The description spells out that an empty result is "NOT an error and NOT evidence that nothing happened, because only recorded events are visible here."

**Q: Why can't the tool server import your contract models?**

Because the MCP boundary is JSON Schema, not Pydantic. A tool server importing the reasoning layer's models cannot be deployed independently, and a Pydantic-only refactor could break a tool. So enum members and input schemas are written out longhand in each server.

The duplication is affordable only because it cannot silently drift: a test asserts the longhand copies still match the Python enums. It parses the module's *import statements* with `ast` rather than grepping - I wrote the naive substring version first and it failed on the docstring explaining the rule.

---

## Postgres and data

**Q: Why is the schema in `docker-entrypoint-initdb.d` rather than a migration tool?**

Because the volume was empty, so a destructive reset cost nothing, and a migration tool on day five is scaffolding that delays the checkpoint.

The limitation that actually matters is not the one you would guess: `init/` runs **only** on first initialisation of an empty local volume. It never runs against hosted Postgres. So the deployment day has to apply these files by hand - every file is written to be runnable standalone, the seed is idempotent, and the deploy plan carries the commands.

The trigger for adopting a real migration tool is specific: the moment a schema change must survive an already-populated database. `init/` cannot express that at all, and the alternative is a destructive reseed that costs the corpus.

**Q: Why CHECK constraints instead of Postgres ENUM types?**

Because adding an enum member is a known-future event under the contract's change process. `ALTER TYPE ... ADD VALUE` cannot run inside a transaction on older Postgres and cannot be reverted; `ALTER TABLE ... DROP/ADD CONSTRAINT` is one reviewable statement either way. Optimise for the change you know is coming.

It also lets the contract's `other`-plus-detail pairing be a `CHECK`, so a bad seed row is rejected at insert rather than surfacing later inside a Pydantic validator where it looks like an agent bug.

**Q: Tell me about a hard bug in the infrastructure.**

Integration tests failing with `password authentication failed for user "aioc"`. Every credential checked out - `DATABASE_URL` set, container password the right length. Since I could not read `.env` (settings deny it, correctly), I compared SHA-256 prefixes of every copy of the password: the one pydantic-settings loaded, the one inside `DATABASE_URL`, the compose default, the container's environment.

All four identical. Right password everywhere, authentication still failing.

That is when I stopped verifying inputs and asked what was *answering*. `netstat -ano | grep :5432` showed **two** processes LISTENING: Docker's proxy and a native Windows PostgreSQL service. Whichever won the race got the connection, and the native one has no `aioc` role.

The irony is that the project's own `.env.example` warns a port collision is the most common day-one failure. I wrote that and still lost time, because the error pointed at credentials.

Fix: republish on a free port - a container recreate, so the named volume and corpus survived - and make the integration tests **skip with the diagnosis**, checking order included, so the next person reads it instead of finding it.

**Q: What is the generalisable lesson there?**

When every input to a check is provably identical and the check still fails, the problem is not the inputs - question what is on the other end. And comparing hashes is a clean way to prove two secrets are equal without either reaching a log.

---

## Configuration and workflow

**Q: How is Claude Code configured for this repo, and why that way?**

Path-scoped rules under `.claude/rules/` keyed on globs, so per-area conventions load only when a file they cover is open - the contract rules cost nothing on a session that only touches the demo app. A three-level `CLAUDE.md` hierarchy (user, project, directory). Two slash commands, and a read-only skill that audits the Pydantic layer against the contract for drift.

The split I would defend: **rules are context, `settings.json` is enforcement.** A preference goes in a rule; a boundary goes in `permissions`. The permission layer allows the inner dev loop without prompting, sends the three destructive commands (`db-reset`, `compose down -v`, `git push`) to `ask`, and denies reads of `.env`, `secrets/**`, `*.pem`, `*.key`.

That deny rule earned its place during the password hunt above - it is why I compared hashes instead of just reading the file, which is the better habit anyway.

**Q: How do you keep the test suite from becoming expensive?**

It makes **zero** API calls. 186 tests, ~1.5 seconds offline, everything model-facing driven by scripted fake clients. Live verification is four separate opt-in scripts, and the model-matrix one defaults to a single call per model.

A suite that costs money per run stops being run, and a suite that is not run is not a suite.

The tradeoff to state honestly: fakes prove plumbing, never model behaviour. Every fake-driven test could pass while the live agent fails - which is what happened, when three models failed a path that had 14 green offline tests. The fakes were answering a different question. Offline proves I did not break the wiring; live proves the model can do the task.
