-- Runs once, on first initialisation of an empty postgres-data volume.
-- After that this file is ignored; use `make db-reset` to re-run it.
--
-- This file installs only the extensions that later work assumes exist.
-- Schema and seed data live in sibling numbered files (02-incidents.sql,
-- 03-seed-incidents.sql), which run after it in alphabetical order.
--
-- That is a revision of the original rule here, which said table creation must
-- not live in this directory at all. The concern behind it was two engineers
-- editing one shared .sql and conflicting on every pull - and one numbered file
-- per concern avoids that without needing a migration tool on Day 5.
--
-- The real limitation is different, and worth knowing before you rely on this
-- directory: `init/` runs ONLY on first initialisation of an empty local volume.
-- It never runs against hosted Postgres, so the Day 24 deployment has to apply
-- these files by hand. Every file here is therefore written to be runnable
-- standalone, and the seed is idempotent:
--     psql "$DATABASE_URL" -f docker/postgres/init/02-incidents.sql
--     psql "$DATABASE_URL" -f docker/postgres/init/03-seed-incidents.sql
--
-- When schema changes need to survive an existing volume rather than requiring
-- `make db-reset`, that is the point to adopt a real migration tool. See the
-- Day 24 entry in docs/EXECUTION_PLAN.md and docs/guides/incidents-table.md.

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
