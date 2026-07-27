# Day 1 - Understanding the Codebase

A first-pass, plain-English walkthrough of what AIOC is and how it is put together.
Written for someone seeing the repository for the first time.
Read it top to bottom; it goes from the big picture down to real code.

---

## What this project is

The name is **AIOC = Enterprise AI Operations Center**.
Ignore the fancy name; here is the real idea.

Imagine a company's software is on fire.
Something broke in production.
A human engineer would normally have to:

1. Look at the **incident** (what broke, when, how bad).
2. Search the **docs / runbooks** (has this happened before, what is the fix).
3. Check **GitHub** (which code change or pull request caused this).
4. Check the **deployment** (what got released, is the rollout healthy).

This project builds an **AI system that does all four automatically**.
You type a question like *"why did checkout start throwing 500 errors at 2pm?"* and the system figures out the answer.

The clever part is the **structure**, not the AI magic.

---

## The big picture (high level)

Think of it like a company org chart.

```
                    YOU (ask a question)
                          |
                          v
                  +---------------+
                  |  COORDINATOR  |  <- the "manager"
                  +---------------+
                          |  decides WHO to ask
        +-------------+---+------+-------------+
        v             v          v             v
   +---------+  +---------+ +---------+  +-----------+
   |Incident |  |  Docs   | | GitHub  |  |Deployment |  <- 4 "specialist employees"
   |  agent  |  |  agent  | |  agent  |  |  agent    |
   +---------+  +---------+ +---------+  +-----------+
        |             |          |             |
        +-------------+----------+-------------+
                          |  each uses TOOLS to get real data
                          v
   +--------------------------------------------------+
   |  TOOLS: get_incident_timeline, diff_release, ...  |
   |  which read from Postgres / Redis / logs          |
   +--------------------------------------------------+
```

**The coordinator is the manager.**
Its whole job:

- **Pick only the right specialists** for the question.
  If you ask a pure "what does this error mean" question, it should not wake up the Deployment agent.
  It even keeps a written list of who it skipped and why (that is real, in the code: `skipped_agents`).
- **Run them smartly.**
  Incident and Docs do not depend on each other, so run them at the same time (parallel).
  But GitHub then Deployment is a chain (first find the pull request, then diff the release), so run them one after another (sequential).
- **Double-check its own work.**
  After collecting answers, it asks "is anything still missing?".
  If yes, it goes back and asks a specialist a more targeted question.
  This is the **refinement loop**.

Each **agent** (specialist) is an AI that reads real data using **tools**, then hands back a clean, structured report.

That is the whole system at the top level: a manager routing questions to four specialists who use tools.
Everything else in this repo exists to make that reliable.

---

## Why is this built at all (the honest answer)

This is **portfolio evidence** for a certification called **CCA-F** (Claude Certified Architect - Foundations).
The whole thing is designed so that each piece proves a specific skill the exam grades.
See `docs/BUILD_PLAN.md`; every decision maps to an exam "domain".
So it is a real working system, but also deliberately a showcase.
Good to know, because it explains why some things are more careful and documented than a normal side project.

---

## Where the project is right now

Very important, so you do not go looking for code that does not exist yet.
The repository is at the **end of Day 1**.

Practically that means:

- The **contract** is built (explained next; it is the most important part and the only real code so far).
- The four agents, the coordinator, and the tools are **empty folders** (`__init__.py` files with nothing in them).
  They are placeholders waiting for future phases.

So do not be confused when you open `src/aioc/agents/` and it is empty.
That is expected.

---

## The one concept you must understand: "the contract"

This is roughly 90% of the actual code, and it is the heart of the design.

### The problem it solves

Two people are building this.
Engineer A does the AI / "reasoning" side; Engineer B does the "tools / platform" side.
They work independently.
But their code has to talk to each other by passing **JSON** back and forth.

If Engineer A thinks a field is called `error_rate` and Engineer B calls it `errorRate`, everything silently breaks.
So they wrote down an exact, frozen agreement of what every message must look like.
That agreement is:

- `docs/CONTRACTS.md` - the human-readable rulebook (the "law").
- `src/aioc/contracts/` - the same rules written as runnable Python code that enforces itself.

"Frozen" means nobody is allowed to casually change it.
Changing it requires a formal process (written agreement plus a version bump).
Why so strict?
Because it is the one shared thing both halves depend on; change it carelessly and you break the other person's work.

### How the contract enforces itself (low level)

They use a library called **Pydantic** (very common in Python).
Pydantic lets you define the shape of data as a class, and it automatically rejects data that does not fit.

Look at `src/aioc/contracts/primitives.py`, the `Assessment` class.
This is the single most important building block.
The idea in plain English:

Whenever the AI judges or concludes something (not a plain fact, a judgment), it cannot just state it.
It must wrap it in an `Assessment` that carries:

