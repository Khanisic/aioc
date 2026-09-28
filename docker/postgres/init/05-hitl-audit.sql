-- Day 17: the audit log of human-in-the-loop approval decisions.
--
-- Runs once on first initialisation of an empty postgres-data volume. A stack
-- that is already up never re-runs `init/`, so this file is written to be
-- applied by hand against a live database too - IF NOT EXISTS / OR REPLACE
-- throughout, like 04-embeddings.sql:
--     psql "$DATABASE_URL" -f docker/postgres/init/05-hitl-audit.sql
-- (or from the host, since init/ is mounted into the container:
--     docker compose exec postgres psql -U aioc -d aioc \
--         -f /docker-entrypoint-initdb.d/05-hitl-audit.sql)
-- `aioc.hitl.audit.PostgresAuditLog` applies it itself when the table is missing.
--
-- One row per `aioc.hitl.ApprovalDecision`: what the gate decided about one
-- recommended production write, who decided, when, and why - `not_required`
-- included, so the log says why nobody was asked as well as who answered.
-- The scalar columns are the ones a reader filters on; `record` is the whole
-- decision as the gate produced it (its own JSON, round-tripped by
-- `ApprovalDecision.model_validate`), so nothing is lost to the column list.
--
-- Append-only, enforced here rather than promised in code: the two triggers
-- refuse UPDATE, DELETE, and TRUNCATE outright. An audit record that can be
-- edited is not an audit record. Correcting one means appending another that
-- says so.

CREATE TABLE IF NOT EXISTS hitl_audit_log (
    seq           bigint      GENERATED ALWAYS AS IDENTITY,  -- append order; readers sort by it
    decision_id   text        PRIMARY KEY,           -- 'apr_' + 8 hex, from the gate
    request_id    text        NOT NULL,              -- the CoordinatorResponse reviewed
    invocation_id text        NOT NULL,              -- the agent invocation that recommended
    agent         text        NOT NULL,
    source        text        NOT NULL,              -- recommended_action | rollback_recommendation
    action_id     text        NOT NULL,
    decision      text        NOT NULL
                  CHECK (decision IN ('approved', 'denied', 'not_required')),
    decided_by    text        NOT NULL,              -- an approver's identity, or 'policy'
    decided_at    timestamptz NOT NULL,
    note          text        NOT NULL,
    record        jsonb       NOT NULL,              -- the full ApprovalDecision
    recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_audit_log_request_idx
    ON hitl_audit_log (request_id, decided_at);
CREATE INDEX IF NOT EXISTS hitl_audit_log_decided_at_idx
    ON hitl_audit_log (decided_at);

CREATE OR REPLACE FUNCTION hitl_audit_log_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'hitl_audit_log is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER hitl_audit_log_no_rewrite
    BEFORE UPDATE OR DELETE ON hitl_audit_log
    FOR EACH ROW EXECUTE FUNCTION hitl_audit_log_is_append_only();

CREATE OR REPLACE TRIGGER hitl_audit_log_no_truncate
    BEFORE TRUNCATE ON hitl_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION hitl_audit_log_is_append_only();
