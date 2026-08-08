-- Day 5: 18 synthetic historical incidents plus their timelines.
--
-- Runs once, on first initialisation of an empty postgres-data volume, after
-- 02-incidents.sql. Idempotent (ON CONFLICT DO NOTHING), so it is also safe to
-- apply by hand against a database this directory never touches - notably
-- hosted Postgres on Day 24, where `init/` does not run at all:
--     psql "$DATABASE_URL" -f docker/postgres/init/03-seed-incidents.sql
--
-- These rows are the RAG corpus (Day 8) and the eval set (Day 19).
--
-- Why the timestamps are absolute literals rather than now() - interval: the
-- corpus must be byte-identical across reseeds, or two eval runs are scoring
-- different data and the Day 24 token-reduction comparison against the Day 20
-- baseline is meaningless. Deterministic beats fresh-looking.
--
-- Coverage is deliberate, because the Day 19 eval scores the agent's
-- `failure_mode` against `true_failure_mode` and a mode with no rows cannot be
-- scored at all:
--
--   resource_exhaustion  4    bad_config_deploy  4
--   code_regression      4    downstream_latency 4
--   other                2  (both carrying the required detail string)
--
--   sev1 3    sev2 7    sev3 6    sev4 2
--
-- Services are the demo app's three (checkout-api, payments-api,
-- inventory-api) plus the stack's postgres and redis, so retrieved incidents
-- are actually about the system the agent can observe. An incident naming a
-- service that does not exist teaches the agent to hallucinate topology.
--
-- Impact columns are null wherever the incident predates that metric being
-- collected. That is the contract's rule (null means not determined, never a
-- placeholder) exercised in the data the agents read, not just in the models.
--
-- Ids are opaque per contract sec 1. The numbering is for review convenience
-- only - never parse meaning out of an id.

INSERT INTO incidents (
    id, title, summary, started_at, ended_at, affected_services,
    true_severity, true_failure_mode, true_failure_mode_detail,
    true_root_cause, resolution,
    error_rate_before, error_rate_after,
    p50_latency_ms_before, p50_latency_ms_after,
    p99_latency_ms_before, p99_latency_ms_after,
    requests_affected
) VALUES

-- ---------------------------------------------------------------- resource_exhaustion
('inc_0001',
 'payments-api resident memory climbed to OOM kill',
 'payments-api RSS grew steadily from 180MB to 1.4GB over two hours with no plateau, then the container was OOM-killed and restarted. Error rate rose as memory pressure grew; checkout-api saw 502s from the restart window.',
 '2026-01-14T12:05:00Z', '2026-01-14T14:40:00Z', '{payments-api,checkout-api}',
 'sev2', 'resource_exhaustion', NULL,
 'An in-memory idempotency cache for payment intents had no eviction policy, so every request retained its entry for the process lifetime.',
 'Added an LRU bound of 10000 entries and a 15-minute TTL to the idempotency cache; set a container memory limit so the failure mode is a fast restart rather than host pressure.',
 0.001, 0.074, 42, 310, 120, 2100, 48200),

('inc_0003',
 'inventory-api connection pool exhausted under catalogue sync',
 'inventory-api stopped accepting new work while a nightly catalogue sync held every pooled Postgres connection. Requests queued rather than failing, so the symptom was latency, not errors.',
 '2026-02-03T02:10:00Z', '2026-02-03T03:25:00Z', '{inventory-api,postgres}',
 'sev3', 'resource_exhaustion', NULL,
 'The catalogue sync opened one connection per batch and never returned them to the pool until the whole sync completed, exhausting a pool sized for request traffic only.',
 'Scoped each batch to a single borrowed connection with an explicit release, and gave the sync its own small pool so it can never starve request traffic.',
 0.002, 0.002, 38, 1850, 95, 8400, 3100),

