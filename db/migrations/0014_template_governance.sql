-- 0014_template_governance — per-project image governance: a `public` flag + a project↔template
-- allowlist (phase A3).
--
-- Phase A2 (0013) let any agent SELECT any registered catalog template per request — there was no
-- per-project image governance: a `consume` token could clone ANY image an operator had registered. This
-- migration adds the missing policy layer so an operator can keep a template PRIVATE and grant it to
-- specific tenants, WITHOUT touching the race-critical admission floor — governance is a PURE RESTRICTION
-- enforced ABOVE admission (br0kr/auth.template_allowed, called by the MCP gateway before admit()), exactly
-- like the bind_project tenant binding. It debits no scope and can only ever REFUSE a request, never widen
-- one, so the node/storage/project overprovision guarantees are untouched.
--
-- 1. template.public — is this catalog image usable by EVERY tenant?
--    DEFAULT true, DELIBERATELY: this is opt-in governance. Every existing template (and every new one an
--    operator forgets to mark) stays visible+usable exactly as in A2 — A3 changes NO behaviour until an
--    operator marks a template non-public. A public template is always allowed (and listed to every token);
--    a non-public one needs a project_template grant to the requesting tenant or the request is refused
--    `template_forbidden`. NOT NULL so the gate's `public OR granted` test is never a three-valued NULL.
--
-- 2. project_template — the (project, template) allowlist. A row GRANTS one tenant the right to select one
--    non-public template. A pure many-to-many join: PK(project_id, template_id) makes a grant idempotent
--    (re-granting is a no-op / ON CONFLICT) and the lookup an index hit. BOTH FKs are ON DELETE CASCADE,
--    DELIBERATELY — a grant is DISPOSABLE POLICY metadata, not capacity or audit history: if the project or
--    the template is removed, the allowlist row is meaningless and should vanish with it. This is the
--    api_token / template-catalog reasoning (a pointer, CASCADE), explicitly DISTINCT from lease.template_id
--    (0013), which is NO ACTION fail-CLOSED because erasing it would mis-provision a LIVE lease. A grant
--    keys NO lease and gates only NEW requests, so cascading it away can never affect a running lease (the
--    lease already carries its own template_id); and admin.delete_template still refuses `template_in_use`
--    while any lease references the template, so this CASCADE only ever fires for a grant whose template has
--    no leases — exactly when dropping the grant is correct.
--
-- Config-as-data, operator-managed (br0kr/admin: the `public` flag on create/set_template +
-- grant/revoke_template; the dashboard's token-gated CRUD). Idempotent (ADD COLUMN / CREATE TABLE/INDEX
-- IF NOT EXISTS).
ALTER TABLE template ADD COLUMN IF NOT EXISTS public boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS project_template (
  project_id  text NOT NULL REFERENCES project(id)  ON DELETE CASCADE,
  template_id text NOT NULL REFERENCES template(id) ON DELETE CASCADE,
  granted_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, template_id)
);
-- Speeds the per-template grant listing (the dashboard's "who can use this image?" view); the per-project
-- direction is served by the PK's leading column.
CREATE INDEX IF NOT EXISTS project_template_template ON project_template(template_id);
