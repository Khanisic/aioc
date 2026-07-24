---
name: contract-audit
description: Audit the Pydantic schema layer against docs/CONTRACTS.md and report any drift between the frozen contract and its implementation. Read-only; runs in an isolated context.
context: fork
agent: Explore
background: false
allowed-tools: Read, Grep, Glob, Bash(uv run:*)
---
Audit `src/aioc/contracts/` against the frozen contract in `docs/CONTRACTS.md` (schema `1.0.0`) and
report drift. You run in a forked, read-only context - do not edit files.

Work section by section through CONTRACTS.md sec 2-7 and check:

1. Every frozen type has a corresponding model, with matching field names, types, and nullability.
2. Every enum has all its members, including `other`.
3. Every *validated* invariant is actually enforced by a validator (not merely documented):
   the `other`+detail pairing; confidence below 0.25 -> value `null`; evidence-id resolution;
   the status/nullness rule; the `RecommendedAction` approval rule; the `Claim` and `Coverage`
   invariants; `context_passed` non-empty; `depends_on` vs `mode`; the tool error taxonomy.
4. Every model forbids extra fields (subclasses `StrictModel`).
5. Run `uv run pytest -q` and confirm the worked-example anchor test passes.

Report:
- (a) a table: contract section -> implementing symbol -> status (ok / missing / drifted);
- (b) any invariant that is documented but not enforced;
- (c) the test result.

Prioritize correctness. If you are unsure whether something is enforced, say so rather than guess.