('inc_0006',
 'postgres connection saturation during a traffic spike',
 'All three services began failing to acquire database connections during a promotional traffic spike. Postgres was at max_connections; none of the services were themselves unhealthy.',
 '2026-03-02T18:20:00Z', '2026-03-02T19:05:00Z', '{postgres,checkout-api,payments-api,inventory-api}',
 'sev2', 'resource_exhaustion', NULL,
 'Three services each sized their pool for peak independently, so aggregate demand exceeded the Postgres max_connections ceiling even though no single service misbehaved.',
 'Reduced per-service pool sizes and put PgBouncer in front of Postgres so connection demand is multiplexed rather than additive.',
 0.003, 0.212, 45, 640, 130, 4900, 91500),

('inc_0010',
 'checkout-api disk filled with debug logging',
 'checkout-api began failing writes after a debug log level left enabled in production filled the container volume. Metrics kept reporting healthy because the process itself never crashed.',
 '2026-04-07T09:30:00Z', '2026-04-07T11:15:00Z', '{checkout-api}',
 'sev3', 'resource_exhaustion', NULL,
 'A debug log level enabled for an investigation the previous week was never reverted, producing roughly 40x the normal log volume until the volume filled.',
 'Reverted the log level, added log rotation with a size cap, and added a disk-free alert so the next occurrence pages before writes start failing.',
 0.001, 0.089, 40, 55, 118, 380, 12400),

-- -------------------------------------------------------------------- code_regression
('inc_0002',
 'checkout-api 500 spike immediately after deploy',
 'checkout-api 5xx rate went from 0.1% to 31% within 90 seconds of a deploy completing. No downstream service showed any degradation, and the errors carried the newly deployed git SHA.',
 '2026-01-22T15:12:00Z', '2026-01-22T15:41:00Z', '{checkout-api}',
 'sev1', 'code_regression', NULL,
 'A refactor of the order total calculation dereferenced the discount object before checking whether a discount had been applied, so every order without a promo code raised.',
 'Rolled back to the previous release, then re-landed the refactor with a null guard and a regression test covering the no-discount path.',
 0.001, 0.310, 44, 41, 125, 130, 22800),

('inc_0007',
 'inventory-api raised on empty-cart availability check',
 'inventory-api returned 500 for availability checks on an empty cart. Low overall volume, so the error rate barely moved and the issue was reported by a customer before it alerted.',
 '2026-03-09T11:00:00Z', '2026-03-09T13:20:00Z', '{inventory-api}',
 'sev3', 'code_regression', NULL,
 'A batch availability endpoint added in the previous release assumed at least one line item and indexed element zero without a length check.',
 'Returned an empty result for an empty request rather than raising, and added a test case for the zero-item boundary.',
 NULL, NULL, NULL, NULL, NULL, NULL, 340),

('inc_0012',
 'inventory-api N+1 query regression after ORM upgrade',
 'inventory-api p99 latency roughly quadrupled with no change in traffic or error rate. Postgres query volume rose sharply while individual query times stayed flat.',
 '2026-04-24T13:45:00Z', '2026-04-24T17:30:00Z', '{inventory-api,postgres}',
 'sev3', 'code_regression', NULL,
 'An ORM upgrade changed the default loading strategy from eager to lazy, so a single catalogue listing issued one query per item instead of one query per page.',
 'Restored eager loading explicitly on the affected relationship rather than relying on a library default, and added a query-count assertion to the listing test.',
 0.002, 0.002, 90, 340, 260, 1180, 28700),

('inc_0016',
 'checkout-api integer overflow on an unusually large order',
 'A single customer order with a very large quantity produced a negative order total. One request affected, caught by a downstream sanity check rather than by monitoring.',
 '2026-06-04T10:20:00Z', '2026-06-04T16:00:00Z', '{checkout-api}',
 'sev4', 'code_regression', NULL,
 'Order totals were accumulated in a 32-bit integer of minor currency units, which overflows above roughly 21 million.',
 'Moved money handling to a 64-bit integer of minor units and added an upper bound on line-item quantity at the API boundary.',
 NULL, NULL, NULL, NULL, NULL, NULL, 1),

