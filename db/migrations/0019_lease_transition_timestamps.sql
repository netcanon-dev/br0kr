-- 0019_lease_transition_timestamps — record WHEN a lease entered ADMITTED and ACTIVE, so the broker can
-- report admission SLOs (queue-wait = admitted_at − created_at; provision latency = active_at − admitted_at)
-- straight from the ledger, with no separate metrics store.
--
-- Both columns are NULLABLE and PURELY ADDITIVE: they are stamped INSIDE the same guarded CAS / INSERT that
-- already performs the state transition (`admitted_at` on admit()'s ADMITTED insert + the PENDING→ADMITTED
-- promote CAS; `active_at` on mark_active's PROVISIONING→ACTIVE CAS — all in br0kr/admission.py + lease.py),
-- so they add NO new statement and cannot touch the admission locking / capacity path (the double-allocation
-- floor is unaffected — this writes a timestamp column, debits nothing, takes no lock of its own). A row that
-- predates this migration, or that never reached the state (a PENDING or terminal-from-PENDING lease that was
-- never admitted; a lease released before ACTIVE), keeps NULL — the percentile reads simply exclude NULLs, so
-- a partial history never skews an SLO. Idempotent.
ALTER TABLE lease ADD COLUMN IF NOT EXISTS admitted_at timestamptz;
ALTER TABLE lease ADD COLUMN IF NOT EXISTS active_at   timestamptz;
