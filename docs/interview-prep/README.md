# Interview prep

Material for talking about this project in interviews.
Weighted toward AI/LLM engineering, with the platform and SRE angles covered second.

| File | Use it for |
|---|---|
| [`war-stories.md`](war-stories.md) | Concrete debugging narratives. The "tell me about a time when..." answers |
| [`decisions.md`](decisions.md) | Architecture decisions, the reasoning, and what I would do differently |
| [`numbers.md`](numbers.md) | Every measured figure, with where it came from. Do not quote a number that is not here |
| [`qa-ai-engineering.md`](qa-ai-engineering.md) | Q&A: agents, structured output, retrieval and grounding, prompting, evals, context |
| [`qa-platform-sre.md`](qa-platform-sre.md) | Q&A: observability, chaos, MCP tool design, Postgres, Docker |

## Two rules for using this

**Never quote a number that is not in `numbers.md`.**
Every figure there is traceable to a recorded run under `test-results/` or to a command you can re-run.
An interviewer who asks "how did you measure that?" is asking a fair question, and "I ran it three times and recorded the failures as JSON" is a much better answer than a remembered approximation.

**Lead with the failure, not the architecture.**
The strongest material here is not the diagram - it is the repeated occasions where something looked like a model problem (or a credentials problem) and turned out to be an engineering problem.
Interviewers have heard a hundred descriptions of a multi-agent system. They have heard far fewer people say "the model was right and my token budget was wrong, and here is how I found out."

## What this project actually is

An AI operations centre: a coordinator routes an operational question to four specialist subagents (Incident, Docs, GitHub, Deployment), each returning schema-validated, confidence-scored output through custom MCP tools.
Built as portfolio evidence for the Claude Certified Architect - Foundations credential, so every major decision maps to a graded domain rather than to taste.

Two layers meet at a JSON wire boundary:

- **Reasoning layer** - coordinator, subagents, output schemas, evals. Pydantic v2 is normative.
- **Platform layer** - MCP tool servers, Claude Code config, demo environment, observability. JSON Schema is normative.

The boundary is the point. It is what let the two halves be built independently, and it is the answer to "how would you split this across a team?"

## Status when this was written

End of Day 11 of a 30-day plan (the calendar is roughly double the day count - one engineer owns both layers since Day 6).
Live: the frozen contract as executable models, the Claude API harness, three of the four agents (Incident, Docs, GitHub - each schema-validated, the latter two grounded in code), the coordinator planning *and* executing (dynamic selection, explicit context passing, real parallelism), hybrid retrieval over the 18-incident corpus, Langfuse tracing on every request, three MCP tool servers on the real wire, the demo app with four injectable failure modes, and an end-to-end demo proven live twice with the GIF captured.
Not yet built: the Deployment agent, the refinement loop and model-written synthesis, the routing case study, evals, CI.

Be straightforward about that in an interview.
"Eleven days into a thirty-day plan, and here is what is proven and how" is credible.
Implying the whole thing is finished is not, and the repo makes the timeline obvious to anyone who looks.
