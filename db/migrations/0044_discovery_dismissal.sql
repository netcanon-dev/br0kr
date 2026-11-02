-- 0044_discovery_dismissal — persist operator-DISMISSED discovery findings (PR-H of the storage/network
-- auto-discovery arc, design §9 PR-D). discover_reachability (0041/PR-B, PR-C) REPORTS drift buckets — an
-- undeclared node-local pool, a declared pair PVE omits, a stale quarantine, content drift, a missing bridge.
-- Some are KNOWN-benign and re-surface in EVERY report forever (e.g. a node's `local`/`local-zfs`/`rpool` scratch
-- pools that will never be declared shared template storage — the live PR-B verify saw 6 of these), noise that
-- buries real drift. This table lets an operator DISMISS a specific finding so it is filtered out of future
-- reports (moved to a `dismissed` roster in the report — still visible + clearable).
--
-- SAFETY (the load-bearing property): this table is READ ONLY by discover_reachability (the advisory report + the
-- 0043 daemon cadence gauge/log). NO admission / placement / verify_declarations path reads it, so a dismissal
-- CANNOT affect the gate — it is purely a REPORTING suppression, never a capability or reachability change.
-- discover_reachability still WRITES NOTHING to gate-read state (it only SELECTs this table); ONLY the operator
-- dismiss/clear verbs write here (audited). A dismissal is UN-FINGERPRINTED: it suppresses the (kind, node,
-- subject) finding until the operator clears it, even if the finding's live details later change (a deliberate
-- simplification — the stale-confirm class the design §10 calls out; the operator owns clearing a stale dismissal).
--
-- The finding IDENTITY is (kind, node_id, subject): `kind` = the drift bucket name (unknown_pool,
-- known_pool_new_node, declared_disabled, declared_absent, declared_inactive, quarantined_but_present,
-- declared_incoherent, content_report, declared_missing_bridge, network_quarantined_but_present — the same set
-- placement._SUBJECT_FIELD keys, i.e. every summable drift bucket; NOT the non-drift bridge_menu / *_nodes lists);
-- `node_id` = the node the finding is on; `subject` = the bucket's discriminator (a pve_name for unknown_pool, a
-- storage_id for the declared_* / known_pool_new_node / content_report buckets, a logical_network_id for the two
-- network buckets). UNIQUE (kind, node_id, subject) makes a re-dismiss idempotent. FK node_id -> node(id) ON
-- DELETE CASCADE so removing a node reaps its dismissals; `subject` is polymorphic (pve_name / storage_id /
-- logical_network_id), so NO FK on it. `note` is a free-text operator note — named DISTINCTLY from the verbs'
-- `reason` refusal-code return key (the established admin-verb convention) to avoid that collision.
-- `dismissed_by` is best-effort provenance: the MCP verb stamps the operator token id; the DASHBOARD route leaves
-- it NULL (a dashboard dismissal's attribution lives in its audited operator_audit row instead). Idempotent
-- (CREATE TABLE IF NOT EXISTS); a schema change is a NEW file.

CREATE TABLE IF NOT EXISTS discovery_dismissal (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,
  node_id      text NOT NULL REFERENCES node(id) ON DELETE CASCADE,
  subject      text NOT NULL,
  note         text,
  dismissed_by text,
  dismissed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, node_id, subject)
);
