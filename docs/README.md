# docs/ — documentation map

This directory holds everything above the source-docstring altitude. It is
organised into stable tiers so a contributor knows where a given kind of
document belongs (and where to look).

## The tree

```
docs/
├── README.md                  # this map
├── standards/                 # BINDING doctrine — stable rules, change rarely
│   ├── documentation.md       # docstrings, the doc tiers, drift prevention
│   ├── testing.md             # the test pyramid, test-docs, the test-after-change loop
│   ├── logging.md             # structured logging, levels, secret redaction
│   ├── ci-cd.md               # the CI workflows + workflow hardening
│   └── security-and-hygiene.md  # secret/PII hygiene (the controls live in /SECURITY.md)
├── architecture.md            # PLAN-OF-RECORD for the whole broker (status-legended)
├── pdm-api-contract.md        # the assumed Proxmox Datacenter Manager API (unverified)
├── designs/                   # per-feature design specs (status-legended)
├── runbooks/                  # operator procedures (deploy, HA, connect an agent, …)
└── reviews/                   # FROZEN evidence trail from reviews/audits
    └── <UTC-date>-<slug>.md   # one file per review run; EXPECTED-STALE by design
```

## What each tier is for

| Tier | Lifecycle | Contents |
|------|-----------|----------|
| **`standards/`** | Stable — changes are deliberate | Binding rules every contributor follows. The "law" of the repo. |
| **`architecture.md`, `designs/*.md`** | Living — tracks reality | Plan-of-record for the broker or one feature: the design, the *why*, the rollout. Carries a status legend (⬜ todo · 🟡 partial · ✅ done) and a `## See also` footer. |
| **`reviews/<date>-<slug>.md`** | **Frozen** | The dated, reproducible record of a review/audit run. **EXPECTED-STALE:** a later audit must never flag a past run's evidence as drift — it is a historical record, not a live doc. |
| **`runbooks/`** | Living — operational | Step-by-step operator procedures (HA, failover, restore). Practiced, not aspirational. e.g. [runbooks/ledger-ha.md](runbooks/ledger-ha.md). |

## Conventions

- **Every standards/architecture doc ends with a `## See also`** linking its
  peers — and the link is reciprocal (added in the same commit). One-way links
  rot fastest.
- **Plan-of-record docs carry a status legend** and annotate sections with
  their state + the date they shipped. Honest calibration beats aspiration.
- **`reviews/` files are named by UTC date** (matching commit/PR timestamps)
  with a topical slug, and are immutable once written.
- **Internal directory READMEs** (one per meaningful source dir) are NOT under
  `docs/` — they live next to the code they describe. See
  [standards/documentation.md](standards/documentation.md) §1b.

## See also
- [runbooks/connecting-an-agent.md](runbooks/connecting-an-agent.md) — pointing an agent's MCP client at the gateway (mint a token, pick a transport, paste the config)
- [runbooks/oauth-connector.md](runbooks/oauth-connector.md) — connecting an OAuth-only MCP host (Claude Desktop / claude.ai custom connector) via the OAuth shim
- [runbooks/public-tunnel.md](runbooks/public-tunnel.md) — making br0kr reachable from the cloud (a named Cloudflare Tunnel) when it's on a private LAN IP
- [runbooks/docker-deploy.md](runbooks/docker-deploy.md) — deploying the whole stack with Docker Compose
- [runbooks/ledger-ha.md](runbooks/ledger-ha.md) — operating the ledger under high availability
- [runbooks/cross-cluster-migration.md](runbooks/cross-cluster-migration.md) — relocating a lease to another cluster, race-free
- [runbooks/operator-tier.md](runbooks/operator-tier.md) — minting/using the cross-tenant `operator` MCP token (cordon + migrate + fleet observability)
- [runbooks/vm-access.md](runbooks/vm-access.md) — handing an agent SSH access to its VM via cloud-init (its own public key)
- [architecture.md](architecture.md) — the broker's plan of record
- [standards/documentation.md](standards/documentation.md) — the full doctrine
- [/README.md](../README.md) — the external front door
- [/CLAUDE.md](../CLAUDE.md) — contributor directives + the doc-sync checklist
