# Design note - the Claude Code configuration layer (Domain 3, configuration half)

Day 2, Engineer B.
Companion to `BUILD_PLAN.md` Phase 0 and `EXECUTION_PLAN.md` Day 2.

The configuration layer is built first because it governs how every later phase gets written.
This note records what is in it, why each piece sits at the layer it does, and how to verify it fires.

---

## What the layer is

Five mechanisms, each chosen for a different reason.
The distinction that matters: **settings are enforced by the client, everything else is context that shapes behaviour.**
Instructions in a `CLAUDE.md` or a rule influence what Claude tries to do; only `permissions` in `settings.json` decides what it is allowed to do.

| Mechanism | File(s) | Loads | Governs |
|---|---|---|---|
| Project instructions | `CLAUDE.md` | every session, in full | architecture, the frozen-contract rule, schema conventions, commands, house style |
| Directory instructions | `src/aioc/CLAUDE.md` | when a file under `src/aioc/` is read | which package owns what, and who imports `contracts/` |
| Path-scoped rules | `.claude/rules/*.md` | when a matching file is read | the per-area rules that would bloat the root file |
| Slash commands | `.claude/commands/*.md` | on invocation | two repeated repo tasks |
| Project skill | `.claude/skills/contract-audit/` | on invocation, forked context | the read-only contract drift audit |
| Permissions | `.claude/settings.json` | every session, enforced | what may run without asking, what must ask, what is never readable |

---

## Why rules rather than one large CLAUDE.md

The root `CLAUDE.md` is 119 lines against a documented target of under 200.
Every area-specific rule pushed into it would be paid for in tokens on every session, including sessions that never touch that area, and longer files measurably reduce adherence.

Path-scoped rules invert that: the tool rules load when someone opens a tool, and cost nothing when nobody does.
So the split is by **blast radius**, not by topic size.
A fact that changes how any file in the repo should be written stays in `CLAUDE.md`; a fact that only matters inside one directory becomes a rule.

The six rules and the paths they claim:

| Rule | `paths:` | Why it exists |
|---|---|---|
| `contracts.md` | `src/aioc/contracts/**`, `docs/CONTRACTS.md` | the frozen contract and the sec 0 change process |
| `coordinator.md` | `src/aioc/coordinator/**` | dynamic selection, explicit context passing, parallel vs sequential, the refinement loop |
| `agents.md` | `src/aioc/agents/**` | envelope discipline, nullable fields, confidence bands, the validation-retry loop |
| `tools.md` | `src/aioc/tools/**` | the four-part description template, the error taxonomy, the deliberate `analyze_*` overlap |
| `tests.md` | `tests/**`, `**/test_*.py` | the worked-example anchor test, and one negative test per invariant |
| `platform.md` | `docker-compose.yml`, `docker/**`, `demo-app/**`, `Makefile`, `.env.example` | pinned tags, volume survival, and the chaos-mode-to-enum mapping |

`coordinator.md` and `platform.md` were added on Day 2 to close two real coverage gaps.
The coordinator carries the most heavily graded Domain 1 behaviours and had no rule at all.
The platform surface had its constraints written as comments inside `docker-compose.yml` and the `Makefile`, which are only read by someone already editing the file that the comment is defending.

---

## Why `settings.json` is split from `settings.local.json`

`.claude/settings.json` is committed; `.claude/settings.local.json` is gitignored.
The split is the difference between a team decision and a personal one.

**Committed, because the whole team should inherit it:**

- `allow` covers the inner development loop: `uv run pytest`, `uv run ruff`, `uv run mypy`, the non-destructive `make` targets, and read-only `docker compose` calls.
  These are commands where a prompt buys nothing, because the answer is always yes.
- `ask` covers the three ways to lose work: `make db-reset`, `docker compose down -v` and `--volumes`, and `git push`.
  The Day 5 seed corpus is both the RAG corpus and the eval set, and it lives in a named Docker volume.
  Dropping it silently costs a day of reseeding, so it is worth one prompt.
