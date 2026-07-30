# The incidents table and seed corpus

**Status: built.**
`docker/postgres/init/02-incidents.sql` holds the schema and `03-seed-incidents.sql` holds 18 synthetic incidents with 65 timeline events.
`tests/test_seed_corpus.py` guards both against contract drift, and `make verify` now checks the corpus is present and covers every failure mode.

Those rows are used twice: as the RAG corpus the Docs and Incident agents retrieve from (Day 8), and as the eval set with ground truth the harness scores against (Day 19).
This guide explains what the schema holds and why, how to re-apply it, and how to extend it.

## Why the schema lives in `init/` at all

`01-extensions.sql` originally said:

> Schema and table creation deliberately does NOT live here.
> Those belong to migrations owned by whoever writes the model, so the two engineers don't race each other in one shared file.

That decision was about **one shared file**, not about the directory.
The race it was avoiding is two engineers editing the same `.sql` and conflicting on every pull, and a separate numbered file per concern does not have that problem.
The comment has been revised to say so.

The real limitation is different: **`init/` never runs against hosted Postgres**, so Day 24 has to apply these files by hand.
Every file is written to be runnable standalone for exactly that reason, and the Day 24 entry in `docs/EXECUTION_PLAN.md` now carries the command.

There is a real tradeoff here, so know which side you are on:

| | `docker/postgres/init/` | A migration tool (Alembic, sqlx, plain numbered scripts) |
|---|---|---|
| Runs | Once, on first init of an empty volume | On demand, tracked in a version table |
| Changing an applied file | No effect until `db-reset` (destructive) | New migration, applied forward |
| Deploy story (Day 24) | None - hosted Postgres never runs `init/` | Works unchanged against Neon/Supabase |
| Cost today | Zero | A dependency plus scaffolding |

**What was done: `init/` for now, migration tool deferred to Day 24.**
The volume was empty when this landed, so `db-reset` cost nothing, and a migration tool on Day 5 is scaffolding that delays the actual checkpoint.
The Day 24 entry in `docs/EXECUTION_PLAN.md` records both the `psql` commands and the point at which a real migration path becomes necessary.

Adopt a migration tool the moment a schema change has to survive an already-populated database.
`init/` cannot express that - it only runs on an empty volume - so the alternative is a destructive reseed, which costs the corpus.

## What the table holds

Three consumers, three requirements:

1. **`get_incident_timeline`** (contract §7.1) returns an ordered event list per incident, so timeline events need their own table with a foreign key, not a JSON blob.
2. **`IncidentFindings.similar_incidents`** is a `list[str]` of incident ids, so ids must be stable, opaque, and `inc_`-prefixed (contract §1).
3. **The Day 19 eval** scores the agent's `failure_mode` against injected ground truth, so each row needs the true `FailureMode` recorded separately from anything the agent produced.

Two conventions from the contract carry into the schema:

- **Enum values are `CHECK` constraints, not Postgres `ENUM` types.**
  A new enum member is a `1.1.0` minor bump under CONTRACTS.md §0, and `ALTER TYPE ... ADD VALUE` cannot run inside a transaction in older Postgres and cannot be reverted.
  A `CHECK` constraint is one `ALTER TABLE` to change and shows up plainly in a diff.
- **The `other`/`detail` pairing is enforced in SQL.**
  The contract validates that `*_detail` is non-null exactly when its partner is `other`.
  Encoding that as a `CHECK` means a bad seed row is rejected at insert instead of failing later inside a Pydantic validator, where it reads as an agent bug.

Timestamps are `timestamptz`.
The contract requires RFC 3339 with an explicit `Z`, and `timestamptz` is the only type that round-trips that without a local-offset guess.

## The files

| File | Holds |
|---|---|
| `docker/postgres/init/02-incidents.sql` | `incidents` and `incident_timeline_events`, their CHECK constraints, and five indexes |
| `docker/postgres/init/03-seed-incidents.sql` | 18 incidents, 65 timeline events, idempotent |

Read the SQL there rather than a copy here.
Both files carry their rationale in comments, and a duplicate in this guide would drift from them silently.
Files in `init/` run in alphabetical order, so the numeric prefixes guarantee each runs after what it depends on.


## Deliberately not included: the embedding column

