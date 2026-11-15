-- 0055_lease_ownership — which leases br0kr built, so the guests it did not build are never destroyed
-- (fleet onboarding, phase 1: the never-destroy floor). Design: docs/designs/fleet-onboarding.md.
--
-- Every lease so far is a guest br0kr cloned (or a migration's or replication's placeholder reservation), and every
-- teardown path assumes it may stop and delete the lease's guest. Bringing existing guests under br0kr adds two
-- kinds of row that must never reach a teardown:
--   * adopted       — a guest br0kr did not build, recorded with its vmid and fence tag (phase 5). ACTIVE while
--                     recorded, RELEASED once detached; never RELEASING, so no reaper step can claim it.
--   * capacity_only — memory and CPU held for a guest br0kr does not manage at all (a container, the ledger VM
--                     itself, a failover hold). ADMITTED with NO vmid, the same shape as a migration reservation:
--                     nothing can name a guest from it. `observed_vmid` and `label` say what it stands for; they
--                     are informational and no code destroys by them.
-- Everything else is `provisioned` (the default), so this migration changes no existing row.
--
-- The CHECKs make the shapes schema facts, so a path that ever tried to break one fails its UPDATE and the lease
-- stays where it was (fail closed):
--   * lease_adopted_states         — an adopted lease is ACTIVE or RELEASED, never ADMITTED/PROVISIONING/RELEASING.
--   * lease_capacity_only_no_guest — a capacity-only lease has no vmid and is ADMITTED (with a node) or RELEASED;
--                                    never RELEASING, so the reaper cannot end one and free its capacity. A record
--                                    closes ADMITTED -> RELEASED in one CAS (phase 2), and a closed one may lose
--                                    its node like any terminal lease (admin.delete_node force, as 0017 allows).
--   * lease_foreign_pinned         — a lease br0kr did not build is pinned, so the TTL step never matches it.
--   * lease_lxc_unfenced           — a container is capacity-only until container provisioning exists (a later
--                                    migration relaxes this one CHECK; nothing else assumes a VM).
-- The code also refuses them by ownership (lease.release, claim_expired, begin_provisioning, the provision and
-- held-lease work lists, unpin, migrate, the orphan sweep), and the teardown itself reads the guest's own config
-- before a stop: Proxmox protection or a `br0kr-keep` tag refuses it even when the ledger has lost the row.
--
-- One partial unique index: at most one live capacity-only record per (node, observed vmid), so the same guest is
-- not counted twice on a node. The ledger VM's record on its home node and its failover hold on another node are
-- on different nodes, so both fit.
--
-- The ADD COLUMNs have constant defaults (catalog-only in Postgres 11+, no rewrite). The CHECKs validate existing
-- rows, all of which are provisioned QEMU leases and pass. Idempotent (IF NOT EXISTS / duplicate_object); a schema
-- change is a new file, never an edit of a shipped one.
--
-- lock_timeout: ALTER TABLE takes a brief ACCESS EXCLUSIVE lock on lease; fail fast behind a long transaction
-- instead of queueing every lease access, and the next deploy retries (as in 0048).
SET LOCAL lock_timeout = '10s';

ALTER TABLE lease ADD COLUMN IF NOT EXISTS ownership     text NOT NULL DEFAULT 'provisioned';
ALTER TABLE lease ADD COLUMN IF NOT EXISTS guest_kind    text NOT NULL DEFAULT 'qemu';
ALTER TABLE lease ADD COLUMN IF NOT EXISTS observed_vmid integer;
ALTER TABLE lease ADD COLUMN IF NOT EXISTS label         text;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_ownership_known
    CHECK (ownership IN ('provisioned', 'adopted', 'capacity_only'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_guest_kind_known CHECK (guest_kind IN ('qemu', 'lxc'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_adopted_states
    CHECK (ownership <> 'adopted' OR state IN ('ACTIVE', 'RELEASED'));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_capacity_only_no_guest
    CHECK (ownership <> 'capacity_only'
           OR (vmid IS NULL AND state IN ('ADMITTED', 'RELEASED') AND (node_id IS NOT NULL OR state = 'RELEASED')));
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_foreign_pinned CHECK (ownership = 'provisioned' OR pinned);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_lxc_unfenced CHECK (guest_kind = 'qemu' OR ownership = 'capacity_only');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_label_bounded CHECK (label IS NULL OR char_length(label) <= 200);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS lease_capacity_only_observed_uniq ON lease (node_id, observed_vmid)
  WHERE ownership = 'capacity_only' AND observed_vmid IS NOT NULL AND state = 'ADMITTED';
