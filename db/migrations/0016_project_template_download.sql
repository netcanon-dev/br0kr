-- 0016_project_template_download — open the operator-only template DOWNLOAD (A4.1) to PROJECTS, bounded by a
-- per-project DISK QUOTA (phase A4.2).
--
-- A4.1 (0015) let an OPERATOR download a net-new image (source allowlist + mandatory sha256, leader build
-- job). A4.2 lets a PROVISION-tier agent token self-service the SAME download for its OWN project, but adds
-- the one thing operator-trust provided for free: a bound on disk. Without a quota a tenant could fill a
-- node's storage by requesting huge/many images. So this migration adds:
--
-- 1. project.max_template_disk_gb — the operator-set CEILING (GB) on the total template-image disk a project
--    may own via downloads. NULL = the project may NOT self-service downloads at all (the same "off until the
--    operator opts you in" posture as the 0009 self-service ceilings; an operator enables a tenant by setting
--    a positive ceiling). The quota is enforced ABOVE admission, in gateway.request_template_download, under a
--    `SELECT ... FOR UPDATE` on THIS project row so two concurrent same-project downloads can't both slip
--    past — it debits NO admission scope and takes NO admission lock, so it cannot weaken the capacity floor.
--
-- 2. template_build.project_id — the requesting project (NULL = an OPERATOR build, A4.1 behaviour unchanged).
--    FK ON DELETE CASCADE: a build row is a disposable JOB pointer (like cluster_id), so deleting the owner
--    project drops its in-flight/queued download requests. (Residual: deleting a project mid-build can leave a
--    fenced partial vmid the build-reclaim can't see because its row is gone — operator reclaims it via the
--    `br0kr-build-<id>` fence; an extreme operator action, documented in SECURITY.md.)
--
-- 3. template_build.size_gb — the DECLARED disk budget the agent reserves up front (the actual image size is
--    unknown until downloaded). The quota check reserves THIS; the reconciler later settles to the ACTUAL
--    imported disk size and FAILS the build if actual > declared (so under-declaring can't beat the quota).
--    NULL for operator builds (no quota).
--
-- 4. template.owner_project_id — which project a downloaded template belongs to, for quota accounting (the
--    project's usage = sum of its owned templates' actual size_gb + its in-flight builds' declared size_gb).
--    FK ON DELETE SET NULL: a catalog template is a REAL asset pointing at a real Proxmox template (NOT
--    disposable) — deleting the owner project must NOT cascade-delete the catalog row (that would orphan the
--    Proxmox template); the template survives as operator-owned (and stops counting against the gone project,
--    which is correct). NULL = an operator/public template (A1–A4.1). SET NULL is safe here: the nulled rows
--    only stop counting against the DELETED project — it does not fail-open another project's quota guard.
--
-- 5. template.size_gb — the template's actual disk size in GB (read off the imported disk), the settled quota
--    charge. NULL for the hand-registered A1–A3 templates (they were never downloaded; they don't count).
--
-- A project-downloaded template is PRIVATE (public=false) and auto-granted to its owner: the reconciler's
-- atomic mark_ready inserts the catalog row (owner_project_id set, public forced false) AND a project_template
-- grant (phase-A3 visibility) in the SAME CTE, so "ready", "owned", and "usable by the owner" never diverge.
--
-- Idempotent (ADD COLUMN / ADD CONSTRAINT IF NOT EXISTS). NO admission path — the quota is a per-project
-- resource ceiling above admission, the same shape as max_leases/max_queued, NOT a capacity scope.

ALTER TABLE project          ADD COLUMN IF NOT EXISTS max_template_disk_gb bigint;
ALTER TABLE template_build   ADD COLUMN IF NOT EXISTS project_id           text REFERENCES project(id) ON DELETE CASCADE;
ALTER TABLE template_build   ADD COLUMN IF NOT EXISTS size_gb              bigint;
ALTER TABLE template         ADD COLUMN IF NOT EXISTS owner_project_id     text REFERENCES project(id) ON DELETE SET NULL;
ALTER TABLE template         ADD COLUMN IF NOT EXISTS size_gb              bigint;

DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_max_template_disk_gb_positive
    CHECK (max_template_disk_gb IS NULL OR max_template_disk_gb > 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE template_build ADD CONSTRAINT template_build_size_gb_positive
    CHECK (size_gb IS NULL OR size_gb > 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  ALTER TABLE template ADD CONSTRAINT template_size_gb_positive
    CHECK (size_gb IS NULL OR size_gb > 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- A project build MUST carry a declared size (the reservation); an operator build MUST NOT (no quota). The
-- two-way invariant keeps the quota math honest (a project row always contributes a positive reserve).
DO $$ BEGIN
  ALTER TABLE template_build ADD CONSTRAINT template_build_project_size_paired
    CHECK ((project_id IS NULL AND size_gb IS NULL) OR (project_id IS NOT NULL AND size_gb IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Quota accounting reads: sum a project's owned-template disk, and its in-flight (non-terminal) build reserves.
CREATE INDEX IF NOT EXISTS template_owner_project ON template(owner_project_id) WHERE owner_project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS template_build_project_active ON template_build(project_id)
  WHERE project_id IS NOT NULL AND state NOT IN ('READY','FAILED');