-- ------------------------------------------------------------------ bad_config_deploy
('inc_0004',
 'payments-api downstream timeout set to 50ms',
 'payments-api began failing roughly half its requests with timeouts immediately after a config change. The upstream provider was healthy throughout; payments-api was giving up before it could answer.',
 '2026-02-11T16:40:00Z', '2026-02-11T17:10:00Z', '{payments-api,checkout-api}',
 'sev2', 'bad_config_deploy', NULL,
 'A timeout value intended as 50 seconds was written in milliseconds, setting the provider call timeout to 50ms against a call whose p50 is 240ms.',
 'Corrected the value and changed the config schema to require an explicit unit suffix so a bare number is rejected rather than interpreted.',
 0.002, 0.470, 240, 50, 610, 52, 31200),

('inc_0008',
 'redis maxmemory policy evicting working-memory keys',
 'Session and idempotency lookups began missing intermittently across all three services. Redis was healthy and well under its memory limit; keys were simply gone earlier than expected.',
 '2026-03-18T20:15:00Z', '2026-03-18T22:00:00Z', '{redis,checkout-api,payments-api,inventory-api}',
 'sev2', 'bad_config_deploy', NULL,
 'The maxmemory-policy was set to allkeys-random during a capacity change, so keys with no TTL were evicted alongside cache entries once memory pressure appeared.',
 'Set the policy to volatile-lru so only keys with an explicit TTL are eviction candidates, and gave working-memory keys explicit TTLs.',
 0.002, 0.058, 44, 62, 128, 410, 24600),

('inc_0011',
 'feature flag rolled out to 100% without its backing migration',
 'checkout-api failed every request touching the new address format within seconds of a flag flip. Rolling the flag back restored service immediately, which localised it faster than the logs did.',
 '2026-04-15T14:00:00Z', '2026-04-15T14:18:00Z', '{checkout-api,postgres}',
 'sev1', 'bad_config_deploy', NULL,
 'A feature flag was moved from 5% to 100% while the database migration adding the column the code path reads was still pending.',
 'Rolled the flag back to 0%, applied the migration, then re-ramped. Added a flag precondition that blocks a ramp while a migration is pending.',
 0.001, 0.960, 43, 44, 122, 128, 8900),

('inc_0015',
 'staging connection string promoted to production',
 'inventory-api served stale catalogue data for 40 minutes without any error. Nothing was unhealthy; the service was reading the wrong database perfectly successfully.',
 '2026-05-23T08:05:00Z', '2026-05-23T08:45:00Z', '{inventory-api}',
 'sev2', 'bad_config_deploy', NULL,
 'A promotion script copied the whole environment block from staging rather than only the release tag, so the production deployment pointed at the staging database.',
 'Restricted the promotion script to the release tag only, and added a startup assertion that the resolved database hostname matches the deployment environment.',
 0.001, 0.001, 41, 39, 124, 119, 15800),

-- ----------------------------------------------------------------- downstream_latency
('inc_0005',
 'checkout-api p99 spike originating in payments-api',
 'checkout-api p99 latency rose from 120ms to 2.1s while its own error rate stayed flat. Requests were succeeding, just slowly. payments-api was the slow origin; inventory-api was nominal throughout.',
 '2026-02-19T14:00:00Z', '2026-02-19T15:30:00Z', '{payments-api,checkout-api}',
 'sev3', 'downstream_latency', NULL,
 'payments-api added a synchronous fraud-scoring call to a third-party service on the critical path, adding 800ms at p50 to every checkout.',
 'Moved fraud scoring off the critical path to an asynchronous post-authorisation check, with a synchronous fallback only above a value threshold.',
 0.001, 0.001, 44, 840, 120, 2100, 36400),

