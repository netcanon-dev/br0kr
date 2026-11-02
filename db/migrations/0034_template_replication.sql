-- 0034_template_replication — the REPLICATION CONTROL PLANE: the async job table that copies a logical
-- template's physical guest onto another storage pool, so a node that cannot reach the original can clone from
-- a local copy. This migration adds the MODEL + the lease-side waiter link + the two hot knobs; it is
-- CONTROL-PLANE ONLY. No daemon step reads or advances a job yet — the DATA PLANE (the leader clone tick, the
-- admit() byte reservation, the post-copy fence + placement assertion, and the waiter coupling) arrives with a
-- LATER PR. A REQUESTED job created here holds NO capacity, NO guest, and NO reservation; it can only be
-- CANCELLED or PRUNED until the engine ships (docs/designs/template-sharing-cross-cluster.md §3.4, §5, §9 PR-4).
--
-- WHY THIS EXISTS: 0032 modelled a template's physical `template_instance` copies and 0033's gate refuses a
-- clone whose target node cannot reach any copy. On a genuinely multi-node cluster that means a template must
-- be COPIED onto a pool every node can reach. `template_replication` is the job that records + tracks such a
-- copy: which template, onto which (cluster, storage) target, by which mechanism, and its lifecycle state. The
-- `template_instance.replication_id` column (0032, no FK — audit-style) is the provenance link back to it.
--
-- LIFECYCLE (CHECK-constrained; guarded-CAS transitions in br0kr/replication.py — build_template's shape):
--   REQUESTED   — asked; NO Proxmox actuation, NO guest, NO reservation (holds nothing). PR-4 STOPS HERE.
--   COPYING     — the leader claimed it + is cloning the source guest (a REPLICATING template_instance twin is
--                 inserted in the same CAS). Data plane, later PR.
--   CONVERTING  — the copy landed + is being templatized (make_template). Data plane, later PR.
--   READY       — success: the twin template_instance is READY (a new cloneable copy); the job's transient
--                 custody of the guest has ended (`guest_settled_at` stamped). Data plane, later PR.
--   FAILED      — CANCELLED (REQUESTED→FAILED, no teardown — PR-4) or the copy failed; `error` says why
--                 (ALWAYS pre-redacted before store — §7).
--   RECLAIMING  — a stale/abandoned COPYING/CONVERTING job being torn down. Data plane, later PR.
--
--   mechanism        — clone_shared (intra-cluster full-clone onto a reachable pool; the ONLY mechanism the
--                      control plane requests) or rebuild_source (cross-cluster provenance rebuild — a later
--                      PR; the CHECK admits it so no migration is needed then, but request_replication refuses
--                      it until it is implemented).
--   project_id       — NULL = an OPERATOR replication (quota-exempt); set = attributed to a tenant. The
--                      byte-charge against a tenant's template-disk quota is enforced by the DATA-PLANE
--                      reservation (later PR), not here — a REQUESTED job reserves no bytes.
--   guest_settled_at — stamped when the job's custody of its transient guest provably ends. A CANCEL never
--                      created a guest, so it stamps now() immediately. This is the PRECONDITION for
--                      prune_replications (retention): a pruned job can never leave a live fence behind.
--   vmid / target_node_id — the target guest's Proxmox id + owning node, minted WITH node_id in one CAS by the
--                      data plane (vmid NULL until then; a READY job always has one, by CHECK). node_id FK is
--                      ON DELETE SET NULL so decommissioning a node is never FK-blocked.
--   upid             — the write-once PVE task handle (re-attached on resume). Data plane, later PR.
--
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS). A schema change is a NEW file.

