# How to add the incidents table to `docker/postgres/init/`

Day 5 seeds 15-20 synthetic historical incidents into Postgres.
Those rows are used twice: as the RAG corpus the Docs and Incident agents retrieve from (Day 8), and as the eval set with ground truth the harness scores against (Day 19).
This guide covers where the schema goes, what it has to hold, and how to apply it.

## Read this first: it contradicts a comment in the repo

`docker/postgres/init/01-extensions.sql` currently says:

> Schema and table creation deliberately does NOT live here.
> Those belong to migrations owned by whoever writes the model, so the two engineers don't race each other in one shared file.

That decision was about **one shared file**, not about the directory.
The race it was avoiding is two engineers editing the same `.sql` and conflicting on every pull.
A separate numbered file per owner does not have that problem, so adding `02-incidents.sql` is compatible with the intent even though it contradicts the letter of the comment.

There is a real tradeoff either way, so pick deliberately:

| | `docker/postgres/init/` | A migration tool (Alembic, sqlx, plain numbered scripts) |
|---|---|---|
| Runs | Once, on first init of an empty volume | On demand, tracked in a version table |
| Changing an applied file | No effect until `db-reset` (destructive) | New migration, applied forward |
| Deploy story (Day 24) | None - hosted Postgres never runs `init/` | Works unchanged against Neon/Supabase |
| Cost today | Zero | A dependency plus scaffolding |

**Recommendation: use `init/` for Day 5, and plan to replace it before Day 24.**
The volume is empty right now, so `db-reset` is free, and a migration tool on Day 5 is scaffolding that delays the actual checkpoint.
But `init/` never runs against a hosted database, so Day 24 needs the schema applied some other way regardless.
Add a note to the Day 24 entry in `docs/EXECUTION_PLAN.md` when you do this, or the gap will be discovered at deploy time.

If you take the recommendation, **update the comment in `01-extensions.sql`** in the same commit so the file stops contradicting the directory next to it.

## What the table has to hold

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

## The file

Write this as `docker/postgres/init/02-incidents.sql`.
Files in `init/` run in alphabetical order, so the `02-` prefix guarantees it runs after the extensions it depends on.