('inc_0009',
 'upstream bank API degradation during a settlement window',
 'payments-api latency and error rate both rose during a provider settlement window. The provider status page confirmed degradation; nothing in our stack had changed.',
 '2026-03-27T22:00:00Z', '2026-03-28T00:30:00Z', '{payments-api,checkout-api}',
 'sev2', 'downstream_latency', NULL,
 'The card provider degraded during its nightly settlement window, and payments-api had no circuit breaker, so slow upstream calls consumed its whole request budget.',
 'Added a circuit breaker with a 2s threshold and a queue-for-retry path, so a slow provider degrades throughput rather than failing checkout outright.',
 0.003, 0.140, 250, 1900, 620, 6800, 19400),

('inc_0014',
 'inventory-api GC pauses surfacing as checkout-api latency',
 'checkout-api p99 became erratic in bursts every few minutes. The pattern matched inventory-api garbage collection pauses; averages hid it entirely and only p99 showed the shape.',
 '2026-05-14T11:20:00Z', '2026-05-14T14:00:00Z', '{inventory-api,checkout-api}',
 'sev3', 'downstream_latency', NULL,
 'A heap sized close to the container limit forced frequent full collections in inventory-api, each pausing the service for 300-600ms while checkout-api waited on it.',
 'Raised the container memory limit and set the heap target to 70% of it, converting full collections into far cheaper incremental ones.',
 0.002, 0.002, 46, 95, 130, 1450, 22100),

('inc_0017',
 'healthcheck cascade removed inventory-api from rotation',
 'inventory-api instances were repeatedly removed from and returned to load-balancer rotation, causing intermittent checkout failures. inventory-api itself was healthy on every occasion.',
 '2026-06-16T17:10:00Z', '2026-06-16T18:40:00Z', '{inventory-api,payments-api,checkout-api}',
 'sev2', 'downstream_latency', NULL,
 'The inventory-api readiness probe called payments-api, so payments-api slowness made healthy inventory-api instances report unready and drop out of rotation.',
 'Reduced the readiness probe to local checks only and moved dependency verification to a separate non-gating deep health endpoint.',
 0.002, 0.115, 45, 210, 128, 3200, 27300),

-- --------------------------------------------------------------------------- other
('inc_0013',
 'payments-api TLS certificate expired mid-window',
 'payments-api began failing every outbound provider call at a clean minute boundary. Total and instantaneous, with no deploy, config change, or resource signal anywhere near it.',
 '2026-05-06T00:00:00Z', '2026-05-06T01:20:00Z', '{payments-api,checkout-api}',
 'sev1', 'other', 'Expired TLS client certificate on an outbound integration - a credential lifecycle failure rather than code, config, resource, or downstream degradation.',
 'The client certificate used to authenticate to the card provider expired, and its renewal was tracked in a calendar reminder that nobody owned.',
 'Rotated the certificate and moved renewal to automated issuance with a 30-day expiry alert wired to the on-call rotation.',
 0.002, 1.000, 240, NULL, 600, NULL, 14200),

('inc_0018',
 'CPU steal from a noisy neighbour on a shared host',
 'All services on one host showed latency roughly double their usual, with no change in traffic, errors, memory, or application-level CPU. Instances of the same services on other hosts were unaffected.',
 '2026-06-25T13:00:00Z', '2026-06-25T15:45:00Z', '{checkout-api,inventory-api}',
 'sev4', 'other', 'Infrastructure contention - CPU steal from a co-tenant on a shared host, outside the application and its dependencies entirely.',
 'A co-tenant workload on the same physical host saturated shared CPU, showing up as steal time our per-service dashboards did not chart.',
 'Migrated the affected instances to a different host class and added host-level steal time to the platform dashboard.',
 0.002, 0.002, 44, 88, 126, 265, 9600)

ON CONFLICT (id) DO NOTHING;


-- Timelines. `get_incident_timeline` (contract sec 7.1) returns these in
-- ascending time order per incident, and IncidentFindings rejects a timeline
-- that is not sorted, so ordering here is load-bearing.
--
-- Events are NOT constrained to sit inside the incident window, deliberately.
-- `evt_0002_1` is a deploy at 15:11 for an incident whose window opens at 15:12:
-- the trigger precedes the damage it caused. That one-minute gap is the causal
-- link an incident agent has to make, so forcing containment would delete the
-- most diagnostic event in the corpus. The contract validates ascending order
-- only, which is why this is legal as well as useful.
--
-- Sequences are written so the story is legible: the signal that fired, what was
-- found, what fixed it. The eval reads these to check whether the agent's
-- reconstruction matches what actually happened.
INSERT INTO incident_timeline_events
    (id, incident_id, at, service, description, kind, kind_detail, severity) VALUES

