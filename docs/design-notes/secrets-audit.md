# Secrets audit (Day 27)

The repository goes public on Day 30.
Before that, the plan asks for a scan of the whole history for leaked keys.
This is the record of that scan and of what keeps it true afterwards.

## Result, 2026-10-11

**No credential in any tracked file or in any commit on any ref.**

| Scope | Covered | Findings |
|---|---|---|
| Tracked files, working tree | 260 files, including this change (binary files skipped: the GIFs and the PNG) | 0 |
| History: every added line of every diff | 98 commits, every branch on `origin` and `khanisic` after a fresh fetch | 0 |
| Pull request heads (`refs/pull/*/head`) | 38 refs from both remotes, fetched into a temporary namespace and deleted after; they added no commit the history scan had not covered | 0 |
| Files ever added that look like secrets (`.env`, `*.pem`, `*.key`, `secret*`) | `.env.example` and `infrastructure/k8s/secret.example.yaml` only | both hold empty values, in every version |
| Local run records (`test-results/`, gitignored) the committed GIFs and `evaluations/` copies came from | every file | 0 |

What the scan does find, and allows by exact text:

- `sk-ant-test-not-a-key` and `sk-ant-not-for-a-tool-server`, test fixtures that need a key-shaped string (the second proves the Claude key never reaches an MCP tool server).
- `ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456`, the fixture that proves a GitHub patch's `token = ...` line is redacted.
- `aioc_dev_only`, the local stack's Postgres password, in `docker-compose.yml`, `.env.example`, and tests: a development default for the local stack's database, never used by a hosted one (a host supplies its own `DATABASE_URL` as a secret, `infrastructure/k8s/secret.example.yaml`).

The khanisic force push of 2026-08-09 rewrote nothing with content (HANDOFF sec 3), and the pull request refs confirm no commit outside the scanned history survives on either remote.

## How

`scripts/audit_secrets.py` (free, offline, reads git only):

```bash
uv run python scripts/audit_secrets.py              # tracked files as they are now
uv run python scripts/audit_secrets.py --history    # every added line on every ref git knows
```

It looks for the formats this project holds (Anthropic `sk-ant-`, Langfuse `sk-lf-` / `pk-lf-`, GitHub `ghp_` and `github_pat_`, Voyage `pa-`), the ones that would be a disaster anywhere (AWS access keys, private key blocks, Slack and Google tokens), and any URL whose password slot holds something other than the dev password or a test value.
Compose-style `${VAR:-default}` is expanded first, so a real password written as a default is still caught.
A finding prints cut to its first ten characters, so the audit's own output never re-leaks what it found, and a fixture is allowed by its exact text, never by loosening a pattern.

To include pull request heads, fetch them into a scratch namespace first and delete it after:

```bash
git fetch origin '+refs/pull/*/head:refs/audit/origin/*'
uv run python scripts/audit_secrets.py --history
git for-each-ref --format='delete %(refname)' refs/audit | git update-ref --stdin
```

## What keeps it true

`tests/test_secrets_audit.py` runs the tree scan in the offline suite, so CI fails a pull request that adds a credential to any tracked file; its own key-shaped values are assembled at run time, so the test file is clean too.
The other layers were already there: `.gitignore` keeps `.env`, `.env.*`, `*.pem`, `*.key`, and `secrets/` out; `.claude/settings.json` denies Claude Code reads of the same paths; CI runs with no secrets; the Docker image is built with `.env` excluded; and config values never leave a tool (`diff_release` hashes them at parse time).

The tree scan cannot see a credential added and removed inside one pull request, because CI checks the head.
Pull requests here are merged with merge commits, so such a commit would land in `main`'s history and stay on GitHub under `refs/pull/*` as well; the history scan with pull request heads, above, is the check for that, and is worth re-running before Day 30.

**If a real key is ever found:** revoke it at the provider first (a rotated key is safe however long the leaked one stays in history), then decide whether rewriting history is worth breaking every clone.
