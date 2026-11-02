-- 0012_templates — an operator-curated OS-template CATALOG (read surface; phase A1).
--
-- Until now the ONLY template mechanism was a single daemon-wide env vmid (`BR0KR_VM_TEMPLATE`): the
-- dashboard/inventory surfaced nodes/clusters/storage/projects/queue but NOT templates, an agent could not
-- see (let alone choose) an image, and there was no per-project image governance. This table is the first
-- slice (A1): a CATALOG an operator registers cloneable templates in, surfaced read-only on the dashboard
-- and to agents (inventory.list_templates + the MCP list_templates tool). It does NOT yet change
-- provisioning — per-request selection (a lease.template_id) is phase A2 and per-project governance (a
-- public flag + a project<->template allowlist) is phase A3; this migration deliberately adds NEITHER, to
-- keep A1 a pure, race-free read surface that touches no admission path.
--
--   id           — the operator-chosen catalog handle an agent will later select by (phase A2).
--   cluster_id   — the cluster whose `vmid` this template lives on. A template vmid is per-cluster
--                  (Proxmox /cluster/nextid is cluster-scoped), so a catalog entry is bound to ONE cluster.
--                  REFERENCES cluster ON DELETE CASCADE: a catalog row is a POINTER at a cluster's template,
--                  not capacity to preserve — if the cluster is removed the pointer is meaningless, so it
--                  goes too (the same reasoning as api_token's project FK CASCADE, distinct from the
--                  node/lease SET-NULL demote which preserves capacity/audit history).
--   vmid         — the Proxmox template vmid on that cluster (CHECK > 0; the daemon will clone it in A2).
--   name         — an optional human label for the dashboard / agent (NULL → display the id).
--   description  — optional free text.
--   created_at   — bookkeeping.
--
-- Config-as-data, operator-managed (br0kr/admin.create/set/delete_template, validated by validate_template;
-- the dashboard's token-gated CRUD). Idempotent (CREATE TABLE/INDEX IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS template (
  id           text PRIMARY KEY,
  cluster_id   text NOT NULL REFERENCES cluster(id) ON DELETE CASCADE,
  vmid         int  NOT NULL CHECK (vmid > 0),
  name         text,
  description  text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS template_cluster ON template(cluster_id);
