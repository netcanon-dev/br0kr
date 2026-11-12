# Runbook: VM access (cloud-init) — give an agent a way INTO the VM it leases

br0kr reserves capacity and provisions a real, fenced VM. By default that VM is a bare shell with no way
in. This runbook turns on the **cloud-init access path**: an agent passes its own SSH public key on
`request_lease`/`schedule_lease`, br0kr clones a cloud-init template, injects the key, starts the guest,
and reports its IP — so the agent can `ssh` in with its own private key.

It is **off by default** (no template configured → today's create-blank shell, unchanged) and **opt-in
per request** (no `ssh_pubkey` → a fenced shell). Nothing here touches the admission/double-allocation
path: the cloud-init fields debit no capacity scope.

See also: [docker-deploy.md](docker-deploy.md) (deploying the daemon + env), [../architecture.md](../architecture.md#lease-lifecycle)
(lease lifecycle), [../designs/runtime.md](../designs/runtime.md#2-provisioning-and-teardown) (provisioning), [../../SECURITY.md](../../SECURITY.md) (the trust model + residuals).

---

## 1. The access model (what the broker does and does NOT hold)

- The agent supplies its **own SSH _public_ key**. The broker injects it via cloud-init `sshkeys`. The
  agent keeps the matching private key. **br0kr never mints, stores, or returns a private key or any
  secret** — the public key is public, safe to hold in the ledger and hand to Proxmox.
- The guest's IP is **discovered**, not assumed: after the guest boots, the QEMU guest agent reports its
  interfaces and the daemon stamps the reachable IPv4 onto the lease. `lease_status` returns it.
- Provisioning is by **clone from a template** you prepare once. The template carries the OS, cloud-init,
  and the guest agent; per lease the broker clones it, sets the cloud-init + the per-lease fence, and
  starts it.

---

## 2. Prepare a cloud-init template (operator, once per cluster)

The feature needs ONE template VM with: a bootable cloud image, a cloud-init drive, and
`qemu-guest-agent` installed (so the IP can be read back). Example on a Proxmox node, using a throwaway
template vmid (here `9000` — pick one outside your real VMs):

```bash
# 1. Fetch a cloud image (Debian 12 genericcloud shown — Ubuntu cloud images work too).
cd /var/lib/vz/template/iso 2>/dev/null || cd /tmp
wget -O debian-12-cloud.qcow2 \
  https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2

# 2. Bake qemu-guest-agent INTO the image so the broker can read the guest IP back. --install is the
#    load-bearing step (on Debian/Ubuntu the agent unit is DEVICE-activated, so install alone yields a running
#    agent); the enable is best-effort insurance (`|| true` so an image whose unit has no [Install] section
#    can't fail the bake). Needs libguestfs-tools: `apt-get install -y libguestfs-tools`.
virt-customize -a debian-12-cloud.qcow2 --install qemu-guest-agent \
  --run-command 'systemctl enable qemu-guest-agent 2>/dev/null || true'
#    (scripts/bake_template_agent.sh does exactly this — copy it onto the node if you have the repo there.)

# 3. Create the template VM, attach the disk + a cloud-init drive, enable the agent.
qm create 9000 --name br0kr-template --memory 1024 --cores 1 --net0 virtio,bridge=vmbr0 --agent enabled=1
qm importdisk 9000 debian-12-cloud.qcow2 local-zfs           # -> imports as an unused disk
qm set 9000 --scsihw virtio-scsi-pci --scsi0 local-zfs:vm-9000-disk-0
qm set 9000 --ide2 local-zfs:cloudinit                       # the cloud-init drive (REQUIRED)
qm set 9000 --boot order=scsi0 --serial0 socket --vga serial0

# 4. (RECOMMENDED) tag the template so the orphan sweep can reclaim an abandoned clone.
qm set 9000 --tags br0kr-template

# 5. Convert to a template.
qm template 9000
```

**Sanity-check the result** with `qm config 9000` before you rely on it — it should show `template: 1`,
`agent: enabled=1`, an `ide2: …cloudinit` drive, a `scsi0:` disk on your intended pool, and `tags:
br0kr-template`. A missing `cloudinit` drive means clones get no ssh key/IP config; a missing `agent: enabled=1`
means the broker never asks the guest for its IP; a missing tag only weakens the crash-window sweep (§5).
(§2b then proves the *in-guest* agent actually runs — a config that looks right can still ship a dead agent.)

Why the `br0kr-template` tag matters: a clone INHERITS the template's tags. If the daemon dies in the
narrow window between cloning and applying the per-lease fence, the orphan sweep finds the abandoned clone
by this marker and reclaims it (gated on the ledger — see §5). Without the tag, normal operation is
unaffected, but that crash-window clone would have to be cleaned up by hand.

### 2b. VERIFY the template's guest agent (preflight — strongly recommended)

The guest agent running in-guest is **load-bearing**: it is the only way the broker learns the guest's IP, so
a template whose agent isn't running yields ACTIVE leases with a null `ip_address` that are eventually reaped.
Prove it before you put the template into service — `scripts/live_verify_template_agent.py` clones the template
to a throwaway `99xx` guest much as the daemon would, boots it, confirms the agent **answers**
`network-get-interfaces` (and reports a usable IP), then tears the clone down under its fence:

```bash
# Mint an EPHEMERAL PVE API token first; revoke it after. Never commit/print the secret.
export PVE_HOST=<node-ip> PVE_NODE=<node> PVE_TOKEN_ID='user@pam!ephem' PVE_TOKEN_SECRET=<secret>
export BR0KR_LIVE_TEMPLATE=9000 BR0KR_LIVE_VMID=9901     # the clone vmid MUST be >= 9900 AND != the template's vmid
python3 scripts/live_verify_template_agent.py            # -> GO (agent_running + agent_reports_ip) or NO-GO
```

The **ephemeral token** needs the same clone → configure → start → agent-query → destroy surface the
production daemon uses, i.e. the four-ACL provisioning set from
[docker-deploy.md](docker-deploy.md#7-operating) §7 (`PVEVMAdmin` on `/vms`, `PVEDatastoreUser` on
`/storage/<pool>`, `PVESDNUser` on `/sdn/zones/localnetwork`, `PVEAuditor` on `/nodes/<node>`) — a token
missing `Datastore.AllocateSpace` or `SDN.Use` fails the clone with a `403` before the agent is ever tested.
Run the script from any host with this repo + `python3` (stdlib only) that can reach the PVE API on `:8006`
(the deploy host qualifies). Afterwards **revoke the ephemeral token** and confirm the throwaway vmid is gone
(`qm status <BR0KR_LIVE_VMID>` → does not exist — the script tears its clone down under a fence, but verify).

A `NO-GO` on `agent_running` means the bake (step 2) didn't take — the guest booted but `qemu-guest-agent`
isn't running; fix the template before using it. A `NO-GO` on `agent_reports_ip` with `agent_running` PASS
means the agent is fine but the clone got no IP — a no-DHCP bridge; set `BR0KR_LIVE_IP_CONFIG` to a free static
(the template needs a cloud-init drive for it to take effect).

**This applies to catalog-DOWNLOADED templates too.** The operator download path (`request_template_build`)
imports the source image **as-is** — it does NOT bake the agent — so a downloaded template inherits any missing
agent and produces the same null-IP failure. Verify a freshly-built catalog template the same way, with
`BR0KR_LIVE_TEMPLATE=<its vmid>`; if it fails, its **source image** needs the agent baked in before download.

**Run the verify only when NO daemon is actively serving/sweeping this node.** The throwaway clone carries a
synthetic fence id with no ledger row, so a concurrent orphan sweep could either spare production orphans while
it runs or (on a mature ledger) tear the verify's clone down mid-run. *In a fresh bring-up this means: verify
the template BEFORE you register its node in the ledger* — an unregistered node is in no daemon's registry, so
nothing sweeps it — or temporarily stop the `daemon` service for the test.

---

## 3. Turn it on

**Register the template in the catalog** (the supported path, and the only one on a fleet with more than one
node or cluster): `admin.create_template` / the dashboard *OS templates* panel with a `template_id`, after
`validate_template` (see [docker-deploy.md](docker-deploy.md), "Template (catalog)"). Agents then pass that
`template_id` per lease, and the placement gate picks the template instance and clone pool for the lease's node.

The daemon env below tunes how clones are made. Under the bundled Compose stack each one is named in
`docker-compose.yml` (the daemon service, or the shared `x-broker-env` block for knobs), so a value in `.env`
takes effect; `tests/unit/test_env_contract.py` keeps that true.

| env | meaning | default |
|-----|---------|---------|
| `BR0KR_VM_TEMPLATE` | **single-node legacy default:** the vmid to clone when a lease names no catalog template. **Unset = off** (create-blank shell). On a fleet with more than one node or cluster such a lease is HELD (`env_template_wrong_cluster`) instead of cloning a vmid that may not exist where it lands. | unset |
| `BR0KR_VM_FULL_CLONE` | `0` = faster/lighter **linked** clones (needs a clone-capable storage); else full. A knob. | `1` (full) |
| `BR0KR_CI_USER` | default cloud-init login user when the agent omits `ci_user` | `br0kr` |
| `BR0KR_GUEST_IP_PREFIX` | prefer this IPv4 prefix (e.g. `192.0.2.`) over a virtual/bridge address. A knob. | unset |
| `BR0KR_IP_DISCOVER_LIMIT` | max ACTIVE cloud-init leases polled for an IP per tick | `16` |

The clone's disk pool is resolved per lease by the placement gate (the retired `BR0KR_VM_CLONE_STORAGE` is
logged and ignored if still set). If a lease requests a key but names no template and `BR0KR_VM_TEMPLATE` is
unset, the daemon logs a loud warning and provisions a fenced shell WITHOUT injected access (its `ip_address`
stays null).

---

## 4. Use it (agent)

Pass your own SSH public key; optionally a `ci_user` and a static `ip_config` (default DHCP):

```jsonc
// request_lease (or schedule_lease)
{ "idem_key": "job-42", "node_id": "pve1", "req_cpu": 2, "req_mem_mb": 2048,
  "ssh_pubkey": "ssh-ed25519 AAAA... you@host", "ci_user": "runner" }
// -> { "lease_id": 7, "admitted": true, ... }
```

Then wait until the guest has booted and the agent reports its IP. `wait_for_lease` does the waiting on the
server (`timeout_s`, default 30, at most 60 a call; call it again on `reason: wait_timeout`), so the agent needs
no poll loop of its own:

```jsonc
// wait_for_lease(7)
{ "ok": true, "lease_id": 7, "state": "ACTIVE", "ip_address": "192.0.2.214", "ci_user": "runner",
  "vmid": 9901, "node_id": "pve1", "exists": true, "reached": true, "waited_s": 24.0 }
// agent:  ssh runner@192.0.2.214   (with its own private key)
```

`ip_address` is `null` until the guest boots and cloud-init starts the agent (poll a few times); it stays
`null` for a lease that requested no cloud-init. A malformed key/user/ip is a hard rejection
(`ssh_pubkey_malformed`, `cloud_init_requires_pubkey`, `ip_config_malformed`, …). Heartbeat with
`renew_lease` and `release_lease` when done — release tears the guest down under its fence.

---

## 4b. Provision from the dashboard (operator, act-as-project)

An operator can create a VM straight from the dashboard instead of an MCP client — the SAME admission path,
with the operator choosing which project to charge. With `BR0KR_DASHBOARD_TOKEN` set and unlocked, the
Active-leases panel gains a **+ create VM** button:

- **Acting as project** — the tenant whose quota the VM debits (required; a GUI-created lease is always
  governed + attributable, like every agent lease).
- **Placement** — a specific node, a cluster (broker picks a node), or *anywhere*.
- **Template** — the images that project may use (public + granted); a forbidden pick is refused
  `template_forbidden`, exactly as for an agent.
- **Size / disk / lifetime / access** — cpu, memory, an optional storage pool + disk (grows the template's
  boot disk), a TTL, and the same `ssh_pubkey`/`ci_user`/`ip_config` fields as the agent path (§4).
- On submit the modal watches the lease boot and shows the `ssh <user>@<ip>` line once the guest reports its
  IP via qemu-guest-agent. **A GUI-created lease has no heartbeating agent**, so it is DESTROYED at its TTL
  unless you **renew** it (per-row *renew*, or *renew* in the lease-detail modal).

The create debits the chosen project's real quota through `admit()` (it can't over-provision), and every
create is written to the audit trail stamped with the acted-as project. It grants the operator nothing they
couldn't already do by minting a project token and provisioning as it — it just collapses that to one audited
click. See SECURITY.md (dashboard write-surface control) and `br0kr/actas.py`.

## 5. Safety model (how a clone can't leak or be wrongly destroyed)

- **Per-lease fence.** The clone is tagged `br0kr-lease-<id>` in the same config call as the cloud-init,
  so it is fenced the instant `configure` returns. Teardown only deletes a guest whose fence matches the
  lease (a reused vmid is refused) — same guarantee as the create path.
- **The unfenced window.** Between `clone` and `configure` the guest carries only the inherited
  `br0kr-template` marker, no fence. If the daemon dies there, the orphan sweep's clone-orphan reclaim
  handles it — but **only on positive ledger evidence**: it reclaims a marker-only, unfenced clone ONLY
  when a TERMINAL lease holds that vmid and no live lease does (`lease.clone_orphan_reclaimable`). So a
  clone whose lease is still LIVE (e.g. a clone whose copy timed out and left the lease PROVISIONING) is
  SPARED, never deleted out from under a live lease; an absent/empty ledger (a wrong `PGCONN`) yields no
  evidence and reclaims nothing. The template itself (`template=1`) is never touched.
- **No host overdraw.** While a clone is in flight or timed-out-PROVISIONING, its lease still holds the
  capacity in the ledger — so the broker's accounting never under-counts the host, and the guest is
  reclaimed once its lease goes terminal. A lease cannot stay PROVISIONING forever: after
  `provision_stuck_seconds` (operator setting, default 20 min) the reaper reclaims it as
  `provision_stuck`, even if the agent keeps renewing it.

---

## 6. Troubleshooting

| symptom | likely cause |
|---------|--------------|
| `ip_address` stays null on an ACTIVE lease | the guest agent isn't running — template missing `qemu-guest-agent`, or `--agent enabled=1` not set; or still booting (poll longer) |
| `lease_status` shows ACTIVE but you can't ssh | cloud-init still finalizing the user/key (retry); or the wrong `ci_user`; or the guest's network (`ip_config`) is unreachable from you |
| a key request provisioned a keyless shell | `BR0KR_VM_TEMPLATE` is unset on the daemon (see the warning in its log) |
| a stray VM left after a daemon crash mid-clone | the template wasn't tagged `br0kr-template`; reclaim it by hand, then add the tag |
| clone fails immediately | linked clone (`BR0KR_VM_FULL_CLONE=0`) on a storage that can't linked-clone — use full, or a zfs/lvm-thin/qcow2 storage |
| lease ends with `released_reason` `provision_acl_denied` | the broker's PVE token lacks a privilege for the clone (most often `Datastore.AllocateSpace` on the target pool or `SDN.Use` on the bridge's zone); the daemon's ERROR line quotes PVE's privilege. Grant the four ACLs in [docker-deploy.md §7](docker-deploy.md#7-operating) |
| lease ends with `released_reason` `provision_stuck` | the lease stayed PROVISIONING past `provision_stuck_seconds` (default 20 min) while you kept renewing it: the clone or start task outlived its wait budget, or the ledger blipped right after the clone (if you stopped renewing, the same case usually ends `expired` first). Check the daemon log for that lease id and PVE's task log for the clone; request a new lease once the node is healthy |
| lease ends with `released_reason` `template_vmid_missing` | the catalog template's vmid does not exist on the node that clones it (deleted, renumbered, or never replicated there); re-point the catalog entry or rebuild the template |

## See also
- [first-vm.md](first-vm.md) — the whole first-VM path in one runbook, this template step included
- [connecting-an-agent.md](connecting-an-agent.md) — pointing an MCP client at the gateway + minting the token, the step before this one
- [operator-tier.md](operator-tier.md) — the cross-tenant operator token (cordon + migrate + fleet observability)
- [/README.md](../../README.md) — the front door: install and run, the feature table