- `value` - the conclusion (e.g. "the failure was a memory leak").
- `confidence` - a number 0 to 1, how sure it is.
- `evidence` - pointers to the actual data that backs this up.
- `reasoning` - why.

And there are hard rules baked in:

> If confidence is below 0.25, `value` must be `null`.
> In other words: if you are not sure, you are not allowed to guess.
> You must say "I do not know" instead of making something up.

That is an anti-hallucination rule, enforced by code.
If the AI tries to output a low-confidence guess, Pydantic literally throws an error and rejects it.

### The `null` vs `[]` rule (this one trips up everyone)

- `null` = "I never found this out / the data was missing."
- `[]` (empty list) = "I looked, and there was genuinely nothing."

These mean different things and you are not allowed to mix them up.
And here is the kicker: if a field is `null` because data was missing, the response must include a matching `Gap` explaining what is missing.
The envelope code walks through every assessment, and if one is `null` without a corresponding `Gap`, it rejects the whole response.

A `Gap` is basically the AI saying "here is a hole in my knowledge, and here is which agent could fill it and what to ask them."
That is literally what feeds the coordinator's refinement loop.

### The "envelope" idea

All four agents return their answer in the same outer wrapper, called `AgentResponse`.
Only the `findings` part inside differs per agent.
Think of it like this: every specialist submits their report on the same company letterhead (status, summary, evidence, gaps, confidence, cost), and only the body of the report changes.

Look at `IncidentFindings` in `src/aioc/contracts/incident.py` for what the Incident specialist's report body contains:
an incident window, affected services, a `severity` assessment, a `root_cause` assessment, a `timeline`, `recommended_actions`, and more.
It even validates that the timeline is sorted by time.
That is the level of strictness here.

### The coordinator's proof-of-good-behavior

Look at the `AgentInvocation` class in `src/aioc/contracts/coordinator.py`.
Remember that each agent must get its context explicitly, no assuming?
The code enforces it:

> `context_passed` must be non-empty.
> An empty value means context was assumed inherited, the exact failure this project demonstrates the absence of.

So the design principle ("always pass context explicitly") is turned into a rule the code refuses to violate.
Same for parallel vs sequential: if you say "parallel" but list dependencies, rejected.
If you say "sequential" but list no dependencies, rejected.

**The pattern to take away:** in this codebase, rules are enforced by validators, not by hoping people read comments.
If the CLAUDE.md says "do X", there is usually a line of code that makes it impossible to not-do-X.

---

## The other stuff around the edges

**`docker-compose.yml`** brings up two databases locally with one command:

- **Postgres** (with `pgvector`) - the long-term memory plus searchable knowledge base.
  `pgvector` lets you search by meaning ("find incidents similar to this one"), not just exact keywords.
  That is the "RAG corpus".
- **Redis** - short-term "working memory", fast and temporary.

The comments in that file are worth reading; they explain why every choice was made (for example, why they pin exact version numbers instead of `:latest`).

**`Makefile`** gives shortcuts so you do not memorize long commands.
`make up` starts the databases, `make test` runs the tests, `make chaos-<mode>` deliberately breaks things (fake a memory leak, a bad deploy, and so on) so the AI has real disasters to practice on.
Those chaos modes map 1:1 to a `FailureMode` enum, so they can check whether the AI correctly diagnosed the disaster that was secretly injected.

**`pyproject.toml`** is the project's ID card plus dependency list.
It uses `uv` (a fast modern Python package manager) and is pinned to Python 3.12 on purpose so everyone runs the identical version.

**`tests/test_contract.py`** proves the Python models actually match the written contract, with one test per rule.

**The `.claude/` folder** is config for Claude Code itself (the AI coding assistant).
It has rules that fire when you edit certain files, plus slash commands like `/validate-schema` and `/contract`.
This is part of the certification story too (Domain 3: configure your AI tooling like a team lead).

---

## If you remember only 5 things

1. It is a manager (coordinator) routing questions to 4 AI specialists (agents) that read real data through tools.
2. The project is at Day 1; only the "contract" layer is actually built.
   Agents, coordinator, and tools are empty stubs waiting for later phases.
3. The "contract" is a frozen, self-enforcing agreement (Pydantic models in `src/aioc/contracts/`) so two engineers can build two halves independently and have them fit perfectly.
4. The golden rule: never guess.
   Low confidence means output `null` plus a `Gap`, not a made-up answer.
   This is enforced by code, not politeness.
5. `null` is not `[]`.
   "Missing" and "found nothing" are different, and the code will reject you for confusing them.

Suggested reading order:
`CLAUDE.md`, then `docs/BUILD_PLAN.md`, then `src/aioc/contracts/primitives.py`, then `src/aioc/contracts/incident.py`.
That path takes you from the "why" all the way down to real code.
