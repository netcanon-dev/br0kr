-- 0010_cloud_init — per-lease VM-access fields: the agent's SSH key + the discovered guest IP.
--
-- The broker reserves + fences a real VM, but until now nothing let an agent ACTUALLY REACH it: the
-- request surface carried no key, and lease_status returned no address. These four columns close that
-- "last mile" WITHOUT touching the race-critical capacity path — they are pure passthrough/return data,
-- never an admission scope (cloud-init can't change how much CPU/mem/disk a lease debits), so the
-- double-allocation safety proof is unaffected.
--
--   ssh_pubkey  — the agent's OWN ssh PUBLIC key, injected into the guest via cloud-init `sshkeys`.
--                 PUBLIC, so it is safe to store in the ledger and hand to Proxmox; the broker NEVER
--                 holds or returns a private key (SECURITY.md: never store/log a secret). NULL = the
--                 agent didn't ask for cloud-init access → today's exact behaviour (a fenced shell).
--   ci_user     — the cloud-init username to create (NULL → the daemon's default at provision time).
--   ip_config   — an optional Proxmox `ipconfig0` value (e.g. 'ip=dhcp' or 'ip=10.0.0.5/24,gw=10.0.0.1');
--                 NULL → the daemon defaults to DHCP. Only honoured alongside ssh_pubkey.
--   ip_address  — the guest IP the QEMU guest agent reported, stamped by the daemon AFTER the guest boots
--                 (lease.set_ip_address, an ACTIVE-only CAS) and returned by lease_status so the agent can
--                 connect. NULL until discovered (or forever, if the agent didn't request cloud-init).
--
-- Validation lives in br0kr/cloudinit.py + admission.validate (config-as-data, validated): these CHECKs
-- are only a defence-in-depth LENGTH cap so a malformed/oversized value can't bloat the row or a config
-- string. Idempotent (ADD COLUMN IF NOT EXISTS; the CHECKs guarded for re-apply).
ALTER TABLE lease ADD COLUMN IF NOT EXISTS ssh_pubkey text;
ALTER TABLE lease ADD COLUMN IF NOT EXISTS ci_user    text;
ALTER TABLE lease ADD COLUMN IF NOT EXISTS ip_config  text;
ALTER TABLE lease ADD COLUMN IF NOT EXISTS ip_address text;

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_ssh_pubkey_len CHECK (ssh_pubkey IS NULL OR length(ssh_pubkey) <= 4096);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_ci_user_len    CHECK (ci_user    IS NULL OR length(ci_user)    <= 64);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_ip_config_len  CHECK (ip_config  IS NULL OR length(ip_config)  <= 256);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_ip_address_len CHECK (ip_address IS NULL OR length(ip_address) <= 64);
EXCEPTION WHEN duplicate_object THEN null; END $$;
