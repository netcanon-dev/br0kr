-- 0007_capacity_checks — the positive-capacity CHECKs the operator-CRUD validators assume.
--
-- 0001 created `node` and `storage` WITHOUT a CHECK(>0) on cpu_total/mem_total_mb/total_gb (only the
-- `project` table, 0005, got its CHECKs). The dashboard's operator config writes (br0kr/admin.py
-- validate_node/validate_storage) are the guard, but the DB should be the real BACKSTOP too — defense in
-- depth + consistency with `project`, so a future writer that bypasses the validator can't persist a
-- zero/negative capacity that would reject every admit. These ALTERs add the constraints idempotently; a
-- greenfield / valid ledger already satisfies them (a populated ledger with a bad row would surface it
-- loudly here, which is the point). Postgres `numeric`/`bigint` still admit NaN/Infinity as > 0, so the
-- validators' `math.isfinite` guard stays load-bearing regardless of these CHECKs.
DO $$ BEGIN
  ALTER TABLE node ADD CONSTRAINT node_cpu_total_positive CHECK (cpu_total > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE node ADD CONSTRAINT node_mem_total_positive CHECK (mem_total_mb > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE storage ADD CONSTRAINT storage_total_gb_positive CHECK (total_gb > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