('evt_0001_1','inc_0001','2026-01-14T12:05:00Z','payments-api','Resident memory crossed the 800MB warning threshold','metric_threshold',NULL,'sev3'),
('evt_0001_2','inc_0001','2026-01-14T13:50:00Z','payments-api','5xx rate crossed 5% as allocation began failing','alert',NULL,'sev2'),
('evt_0001_3','inc_0001','2026-01-14T14:12:00Z','payments-api','Container OOM-killed and restarted by the runtime','restart',NULL,'sev2'),
('evt_0001_4','inc_0001','2026-01-14T14:40:00Z','payments-api','Idempotency cache bound and TTL deployed','deploy',NULL,NULL),

('evt_0002_1','inc_0002','2026-01-22T15:11:00Z','checkout-api','Release 4a91c2e deployed to production','deploy',NULL,NULL),
('evt_0002_2','inc_0002','2026-01-22T15:12:30Z','checkout-api','5xx rate crossed 5%, errors carrying SHA 4a91c2e','alert',NULL,'sev1'),
('evt_0002_3','inc_0002','2026-01-22T15:18:00Z','checkout-api','NoneType attribute error on discount object identified in logs','log_pattern',NULL,'sev1'),
('evt_0002_4','inc_0002','2026-01-22T15:41:00Z','checkout-api','Rolled back to release 1f7b03d; error rate recovered','deploy',NULL,NULL),

('evt_0003_1','inc_0003','2026-02-03T02:10:00Z','inventory-api','Nightly catalogue sync started','other','Scheduled batch job start - not a deploy, alert, or config change',NULL),
('evt_0003_2','inc_0003','2026-02-03T02:26:00Z','inventory-api','p99 latency crossed 2s with error rate flat','metric_threshold',NULL,'sev3'),
('evt_0003_3','inc_0003','2026-02-03T02:55:00Z','postgres','Connection count at pool ceiling for inventory-api role','metric_threshold',NULL,'sev3'),
('evt_0003_4','inc_0003','2026-02-03T03:25:00Z','inventory-api','Sync completed, connections released, latency recovered','other','Batch job completion - recovery was self-resolving, not an intervention',NULL),

('evt_0004_1','inc_0004','2026-02-11T16:40:00Z','payments-api','Provider timeout config changed from 50s to 50ms','config_change',NULL,NULL),
('evt_0004_2','inc_0004','2026-02-11T16:41:00Z','payments-api','Timeout error rate crossed 40%','alert',NULL,'sev2'),
('evt_0004_3','inc_0004','2026-02-11T16:58:00Z','payments-api','Provider status confirmed healthy, ruling out upstream','other','External dependency verification - evidence gathered from outside our stack',NULL),
('evt_0004_4','inc_0004','2026-02-11T17:10:00Z','payments-api','Timeout corrected to 50s','config_change',NULL,NULL),

('evt_0005_1','inc_0005','2026-02-19T14:00:00Z','payments-api','Synchronous fraud scoring enabled on the checkout path','deploy',NULL,NULL),
('evt_0005_2','inc_0005','2026-02-19T14:06:00Z','checkout-api','p99 latency crossed 1s with error rate flat','metric_threshold',NULL,'sev3'),
('evt_0005_3','inc_0005','2026-02-19T14:35:00Z','payments-api','Per-call tracing localised the added 800ms to fraud scoring','other','Trace analysis - localisation step, no state change',NULL),
('evt_0005_4','inc_0005','2026-02-19T15:30:00Z','payments-api','Fraud scoring moved off the critical path','deploy',NULL,NULL),