```sql
-- Day 5: the synthetic historical incident corpus.
--
-- Runs once, on first initialisation of an empty postgres-data volume, after
-- 01-extensions.sql. Changing this file has no effect on a stack that is
-- already up; it needs `make db-reset` (destructive) to take.
--
-- These rows are the RAG corpus (Day 8) and the eval set (Day 19). The
-- `true_*` columns are injected ground truth: what actually happened, not
-- what an agent concluded. Never write agent output into them.
--
-- Enum values below are quoted from CONTRACTS.md sec 4.1 and must not drift.
-- They are CHECK constraints rather than Postgres ENUM types because adding a
-- member is a contract minor bump, and ALTER TABLE is far easier to review and
-- revert than ALTER TYPE.

CREATE TABLE incidents (
    -- Opaque prefixed id (contract sec 1). Referenced by
    -- IncidentFindings.similar_incidents, so it must be stable across reseeds.
    id                  text PRIMARY KEY
                        CHECK (id ~ '^inc_[a-z0-9_]+$'),

    title               text        NOT NULL,
    summary             text        NOT NULL,

    -- IncidentWindow (contract sec 4.1). Half-open: ended_at NULL means ongoing.
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    CHECK (ended_at IS NULL OR ended_at >= started_at),

    -- Plain factual scalars, not assessments: these are recorded truth.
    affected_services   text[]      NOT NULL DEFAULT '{}',

    -- Injected ground truth for the Day 19 eval.
    true_severity       text        NOT NULL
                        CHECK (true_severity IN ('sev1','sev2','sev3','sev4','other')),
    true_severity_detail text,
    CHECK ((true_severity = 'other') = (true_severity_detail IS NOT NULL)),

    true_failure_mode   text        NOT NULL
                        CHECK (true_failure_mode IN (
                            'resource_exhaustion','bad_config_deploy',
                            'downstream_latency','code_regression','other')),
    true_failure_mode_detail text,
    CHECK ((true_failure_mode = 'other') = (true_failure_mode_detail IS NOT NULL)),

    true_root_cause     text        NOT NULL,
    resolution          text        NOT NULL,

    -- Impact (contract sec 4.1). NULL means not measured, never a guessed
    -- placeholder - the same rule the agents follow.
    error_rate_before   double precision CHECK (error_rate_before BETWEEN 0 AND 1),
    error_rate_after    double precision CHECK (error_rate_after  BETWEEN 0 AND 1),
    p50_latency_ms_before integer   CHECK (p50_latency_ms_before >= 0),
    p50_latency_ms_after  integer   CHECK (p50_latency_ms_after  >= 0),
    p99_latency_ms_before integer   CHECK (p99_latency_ms_before >= 0),
    p99_latency_ms_after  integer   CHECK (p99_latency_ms_after  >= 0),
    requests_affected   bigint      CHECK (requests_affected >= 0),

    -- Provenance. 'synthetic' rows are hand-authored; 'chaos_run' rows are
    -- captured from a real `make chaos-<mode>` injection against the demo app,
    -- where the ground truth is readable off the chaos_knob_value gauges.
    source              text        NOT NULL DEFAULT 'synthetic'
                        CHECK (source IN ('synthetic','chaos_run')),

    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Retrieval filters the eval and the agents actually use.
CREATE INDEX incidents_started_at_idx    ON incidents (started_at DESC);
CREATE INDEX incidents_failure_mode_idx  ON incidents (true_failure_mode);
CREATE INDEX incidents_services_idx      ON incidents USING gin (affected_services);

-- Lexical half of Day 8's hybrid search. pg_trgm is installed by
-- 01-extensions.sql; this is the index that makes it useful.
CREATE INDEX incidents_summary_trgm_idx  ON incidents USING gin (summary gin_trgm_ops);


-- What `get_incident_timeline` (contract sec 7.1) returns: one row per event,
-- ordered. A separate table rather than a JSON column because the tool sorts,
-- filters, and pages over these, and because each event cites its own evidence.
CREATE TABLE incident_timeline_events (
    id              text PRIMARY KEY
                    CHECK (id ~ '^evt_[a-z0-9_]+$'),
    incident_id     text        NOT NULL
                    REFERENCES incidents (id) ON DELETE CASCADE,

    at              timestamptz NOT NULL,
    service         text        NOT NULL,
    description     text        NOT NULL,

    kind            text        NOT NULL
                    CHECK (kind IN (
                        'deploy','alert','config_change','restart','scale',
                        'metric_threshold','log_pattern','other')),
    kind_detail     text,
    CHECK ((kind = 'other') = (kind_detail IS NOT NULL)),

    severity        text
                    CHECK (severity IS NULL OR severity IN
                           ('sev1','sev2','sev3','sev4','other'))
);

-- The tool returns events in ascending time order, and the contract rejects a
-- timeline that is not sorted. Index the sort so the tool never has to.
CREATE INDEX incident_timeline_events_incident_at_idx
    ON incident_timeline_events (incident_id, at);
```

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

## Applying it

`init/` only runs on an **empty** data volume.
The volume is currently empty (only `01-extensions.sql` has run, and there are no tables), so this costs nothing today and will cost the seeded corpus later.

```bash
# Confirm there is nothing to lose first.
docker compose exec -T postgres psql -U aioc -d aioc -c '\dt'
# Expect: "Did not find any relations."

# DESTRUCTIVE: drops volumes and re-runs every file in docker/postgres/init/.
docker compose down -v
docker compose up -d --wait
```

`make db-reset` is the same two commands.
GNU Make is not installed on this machine, so paste them directly or run `winget install ezwinports.make`.

`.claude/settings.json` routes both `make db-reset` and `docker compose down -v` to `ask`, so you will be prompted.
That prompt is the safety net for exactly this command - do not add it to the allowlist.

## Verifying it took

A container reporting healthy is not the same as a schema being present.
A syntax error in `02-incidents.sql` makes Postgres log the failure and carry on, so check the tables rather than the container:

```bash
docker compose exec -T postgres psql -U aioc -d aioc -c '\dt'
# Expect: incidents, incident_timeline_events

# The init log is where a broken .sql actually surfaces.
docker compose logs postgres | grep -iE "error|fatal"

# Prove the contract invariant is enforced: this INSERT must fail.
docker compose exec -T postgres psql -U aioc -d aioc -c "
  INSERT INTO incidents (id,title,summary,started_at,true_severity,
      true_failure_mode,true_failure_mode_detail,true_root_cause,resolution)
  VALUES ('inc_bad','x','x',now(),'sev2','code_regression','should be null','x','x');"
# Expect: new row violates check constraint
```

That last check is the one worth keeping.
If it succeeds, the `other`/`detail` pairing is not being enforced and bad seed data will reach the agents.

Extend `make verify` with the `\dt` check once the table exists.
The platform rule is that `verify` proves the stack is *usable*, not merely running, and a missing incidents table is precisely the kind of failure it is there to catch.
