-- 0053_sweep_hold_absent — keep the orphan sweep's hands off every guest the ledger has no lease for, until an
-- operator has looked (the lease-id rollback fix).
--
-- After a ledger failover or restore the ledger is behind the fleet: guests created in the lost minutes carry
-- lease ids the ledger never saw. The sweep's rollback watermark (review S20) spares them, but only while one of
-- them is at or above the ledger's highest lease id. The daemon now also pushes the lease id sequence past the
-- highest fence id it finds (fence_census.secure), so the next lease the gateway admits is above them all, and the
-- watermark signal would vanish with that one admission. These guests would then look like ordinary orphans and
-- be destroyed on the next pass, even if an agent is still using one.
--
-- sweep_hold_absent makes the sparing durable. br0kr never deletes a lease row, so a fence id with no row is the
-- signal by itself (and so is a fence id whose row never recorded a guest: the id was minted again): the daemon
-- sets this knob to true the moment its fleet census sees one (and audits that), BEFORE it moves the id floor. While it is true the sweep destroys no guest whose lease row is absent (a terminal lease's
-- guest is still reclaimed, when that lease recorded it). An operator clears it in the dashboard settings after the
-- dry-run review in docs/runbooks/ledger-ha.md. Nullable, no env seed (like max_release_attempts):
-- settings.resolve()'s COALESCE(ledger, default=false) keeps an all-NULL row byte-identical to before.
--
-- sweep_hold_ids is daemon state, not a knob (like reload_requested): every evidence id a hold was raised for,
-- written in the same UPDATE as the hold (distinct, sorted, capped; fence_census.HOLD_IDS_CAP). It makes an
-- operator's clear stick. The census raises the hold again only for an id NOT in it, and the sweep no longer
-- spares the ids in it as watermark evidence, so the next sweep after a clear reclaims what the operator reviewed.
-- A set, not a high-water mark: a node the census could not list can come back later with a LOWER lost id, and
-- that id must raise the hold again rather than pass as reviewed. NULL reads as empty (no hold ever raised).
--
-- Idempotent; a schema change is a new file, never an edit of a shipped one.

ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS sweep_hold_absent boolean;
ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS sweep_hold_ids bigint[];