CREATE TABLE IF NOT EXISTS template_replication (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  template_id        text   NOT NULL REFERENCES template(id),          -- NO ACTION (fail-closed, like 0032)
  source_instance_id bigint REFERENCES template_instance(id) ON DELETE SET NULL,  -- plan-time PROVENANCE only:
                                                                       -- SET NULL so retiring/deleting the source
                                                                       -- instance is never FK-blocked (the data
                                                                       -- plane re-resolves the source before it
                                                                       -- copies); mirrors template_instance
                                                                       -- .replication_id which has no FK at all.
  target_cluster_id  text   NOT NULL REFERENCES cluster(id),           -- NO ACTION (decommission pre-checked)
  target_storage_id  text   NOT NULL REFERENCES storage(id),           -- the target pool (delete_storage pre-checks)
  target_node_id     text   REFERENCES node(id) ON DELETE SET NULL,    -- never FK-block a node decommission
  vmid               int    CHECK (vmid IS NULL OR vmid > 0),          -- minted by the data plane (later PR)
  mechanism          text   NOT NULL CHECK (mechanism IN ('clone_shared','rebuild_source')),
  upid               text,                                             -- write-once PVE task handle (data plane)
  job_state          text   NOT NULL DEFAULT 'REQUESTED'
                     CHECK (job_state IN ('REQUESTED','COPYING','CONVERTING','READY','FAILED','RECLAIMING')),
  attempts           int    NOT NULL DEFAULT 0,
  error              text,                                             -- ALWAYS pre-redacted before store (§7)
  requested_by       text   NOT NULL,                                  -- the operator/agent identity that asked
  project_id         text   REFERENCES project(id) ON DELETE SET NULL, -- NULL = operator (quota-exempt). SET NULL
                                                                       -- (like lease.project_id + every other
                                                                       -- project FK) so delete_project is never
                                                                       -- FK-blocked: a deleted tenant's job history
                                                                       -- degrades to operator-attributed.
  size_gb            bigint,
  guest_settled_at   timestamptz,                                     -- custody of the transient guest ended
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (job_state <> 'READY' OR vmid IS NOT NULL)                    -- a READY replica always has a guest
);

-- At most ONE live job per (template, target cluster, target pool) — a re-request while one is in flight is
-- refused `replication_in_progress`, not a second concurrent copy. Terminal (READY/FAILED) rows are excluded so
-- a fresh copy can be requested after one settles.
CREATE UNIQUE INDEX IF NOT EXISTS replication_one_active
  ON template_replication (template_id, target_cluster_id, target_storage_id)
  WHERE job_state IN ('REQUESTED','COPYING','CONVERTING','RECLAIMING');
-- A per-(target cluster) physical vmid fact for the transient guest — mirrors the lease/instance fences so the
-- data plane's vmid mint can't collide. Partial: only while a guest exists (vmid stamped) AND is non-terminal.
CREATE UNIQUE INDEX IF NOT EXISTS replication_cluster_vmid
  ON template_replication (target_cluster_id, vmid)
  WHERE vmid IS NOT NULL AND job_state IN ('COPYING','CONVERTING','RECLAIMING');

-- The lease-side waiter link (§3.3/§3.4): a PENDING lease parked behind a live replication job stamps this so
-- the data-plane waiter sweep (later PR) can reject/promote it level-triggered. Added HERE — the FK target now
-- exists — as an additive nullable column; NOTHING writes it until the waiter coupling ships (so it is a no-op
-- on the live fleet). NO ACTION FK so a referenced job can't be deleted out from under a waiter —
-- prune_replications severs the terminal-lease links first (the delete_node force-sever pattern).
ALTER TABLE lease ADD COLUMN IF NOT EXISTS awaiting_replication_id bigint REFERENCES template_replication(id);

-- The two replication knobs (0021 operator_setting pattern — a SHARED value all admitting processes read, never
-- per-container env, so the gateway/dashboard/daemon can never disagree). Both nullable → settings.py's
-- COALESCE(ledger, env, default) keeps an all-NULL row byte-identical to the code default.
--   replication_mechanisms — the enabled-mechanism allowlist (a comma list; default 'clone_shared').
--                            request_replication refuses a mechanism not in it. char_length CHECK mirrors the
--                            0021 str-column discipline (settings.MAX_STR_LEN).
--   replication_enabled    — the leader-PUBLISHED data-plane fact (the leader writes its effective limit each
--                            tick in a LATER PR). DORMANT here (no writer) → reads NULL → replication_enabled()
--                            returns false, so PR-4 honestly tells a requester the engine is off. NOT an
--                            operator dial (it is a published fact, not a settings Knob) — hence a plain column.
ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS replication_mechanisms text
  CHECK (replication_mechanisms IS NULL OR char_length(replication_mechanisms) <= 64);
ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS replication_enabled boolean;
