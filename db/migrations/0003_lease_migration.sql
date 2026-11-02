-- 0003_lease_migration — cross-cluster LEASE migration: the durable, resumable saga that
-- relocates a running lease's guest to another cluster RACE-FREE, on top of the PdmDriver
-- (br0kr/pdm.py) and the proven admission transaction (br0kr/admission.py).
--
-- The model (br0kr/migration.py):
--   * A migration moves the SAME lease. Its fence tag `br0kr-lease-<lease_id>` travels with the
--     guest's config during a remote migration, so the lease stays the owner — no re-stamp, and a
--     stale destroy on either cluster stays self-fencing.
--   * Capacity stays race-free by RE-DEBITING the target node through the proven `admit()`: a
--     transient RESERVATION lease (admit()'d with the reserved idem_key `br0kr-mig-<migration_id>`,
--     see br0kr/lease.py MIGRATION_IDEM_PREFIX) holds the destination slot under admit's scope lock
--     while the guest is in flight. Only AFTER the migration settles does the lease's node_id/vmid
--     flip onto the target, and the reservation is then released. So the target is never
--     over-committed (admit serializes concurrent migrations/admissions onto it) and the source is
--     debited until the guest actually lands — conservative, never under-counted.
--   * The leader-elected daemon (single-writer, like provisioning) drives the saga; each phase
--     commits durably so a crash resumes from the last committed state.
--
-- This file is the durable record of that saga. Non-destructive and idempotent (safe to re-apply),
-- like every migration: a new schema change is a NEW NNNN_*.sql file, never an edit to this one.

-- The saga states. REQUESTED → RESERVED (target slot held) → MIGRATING (actuation attempted) →
-- DONE (lease flipped onto the target, reservation settled) | ABORTED (refused/failed; reservation
-- settled if one was taken; the lease is left untouched). CREATE TYPE has no IF NOT EXISTS, so guard
-- it for an idempotent re-apply (same pattern as lease_state in 0001).
DO $$ BEGIN
  CREATE TYPE migration_state AS ENUM ('REQUESTED','RESERVED','MIGRATING','DONE','ABORTED');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS migration (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- Idempotency for the migration REQUEST: a retry with the same key returns the same migration,
  -- never a second one (the control-plane analogue of the lease idem_key). NOT NULL + UNIQUE so a
  -- NULL can't defeat it (Postgres treats NULLs as distinct).
  idem_key       text UNIQUE NOT NULL,
  -- The lease being relocated (the owner; its guest carries fence `br0kr-lease-<lease_id>`).
  lease_id       bigint NOT NULL REFERENCES lease(id),
  -- Snapshot of the lease's node at request time. The commit step's CAS only flips a lease that is
  -- STILL on this node (so a lease moved/released out from under the saga is detected, not clobbered).
  source_node    text NOT NULL REFERENCES node(id),
  -- The destination node. It must be in a DIFFERENT cluster (same-cluster moves are a PVE-native
  -- path, out of scope here — the broker only brokers the cross-cluster, capacity-bearing case).
  target_node    text NOT NULL REFERENCES node(id),
  -- The transient capacity reservation on the target: a lease, admit()'d with the reserved idem_key
  -- so it debits the target node race-free yet is never provisioned (br0kr/lease.py excludes it from
  -- the provisioning work-list). NULL until RESERVED; the destination slot is held by it until the flip.
  reservation_id bigint REFERENCES lease(id),
  state          migration_state NOT NULL DEFAULT 'REQUESTED',
  reason         text,                                  -- terminal/abort detail (observability)
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The daemon's pending-migration scan: the non-terminal saga rows, oldest first. Partial so it stays
-- tiny — terminal DONE/ABORTED rows (the bulk over time) are not indexed here, mirroring lease_due.
CREATE INDEX IF NOT EXISTS migration_pending ON migration(created_at)
  WHERE state IN ('REQUESTED','RESERVED','MIGRATING');
