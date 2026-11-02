-- 0027_migration_upid — persist the cross-cluster migration's PDM task UPID so a resume RE-ATTACHES to
-- the in-flight copy instead of re-POSTing it or mis-reading an ambiguous source (review S07).
--
-- The migration saga (br0kr/migration.py) actuates the guest move by POSTing a PDM remote-migrate task and
-- then WAITing for it. Until now the task's UPID lived only in the actuating call and was never persisted,
-- so a resume — after the poll budget (migrate_timeout), a daemon crash, or the ~one-tick leader overlap —
-- could not tell an IN-FLIGHT copy from a failed one: it re-POSTed a second migrate, or read the source as
-- momentarily 'gone' mid-copy and mapped that to a terminal ABORT that SETTLED the target reservation while
-- the copy was still landing → the target over-commits and the lease is stranded on the source.
--
-- This adds one nullable column so the saga can persist the UPID the instant the POST returns and, on the
-- next pass, re-attach with `pdm.wait(source_remote, migrate_upid)` — settling only on a CONFIRMED task
-- failure (or the guest confirmed present-and-not-ours), never while the copy is still in flight. NULL until
-- the migrate is actually POSTed; it stays set on a terminal row for observability. Non-destructive and
-- idempotent (ADD COLUMN IF NOT EXISTS), like every migration — a new schema change is a NEW NNNN_*.sql file.

ALTER TABLE migration ADD COLUMN IF NOT EXISTS migrate_upid text;

COMMENT ON COLUMN migration.migrate_upid IS
  'PDM remote-migrate task UPID (on the SOURCE remote), persisted the instant the migrate is POSTed so a '
  'resume re-attaches via pdm.wait instead of re-POSTing or mis-reading the source (br0kr/migration.py, '
  'review S07). NULL until the migrate is actuated.';