There is no `vector` column here, and that is on purpose.

A `vector` column needs a fixed dimension at `CREATE TABLE` time, and the dimension is a property of the embedding model.
No embedding model has been chosen yet - that decision belongs to Day 8's ingestion pipeline, and Anthropic does not provide an embeddings endpoint, so it is an additional external dependency to pick.
Guessing a dimension now means either a `db-reset` later or an `ALTER TABLE` against seeded data.

Day 5 does not need embeddings.
Seeding is `INSERT`, and the Day 5 checkpoint ("break the app, agent produces valid JSON") reads Prometheus, not the corpus.

When Day 8 picks a model, add `03-embeddings.sql` with a separate table:

```sql
CREATE TABLE incident_embeddings (
    incident_id text PRIMARY KEY REFERENCES incidents (id) ON DELETE CASCADE,
    model       text        NOT NULL,        -- which model produced this
    embedding   vector(1536) NOT NULL,       -- dimension follows the model
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

A separate table means re-embedding with a different model does not touch the corpus rows, and the `model` column keeps you honest about which vectors are comparable.

## Re-applying it

`init/` only runs on an **empty** data volume, so re-running these files locally means destroying the volume.
**That now costs the seeded corpus**, which was not true before it was seeded.

```bash
# DESTRUCTIVE: drops volumes and re-runs every file in docker/postgres/init/.
docker compose down -v
docker compose up -d --wait
```

`make db-reset` is the same two commands.
GNU Make is not installed on this machine, so paste them directly or run `winget install ezwinports.make`.

`.claude/settings.json` routes both `make db-reset` and `docker compose down -v` to `ask`, so you will be prompted.
That prompt is the safety net for exactly this command - do not add it to the allowlist.

Against a database `init/` never touches - hosted Postgres on Day 24 - apply the files directly instead.
The seed is idempotent, so re-running it is safe; the schema files are not, and will fail loudly if the tables already exist.

```bash
for f in docker/postgres/init/*.sql; do psql "$DATABASE_URL" -f "$f"; done
```

## Verifying it took

A container reporting healthy is not the same as a schema being present.
A syntax error in `02-incidents.sql` makes Postgres log the failure and carry on, so check the tables rather than the container:

`make verify` covers the routine case - it now asserts the corpus is present, is 15-20 rows, and covers all five `FailureMode` members.
A mode with no rows is not a thin spot; it is a mode the Day 19 eval cannot score at all, which is why coverage is a `verify` check rather than a note.

For a manual pass after changing the SQL:

```bash
# The init log is where a broken .sql actually surfaces.
docker compose logs postgres | grep -iE "error|fatal"

docker compose exec -T postgres psql -U aioc -d aioc -c "
  SELECT true_failure_mode, count(*) FROM incidents GROUP BY 1 ORDER BY 2 DESC;"

# Prove the contract invariant is enforced: this INSERT must fail.
docker compose exec -T postgres psql -U aioc -d aioc -c \"
  INSERT INTO incidents (id,title,summary,started_at,true_severity,
      true_failure_mode,true_failure_mode_detail,true_root_cause,resolution)
  VALUES ('inc_bad','x','x',now(),'sev2','code_regression','should be null','x','x');\"
# Expect: new row violates check constraint
```

That last check is the one worth keeping.
If it succeeds, the `other`/`detail` pairing is not being enforced and bad seed data will reach the agents.

## Extending the corpus

`tests/test_seed_corpus.py` runs offline and is the thing that will tell you when a change is wrong.
It parses both `.sql` files and asserts the `CHECK` lists match the Python enums, every `FailureMode` has at least two rows, ids carry their contract prefixes, referenced services exist in the demo stack, the seed stays idempotent, and timestamps stay absolute rather than `now()`-relative.

Two rules worth knowing before you edit the seed:

- **Keep timestamps absolute.** A `now() - interval` corpus changes between runs, and two eval runs then score different data - which makes the Day 24 token-reduction comparison against the Day 20 baseline measure the corpus drifting rather than the context work.
- **Timeline events may fall outside their incident window, on purpose.** `evt_0002_1` is a deploy one minute before its incident's `started_at`: the trigger preceding the damage it caused. That gap is the causal link the agent has to make, and the contract validates ascending order only, so containment is neither required nor desirable.
