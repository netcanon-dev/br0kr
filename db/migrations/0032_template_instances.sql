-- 0032_template_instances — a logical template's PHYSICAL instances: which concrete (cluster, node, storage,
-- vmid) actually hold a cloneable copy. The second floor of the template-reachability arc
-- (docs/designs/template-sharing-cross-cluster.md §3.2). Behaviourally IDENTICAL on the current fleet — the
-- placement gate that READS instances is 0033/PR-2; this migration adds the model + a seed backfill only.
--
-- WHY THIS EXISTS: a `template` row (0012) is a single `(cluster_id, vmid)` pin — it cannot express "this
-- image lives on node A's local pool, so ONLY node A can clone it" on a multi-node cluster. That per-node
-- hosting fact is exactly what the placement gate needs to refuse a cross-node clone. A template_instance
-- row carries it: `node_id` = the PVE node whose config OWNS the vmid (a `qm clone` must be POSTed there),
-- `storage_id` = the quota row whose pve_name the disk lives on, `vmid` = the concrete template vmid.
--
--   instance_state — REPLICATING (a copy is materialising; the job table arrives with replication), READY
--                    (a live cloneable copy), RETIRED (superseded — excluded from uniqueness + usability so
--                    a replacement can be materialised), FAILED. A READY instance ALWAYS has a vmid (CHECK).
--   origin         — seed (backfilled from the existing catalog), build, replication, manual.
--   node_id/storage_id — NULLABLE: a SEED backfilled from a bare `(cluster, vmid)` catalog row knows neither
--                    until an operator binds it (bind_instance_placement). An unbound instance is usable ONLY
--                    in a single-node cluster (0033's predicate); register_node refuses making a cluster
--                    multi-node while an unbound instance exists.
--   node_id FK is ON DELETE SET NULL so decommissioning a node is never FK-blocked; template_id/cluster_id
--   are NO ACTION (fail-closed, the 0013 rationale — a catalog/cluster removal is pre-checked, not cascaded).
--
-- Idempotent (CREATE ... IF NOT EXISTS + ON CONFLICT DO NOTHING backfill). A schema change is a NEW file.
CREATE TABLE IF NOT EXISTS template_instance (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  template_id    text   NOT NULL REFERENCES template(id),        -- NO ACTION (fail-closed)
  cluster_id     text   NOT NULL REFERENCES cluster(id),         -- NO ACTION (deliberately not 0012's CASCADE)
  node_id        text   REFERENCES node(id) ON DELETE SET NULL,  -- the PVE node OWNING the vmid's config
  vmid           int    CHECK (vmid IS NULL OR vmid > 0),        -- nullable: a REPLICATING row pre-mint has none
  storage_id     text   REFERENCES storage(id),                  -- quota row whose pve_name the disk lives on
  instance_state text   NOT NULL DEFAULT 'READY'
                 CHECK (instance_state IN ('REPLICATING','READY','RETIRED','FAILED')),
  origin         text   NOT NULL DEFAULT 'seed'
                 CHECK (origin IN ('seed','build','replication','manual')),
  replication_id bigint,          -- provenance link, no FK (the job table arrives with replication, PR-4)
  size_gb        bigint,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (instance_state <> 'READY' OR vmid IS NOT NULL)          -- a READY instance always has a guest
);

-- One live copy per (template, cluster, pool); RETIRED/FAILED excluded so a stale copy can be replaced.
CREATE UNIQUE INDEX IF NOT EXISTS template_instance_live_uniq
  ON template_instance (template_id, cluster_id, storage_id)
  WHERE instance_state IN ('REPLICATING','READY') AND storage_id IS NOT NULL;
-- One UNBOUND live copy per (template, cluster) — the seed before it is bound to a pool.
CREATE UNIQUE INDEX IF NOT EXISTS template_instance_unbound_uniq
  ON template_instance (template_id, cluster_id)
  WHERE instance_state IN ('REPLICATING','READY') AND storage_id IS NULL;
-- A per-(template, cluster) physical vmid fact. Per-TEMPLATE, so two logical templates MAY alias one guest
-- (a shared (cluster, vmid)) — legal + intentional; the cross-template guest refcount lives in the verbs.
CREATE UNIQUE INDEX IF NOT EXISTS template_instance_cluster_vmid
  ON template_instance (template_id, cluster_id, vmid)
  WHERE vmid IS NOT NULL AND instance_state IN ('REPLICATING','READY');

-- Seed backfill: one instance per existing catalog row, born READY + unbound (node_id/storage_id NULL) —
-- byte-identical semantics to today (the daemon still resolves the template's vmid the same way until 0033).
-- ON CONFLICT DO NOTHING keeps a re-apply a no-op (the unbound_uniq partial index catches the duplicate).
INSERT INTO template_instance (template_id, cluster_id, vmid, instance_state, origin)
SELECT id, cluster_id, vmid, 'READY', 'seed' FROM template
ON CONFLICT DO NOTHING;

-- Named-offender report (NOT an abort — the deploy never bricks): two catalog rows that alias one
-- (cluster, vmid) yield two instance rows sharing one physical guest — legal under the per-template index
-- above, and the shared-guest refcount in the verbs protects the guest. Just name them for the operator.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT cluster_id, vmid, array_agg(id ORDER BY id) AS ids
      FROM template GROUP BY cluster_id, vmid HAVING count(*) > 1
  LOOP
    RAISE NOTICE '0032: templates % share cluster "%" vmid % — they will map to instances over ONE physical '
                 'guest (legal; the destroy-guest verb is refcounted across templates so removing one never '
                 'orphans the other).', r.ids, r.cluster_id, r.vmid;
  END LOOP;
END $$;
