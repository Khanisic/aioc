-- Day 5: the historical incident corpus schema.
--
-- Runs once, on first initialisation of an empty postgres-data volume, after
-- 01-extensions.sql. Changing this file has no effect on a stack that is
-- already up; it needs `make db-reset` (destructive) to take.
--
-- Also safe to apply by hand against a database this directory never touches -
-- notably hosted Postgres on Day 24, where `init/` does not run at all:
--     psql "$DATABASE_URL" -f docker/postgres/init/02-incidents.sql
--
-- The rows this schema holds are used twice: as the RAG corpus the agents
-- retrieve from (Day 8) and as the eval set with ground truth (Day 19). The
-- `true_*` columns are injected ground truth - what actually happened, not what
-- an agent concluded. Never write agent output into them.
--
-- Enum values are quoted from CONTRACTS.md sec 4.1 and must not drift. They are
-- CHECK constraints rather than Postgres ENUM types because adding a member is a
-- contract minor bump, and ALTER TABLE reviews and reverts far more easily than
-- ALTER TYPE (which cannot run in a transaction on older Postgres, and cannot be
-- undone). tests/test_seed_corpus.py asserts these lists against the Python
-- enums, so drift fails the suite rather than surfacing as bad retrieval.

CREATE TABLE incidents (
    -- Opaque prefixed id (contract sec 1). Referenced by
    -- IncidentFindings.similar_incidents, so it must be stable across reseeds.
    -- Opaque means opaque: never parse meaning out of one.
    id                  text PRIMARY KEY
                        CHECK (id ~ '^inc_[a-z0-9_]+$'),

    title               text        NOT NULL,
    summary             text        NOT NULL,

    -- IncidentWindow (contract sec 4.1). ended_at NULL means still ongoing.
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
    -- placeholder - the same rule the agents follow. Several seed rows leave
    -- these null on purpose so retrieval and the eval both see real absences.
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


-- No `vector` column here on purpose. A vector column needs a fixed dimension
-- at CREATE TABLE time, and the dimension is a property of an embedding model
-- that has not been chosen yet (Day 8 owns that decision, and Anthropic has no
-- embeddings endpoint, so it is a further external dependency). Day 5 does not
-- need embeddings: seeding is INSERT, and the Day 5 checkpoint reads Prometheus.
-- Day 8 adds 04-embeddings.sql with a separate incident_embeddings table keyed
-- by incident_id, so re-embedding never rewrites the corpus and the `model`
-- column records which vectors are comparable.
