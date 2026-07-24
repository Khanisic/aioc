-- Runs once, on first initialisation of an empty postgres-data volume.
-- After that this file is ignored; use `make db-reset` to re-run it.
--
-- Schema and table creation deliberately does NOT live here. Those belong to
-- migrations owned by whoever writes the model, so the two engineers don't
-- race each other in one shared file. This file only installs the extensions
-- that later work assumes exist.

-- Day 8: pgvector ingestion pipeline - embeddings, similarity search.
-- This covers both the episodic and semantic memory tiers.
CREATE EXTENSION IF NOT EXISTS vector;

-- Day 8: "hybrid search" in the plan means vector similarity combined with
-- lexical matching. pg_trgm supplies the lexical half (trigram similarity and
-- fast ILIKE), so the retrieval code doesn't need a second datastore.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Stable, collision-resistant ids for seeded incidents and document chunks.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Fail loudly at container start rather than silently at query time on Day 8.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION
            'pgvector is not installed. The postgres image must be pgvector/pgvector, not postgres.';
    END IF;
END
$$;
