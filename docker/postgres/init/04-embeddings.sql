-- Day 8: incident corpus embeddings for the retrieval layer's vector half.
--
-- Runs once on first initialisation of an empty postgres-data volume, after
-- 02-incidents.sql. A stack that is already up never re-runs `init/`, so this
-- file is written to be applied by hand against a live database too - that is
-- why it uses IF NOT EXISTS where 02 does not:
--     psql "$DATABASE_URL" -f docker/postgres/init/04-embeddings.sql
-- (or from the host, since init/ is mounted into the container:
--     docker compose exec postgres psql -U aioc -d aioc \
--         -f /docker-entrypoint-initdb.d/04-embeddings.sql)
--
-- A separate table rather than a vector column on `incidents`, on purpose:
-- re-embedding (new model, changed render) must never rewrite the corpus,
-- because the corpus doubles as the Day 19 eval set. The `model` column records
-- which vectors are comparable - similarity between vectors from different
-- models is meaningless, so every vector query filters on it.
--
-- The dimension is fixed at CREATE TABLE time and is a property of the chosen
-- embedding model. Day 8 chose voyage-3.5 at its default 1024 dimensions
-- (Anthropic has no embeddings endpoint, so this is an external dependency;
-- the key is VOYAGE_API_KEY in .env). Moving to a model with a different
-- dimension is a new table or an ALTER, recorded like any schema change -
-- the `model` column makes the stale rows identifiable either way.

CREATE TABLE IF NOT EXISTS incident_embeddings (
    incident_id  text NOT NULL
                 REFERENCES incidents (id) ON DELETE CASCADE,

    -- The embedding model that produced this vector, e.g. 'voyage-3.5'.
    model        text NOT NULL,

    embedding    vector(1024) NOT NULL,

    -- sha256 of the exact rendered document text that was embedded. Ingestion
    -- compares it to detect stale vectors after a corpus edit and re-embeds
    -- only those rows, which keeps re-runs cheap and idempotent.
    input_sha256 text NOT NULL,

    created_at   timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (incident_id, model)
);

-- No ANN index (ivfflat/hnsw) on purpose: the corpus is 18 rows and an exact
-- scan is both faster and recall-perfect at this size. Add an HNSW index when
-- the corpus grows past a few thousand rows, not before.
