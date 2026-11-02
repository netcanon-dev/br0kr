-- 0036_replication_copy_started_at — a stable COPY-START epoch for the replication wall-clock cap (PR-5;
-- design §5.3). The stale/wedged reclaim has TWO cutoffs: an IDLE cutoff (updated_at older than the stale
-- window → a leader died mid-copy) and a WALL cutoff (a job that keeps updated_at fresh by leader polling but
-- never reaches READY — a wedged copy the idle check can't see). The wall cutoff MUST measure "how long has
-- this COPY been running", NOT "how long ago was the job requested": PR-4's decided contract lets an operator
-- create a REQUESTED job while the engine is OFF (replication_enabled=false), so a job can sit REQUESTED for
-- days before the engine advances it — and basing the wall cap on created_at would then reclaim+destroy a
-- perfectly healthy clone on its very FIRST copy attempt (Fable: reservation-leak MEDIUM). A REQUESTED job
-- holds no guest and no reservation, so its age says nothing about a wedged copy (the same S04/F5a rule that
-- already keeps REQUESTED out of the idle reclaim).
--
-- So the leader stamps copy_started_at at the REQUESTED→COPYING claim (write-once — the claim CAS matches only
-- a REQUESTED job) and the wall cap keys on it. NULL until the copy starts (a REQUESTED job → never wall-capped,
-- and it is already excluded from the reclaim states anyway). Idempotent; a schema change is a NEW file.

ALTER TABLE template_replication ADD COLUMN IF NOT EXISTS copy_started_at timestamptz;
