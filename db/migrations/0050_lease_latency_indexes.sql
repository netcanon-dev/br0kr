-- 0050_lease_latency_indexes — index the admission-SLO windows (review PERF-03).
--
-- inventory.fleet_stats computes the admission-latency percentiles over a recent window (an hour by default):
-- queue-wait from the leases admitted in it, provision latency from the leases that went ACTIVE in it. It read
-- them in one pass over the whole lease table, history included, on every dashboard poll and every metrics
-- refresh, so its cost grew with every lease ever made. These partial indexes let each percentile read only its
-- window's rows. A NULL timestamp (a lease that never reached the state, or predates 0019) is left out of the
-- index, as the query leaves it out of the percentile.
--
-- Additive and idempotent; no data changes, and an image from before it computes the same numbers without them.
-- CREATE INDEX holds a SHARE lock on lease while it builds (writes wait, reads do not); lock_timeout makes the
-- migrate one-shot fail fast instead of queueing every lease write behind a long transaction, and the next
-- deploy retries (as in 0048).
SET LOCAL lock_timeout = '10s';

CREATE INDEX IF NOT EXISTS lease_admitted_at ON lease (admitted_at) WHERE admitted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS lease_active_at ON lease (active_at) WHERE active_at IS NOT NULL;