- `deny` covers secrets: `.env`, `secrets/**`, and any `*.pem` or `*.key`.
  This is the enforcement twin of the contract's "config values are never returned" rule.
  The contract stops a *tool* from returning a value; the deny rule stops the *agent* from reading the file at all.

Rules are evaluated deny, then ask, then allow, and the first match wins.
That ordering is why `docker compose down -v` prompts while plain `docker compose down` does not, even though the allow entry `Bash(docker compose down:*)` also matches the destructive form.

Note that `deny` cannot carry exceptions: a broad deny beats a narrower allow.
This is why the secrets rule names `Read(/.env)` exactly rather than `Read(/.env.*)`, which would also block the committed, value-free `.env.example`.

Paths use the leading-slash form (`Read(/.env)`), which anchors at the project root when written in project settings, rather than the `./` form, which anchors at whatever directory the session started in.

**Local, because it is nobody else's business:** `settings.local.json` is where "yes, don't ask again" writes.
It is gitignored alongside `CLAUDE.local.md` so that a personal approval never silently becomes a team policy.

---

## Verification

Rules are context, not configuration, so "it exists" and "it fires" are different claims.
Three checks, in increasing strength.

**1. No dead globs.** Every `paths:` pattern must match at least one tracked file, or the rule is decoration.
Run from the repo root:

```bash
for p in "src/aioc/contracts/**" "src/aioc/coordinator/**" "src/aioc/agents/**" \
         "src/aioc/tools/**" "tests/**" "docker-compose.yml" "demo-app/**"; do
  printf '%-28s -> %s file(s)\n' "$p" "$(git ls-files -- ":(glob)$p" | wc -l)"
done
```

All eleven patterns across the six rules resolve to tracked files as of Day 2.

**2. The files loaded.** Run `/context` in a session and check the **Memory files** list.
`CLAUDE.md` and any unconditional rules appear at launch; path-scoped rules appear once a matching file has been read.

**3. The rule actually fired.** Open a file under a scoped path and confirm the rule text enters context.
Observed on Day 2: reading `src/aioc/contracts/primitives.py` injected both `.claude/rules/contracts.md` and the directory-scoped `src/aioc/CLAUDE.md`.
Injection is deduplicated within a session, so a rule appears on the first matching read and not again.

Two caveats found while verifying, worth knowing before anyone re-runs this:

- `src/aioc/agents/`, `src/aioc/coordinator/`, and `src/aioc/tools/` currently hold only 0-byte `__init__.py` stubs.
  Reading an empty file yields an empty-content warning and no rule injection, so those four rules cannot be demonstrated until Phase 1 and Phase 2 put real code behind them.
  This is a property of the stubs, not of the rules.
- Rule files added mid-session are not picked up by that session.
  Verify `coordinator.md` and `platform.md` from a fresh session.

For a definitive per-rule trace, the `InstructionsLoaded` hook logs exactly which instruction files load and when.
That is the tool to reach for if a rule is ever suspected of not firing, rather than inferring it from behaviour.

---

## What was deliberately not built

- **Hooks.** They are the enforcement layer for "always run X before Y", and the natural candidate is `make lint` before a commit.
  Deferred because Day 2's scope is the configuration hierarchy, and a lint hook is worth more once there is code under `agents/` and `tools/` to lint.
  Domain 3's workflow half lands on Days 21 and 22 with Claude Code in CI.
- **A third slash command.** The plan specifies two, and `/contract` plus `/validate-schema` cover the two genuinely repeated tasks.
  A command per contract section would be config for its own sake.
- **A `permissions.defaultMode`.** Committing a default mode imposes one engineer's risk tolerance on the other.
  It belongs in `settings.local.json`.
- **A directory `CLAUDE.md` under `tools/` or `agents/`.** Those paths already have rules.
  Covering one path with both mechanisms means two files to keep in sync and a real chance of them contradicting each other, which the memory docs call out as the failure mode that makes Claude pick arbitrarily.
