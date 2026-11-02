-- 0002_clusters — model Proxmox CLUSTERS as a first-class scope.
--
-- A Proxmox cluster shares ONE API endpoint + token (any node serves the cluster API;
-- /cluster/nextid is cluster-wide and unique only WITHIN a cluster). So the connection is a
-- property of the CLUSTER, not the node — 0001 put it on the node as a single-cluster
-- shortcut. This migration corrects that: a `cluster` table owns the connection, each node
-- references its cluster and keeps only its in-cluster `pve_node` name.
--
-- This makes the single-cluster-per-ledger assumption explicit and removes it: nodes in
-- different clusters have independent vmid namespaces (the per-VMID fence tag is global by
-- lease id, so a cross-cluster destroy is still refused — see br0kr/pve.py). Idempotent.

CREATE TABLE IF NOT EXISTS cluster (
  id               text PRIMARY KEY,
  -- the cluster API endpoint (a bare host/IP — any node in the cluster serves it).
  pve_host         text NOT NULL,
  token_id         text NOT NULL,
  -- the token SECRET is NEVER stored here: token_secret_env names the env var holding it.
  token_secret_env text NOT NULL,
  -- optional, for future cross-cluster inventory via Proxmox Datacenter Manager (PdmDriver).
  pdm_endpoint     text
);

-- A node belongs to a cluster (NULL = unassigned → capacity-only: admission can still
-- reserve against it, but no daemon can actuate it, so its leases sit ADMITTED until a
-- cluster is assigned or the TTL reaps them). pve_node (its in-cluster name, added in
-- 0001) stays. ON DELETE SET NULL: deleting a cluster DEMOTES its nodes to capacity-only
-- rather than refusing the delete (NO ACTION) or cascading the nodes away.
ALTER TABLE node ADD COLUMN IF NOT EXISTS cluster_id text REFERENCES cluster(id) ON DELETE SET NULL;

-- Guard: this restructure DROPs the per-node connection columns. On the greenfield ledger
-- that's a no-op, but refuse to SILENTLY destroy data if a DB ever populated them (a dev /
-- staging box seeded from 0001) — abort loudly so the operator backfills cluster rows first.
-- Nested so a direct re-apply (after the columns are already gone) is a clean no-op.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'node' AND column_name = 'pve_host') THEN
    IF EXISTS (SELECT 1 FROM node WHERE pve_host IS NOT NULL) THEN
      RAISE EXCEPTION 'migration 0002 would drop populated node connection columns; create cluster rows + set node.cluster_id, then NULL the node connection columns, before applying';
    END IF;
  END IF;
END $$;

-- Connection now lives on the cluster; drop the per-node copies 0001 added.
ALTER TABLE node DROP COLUMN IF EXISTS pve_host;
ALTER TABLE node DROP COLUMN IF EXISTS token_id;
ALTER TABLE node DROP COLUMN IF EXISTS token_secret_env;

-- The daemon's registry build joins node→cluster on this; index it.
CREATE INDEX IF NOT EXISTS node_cluster ON node(cluster_id) WHERE cluster_id IS NOT NULL;