('evt_0006_1','inc_0006','2026-03-02T18:20:00Z','checkout-api','Request rate rose 6x on a promotional campaign','metric_threshold',NULL,NULL),
('evt_0006_2','inc_0006','2026-03-02T18:24:00Z','postgres','Connection count reached max_connections','alert',NULL,'sev2'),
('evt_0006_3','inc_0006','2026-03-02T18:31:00Z','checkout-api','Connection acquisition failures across all three services','log_pattern',NULL,'sev2'),
('evt_0006_4','inc_0006','2026-03-02T19:05:00Z','postgres','Per-service pool sizes reduced; connections recovered','config_change',NULL,NULL),

('evt_0007_1','inc_0007','2026-03-09T11:00:00Z','inventory-api','Batch availability endpoint deployed','deploy',NULL,NULL),
('evt_0007_2','inc_0007','2026-03-09T12:40:00Z','inventory-api','Customer report of checkout failure on an empty cart','other','Customer-reported signal - the issue never crossed an alert threshold',NULL),
('evt_0007_3','inc_0007','2026-03-09T13:20:00Z','inventory-api','Empty-request guard deployed','deploy',NULL,NULL),

('evt_0008_1','inc_0008','2026-03-18T20:15:00Z','redis','maxmemory-policy changed to allkeys-random','config_change',NULL,NULL),
('evt_0008_2','inc_0008','2026-03-18T21:02:00Z','checkout-api','Session lookup miss rate crossed 5%','alert',NULL,'sev2'),
('evt_0008_3','inc_0008','2026-03-18T21:30:00Z','redis','Evicted-keys counter rising against keys with no TTL','metric_threshold',NULL,'sev2'),
('evt_0008_4','inc_0008','2026-03-18T22:00:00Z','redis','Policy set to volatile-lru','config_change',NULL,NULL),

('evt_0009_1','inc_0009','2026-03-27T22:00:00Z','payments-api','Provider p99 latency crossed 2s','metric_threshold',NULL,'sev3'),
('evt_0009_2','inc_0009','2026-03-27T22:20:00Z','payments-api','Provider status page reported settlement-window degradation','other','Third-party status confirmation - external evidence, no local change',NULL),
('evt_0009_3','inc_0009','2026-03-27T22:35:00Z','checkout-api','Checkout error rate crossed 10% from payments-api timeouts','alert',NULL,'sev2'),
('evt_0009_4','inc_0009','2026-03-28T00:30:00Z','payments-api','Circuit breaker deployed; provider recovered','deploy',NULL,NULL),

('evt_0010_1','inc_0010','2026-04-07T09:30:00Z','checkout-api','Disk usage crossed 90% on the container volume','metric_threshold',NULL,'sev3'),
('evt_0010_2','inc_0010','2026-04-07T10:15:00Z','checkout-api','Write failures appearing in logs with disk full errors','log_pattern',NULL,'sev3'),
('evt_0010_3','inc_0010','2026-04-07T10:40:00Z','checkout-api','Debug log level from a prior investigation identified as the cause','config_change',NULL,NULL),
('evt_0010_4','inc_0010','2026-04-07T11:15:00Z','checkout-api','Log level reverted and rotation cap applied','config_change',NULL,NULL),

('evt_0011_1','inc_0011','2026-04-15T14:00:00Z','checkout-api','Address-format feature flag ramped from 5% to 100%','config_change',NULL,NULL),
('evt_0011_2','inc_0011','2026-04-15T14:01:00Z','checkout-api','5xx rate crossed 50%','alert',NULL,'sev1'),
('evt_0011_3','inc_0011','2026-04-15T14:06:00Z','postgres','Undefined column errors identified in checkout-api logs','log_pattern',NULL,'sev1'),
('evt_0011_4','inc_0011','2026-04-15T14:18:00Z','checkout-api','Flag rolled back to 0%; service recovered','config_change',NULL,NULL),

