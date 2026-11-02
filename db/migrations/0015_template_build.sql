-- 0015_template_build — operator-driven template DOWNLOAD: a trusted-source allowlist + an async build job
-- that fetches a net-new OS image, imports it, templatizes it, and registers it in the catalog (phase A4.1).
--
-- Phases A1–A3 surfaced, selected, and governed templates an operator had ALREADY prepared by hand (point
-- the catalog at an existing Proxmox template vmid). A4 is the missing first mile: bring a NET-NEW image in
-- by DOWNLOADING it. This is the RISKIEST template slice — an arbitrary image download is a supply-chain +
-- disk-blowup hazard — so A4.1 is deliberately the smallest safe cut: OPERATOR-ONLY (no agent surface, no
-- per-project quota yet — that is A4.2), every download restricted to an operator-curated SOURCE ALLOWLIST,
-- and a MANDATORY SHA-256 the downloader verifies. The actual fetch/import/templatize is async and ACTUATES
-- on Proxmox, so it runs as a leader-daemon BUILD JOB (the same single-writer/leader discipline as lease
-- provisioning), never inline in the operator request.
--
-- 1. template_source — the operator-curated allowlist of trusted download URL PREFIXES. A build is REFUSED
--    (`source_not_allowed`) unless its url begins with one of these prefixes (matched scheme+host+path-prefix
--    in br0kr/imgbuild.source_allowed, NOT a substring) — so a hostile or typo'd URL can never be fetched.
--    Config-as-data, operator-managed (admin.create/delete_template_source, the dashboard's token-gated CRUD).
--
-- 2. template_build — the async download/import/templatize JOB. Lifecycle (CHECK-constrained):
--      REQUESTED  — the operator asked; no Proxmox actuation yet (holds no vmid).
--      DOWNLOADING— the leader daemon claimed it, allocated `vmid`, and is fetching the image (checksum
--                   verified server-side by Proxmox download-url).
--      IMPORTING  — `vmid` allocated; the downloaded disk imported into a transient build VM stamped with
--                   ONLY the build fence `br0kr-build-<id>` (cloud-init drive + qemu-guest-agent too), NOT
--                   the `br0kr-template` marker — a template=0 guest carrying the marker would be reclaimed
--                   by the clone-orphan sweep. The marker is applied by make_template ONLY AFTER the VM is
--                   converted to a template (template=1, sweep-immune); the build fence is stripped after
--                   the catalog row commits.
--      READY      — success: the `template` catalog row (target_template_id) was inserted; the image is now
--                   selectable by A2/A3.
--      FAILED     — the fetch/import/convert failed (or was abandoned); `error` carries why and the partial
--                   `vmid` (if any) is destroyed by the daemon — a half-built image never lingers.
--    `cluster_id` binds the build to the cluster whose driver actuates it (a template vmid is per-cluster,
--    like the catalog). FK ON DELETE CASCADE — a build record is a job pointer, not capacity to preserve
--    (the same reasoning as the template catalog / api_token FKs, distinct from lease's audit-preserving NO
--    ACTION). `storage_id` is the Proxmox storage NAME the image is downloaded to / the disk imported on (a
--    plain operator-provided string, NOT the broker's `storage` quota table — A4.1 has no per-project disk
--    quota yet). `public` is forwarded to the catalog row on success (phase-A3 visibility). The partial
--    index serves the daemon's "claim the oldest REQUESTED build" scan.
--
-- Idempotent (CREATE TABLE/INDEX IF NOT EXISTS). NO admission path — a build holds no lease/capacity scope;
-- it is config provisioning, governed by operator trust + the allowlist + the checksum, not by admit().
CREATE TABLE IF NOT EXISTS template_source (
  url_prefix   text PRIMARY KEY,
  description  text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS template_build (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cluster_id         text NOT NULL REFERENCES cluster(id) ON DELETE CASCADE,
  target_template_id text NOT NULL,
  url                text NOT NULL,
  sha256             text NOT NULL,
  storage_id         text NOT NULL,
  name               text,
  description        text,
  public             boolean NOT NULL DEFAULT true,
  vmid               int CHECK (vmid IS NULL OR vmid > 0),
  state              text NOT NULL DEFAULT 'REQUESTED'
                       CHECK (state IN ('REQUESTED','DOWNLOADING','IMPORTING','READY','FAILED')),
  error              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
-- The daemon claims the oldest non-terminal build; index the live partition (terminal rows excluded).
CREATE INDEX IF NOT EXISTS template_build_active ON template_build(created_at, id)
  WHERE state NOT IN ('READY','FAILED');
-- At most ONE non-terminal build per target catalog id (a re-request while one is in flight is refused
-- `build_in_progress`, not a second concurrent fetch of the same image).
CREATE UNIQUE INDEX IF NOT EXISTS template_build_one_active_per_target
  ON template_build(target_template_id) WHERE state NOT IN ('READY','FAILED');