('evt_0012_1','inc_0012','2026-04-24T13:45:00Z','inventory-api','ORM library upgraded as part of a routine dependency bump','deploy',NULL,NULL),
('evt_0012_2','inc_0012','2026-04-24T14:20:00Z','inventory-api','p99 latency crossed 1s with error rate and traffic flat','metric_threshold',NULL,'sev3'),
('evt_0012_3','inc_0012','2026-04-24T15:10:00Z','postgres','Query volume up 40x with per-query duration unchanged','metric_threshold',NULL,'sev3'),
('evt_0012_4','inc_0012','2026-04-24T17:30:00Z','inventory-api','Eager loading restored explicitly on the affected relationship','deploy',NULL,NULL),

('evt_0013_1','inc_0013','2026-05-06T00:00:00Z','payments-api','All outbound provider calls began failing TLS handshake','alert',NULL,'sev1'),
('evt_0013_2','inc_0013','2026-05-06T00:14:00Z','payments-api','Certificate expiry confirmed as the handshake failure cause','log_pattern',NULL,'sev1'),
('evt_0013_3','inc_0013','2026-05-06T01:20:00Z','payments-api','Client certificate rotated; provider calls recovered','other','Credential rotation - neither a deploy nor a config change to the service',NULL),

('evt_0014_1','inc_0014','2026-05-14T11:20:00Z','checkout-api','p99 latency became erratic in recurring bursts','metric_threshold',NULL,'sev3'),
('evt_0014_2','inc_0014','2026-05-14T12:30:00Z','inventory-api','GC pause duration correlated with the checkout-api p99 bursts','other','Correlation analysis across two services - localisation, no state change',NULL),
('evt_0014_3','inc_0014','2026-05-14T14:00:00Z','inventory-api','Container memory limit raised and heap target set to 70%','config_change',NULL,NULL),

('evt_0015_1','inc_0015','2026-05-23T08:05:00Z','inventory-api','Release promoted from staging, carrying the full environment block','deploy',NULL,NULL),
('evt_0015_2','inc_0015','2026-05-23T08:22:00Z','inventory-api','Catalogue staleness reported with all health signals nominal','other','Customer-reported data-correctness signal - no metric crossed a threshold',NULL),
('evt_0015_3','inc_0015','2026-05-23T08:45:00Z','inventory-api','Database hostname corrected and service redeployed','deploy',NULL,NULL),

('evt_0016_1','inc_0016','2026-06-04T10:20:00Z','checkout-api','Downstream sanity check rejected a negative order total','log_pattern',NULL,'sev4'),
('evt_0016_2','inc_0016','2026-06-04T16:00:00Z','checkout-api','Money handling moved to 64-bit minor units','deploy',NULL,NULL),

('evt_0017_1','inc_0017','2026-06-16T17:10:00Z','payments-api','p99 latency crossed 2s','metric_threshold',NULL,'sev3'),
('evt_0017_2','inc_0017','2026-06-16T17:22:00Z','inventory-api','Instances began failing readiness and leaving rotation','alert',NULL,'sev2'),
('evt_0017_3','inc_0017','2026-06-16T17:50:00Z','inventory-api','Readiness probe found to call payments-api','other','Configuration review finding - localisation, no state change',NULL),
('evt_0017_4','inc_0017','2026-06-16T18:40:00Z','inventory-api','Readiness probe reduced to local checks only','deploy',NULL,NULL),

('evt_0018_1','inc_0018','2026-06-25T13:00:00Z','checkout-api','p50 latency roughly doubled with traffic and errors flat','metric_threshold',NULL,'sev4'),
('evt_0018_2','inc_0018','2026-06-25T14:10:00Z','checkout-api','Only instances on one host affected; application CPU normal','other','Host-level correlation - the signal was outside per-service metrics',NULL),
('evt_0018_3','inc_0018','2026-06-25T15:45:00Z','checkout-api','Affected instances migrated to a different host class','other','Instance migration - an infrastructure action, not a deploy',NULL)

ON CONFLICT (id) DO NOTHING;
