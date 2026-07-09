# The demo estate — "Meridian Retail"

A fictional **mid-sized online retailer with an in-house payments platform**,
monitored by a single Checkmk site (`monitoring/prod`). Every host here is
fake (stdlib-only Python — see `CLAUDE.md`): the servers impersonate a Checkmk
agent over plaintext TCP, the network gear is simulated as **stored SNMP
walks** (`snmp/`, rule "Simulating SNMP by using a stored SNMP walk") — so
the whole "company" can be staged on one laptop or VM. The estate is deliberately
**mostly green**: real infrastructure is mostly fine, and a wall of green with
one or two reds tells a single clean story (the storyline rule from
`CLAUDE.md`).

The two original hosts (`payment-api`, `db-postgres-01`) anchor the platform;
the rest fill in the tiers a real shop would run so the picture reads as a
genuine company, not a pile of unrelated test boxes.

## Roster

| Host | Tier | OS / role | Demo state | Incident story |
|---|---|---|---|---|
| `core-gw-01` | network | Ubuntu 24.04 · edge gateway/router (VRRP active) | **steady green** | background — routes the estate to the ISP; top of the parent topology |
| `leaf-sw-01` | network | Cumulus Linux 5.9 · whitebox ToR access switch | **steady green** | background — every server hangs off its swp ports; parent of the whole rack |
| `sw-core-01` | network | Cisco Catalyst 9300 campus core switch · **SNMP** | **steady green** | background — the office/campus backbone, 12 × 10G *(via stored SNMP walk, `snmp/`)* |
| `sw-access-01` | network | Cisco Catalyst 9200 access switch · **SNMP** | incident | **CRC error storm on uplink Te1/1/1 (WARN) → link dies (CRIT)**, traffic fails over to Te1/1/2 |
| `rt-wan-01` | network | Cisco ISR 2921 warehouse WAN router · **SNMP** | incident | **WAN saturation**: runaway inventory replication ramps Gi0/1 to ~940 Mbit/s, CPU past the 80/90 defaults, output discards |
| `ups-01` | network | APC Smart-UPS 3000 · **SNMP** | **steady green** | background — battery/load/temperature corroboration |
| `web-frontend-01` | edge | Ubuntu 24.04 · nginx reverse proxy / TLS termination | **steady green** | background — the estate's front door, always healthy |
| `payment-api` | app | Ubuntu 24.04 · gunicorn + nginx + redis client | incident | HTTP 503 symptom + failed `payment-worker.service` root cause *(`hosts/payment-api/`)* |
| `app-worker-01` | app | Ubuntu 24.04 · Java settlement/order worker | incident | **memory leak → swap thrash → OOM kill → service flap** (a *real* resource exhaustion, the mirror image of the dying-disk fake load) |
| `app-redis-01` | app | Ubuntu 24.04 · Redis 7 session + cache store | incident | **maxmemory eviction storm**: a bad TTL deploy floods memory, evictions spike, hit-ratio collapses, clients block |
| `db-postgres-01` | data | Ubuntu 24.04 · PostgreSQL 16 primary | incident | "Explain with AI": CPU-load page that is really a **fail-slow dying SSD** *(`hosts/db-postgres-01/`)* |
| `db-postgres-02` | data | Ubuntu 24.04 · PostgreSQL 16 read replica | incident | **connection-pool exhaustion**: a runaway BI/reporting job opens connections until `postgres_connections` approaches `max_connections` (the one postgres check that alerts by default: 80 %/90 %) |
| `mail-relay-01` | infra | Ubuntu 24.04 · Postfix transactional mail relay | incident | **mail queue backlog**: a downstream MX goes unreachable, the deferred queue piles up |
| `fileserver-01` | infra | Ubuntu 24.04 · Samba/NFS shared storage | incident | **filesystem filling**: a runaway log/upload spool grows until df magnitude + trend cross the levels (predictive) |
| `backup-01` | infra | Ubuntu 24.04 · restic backup host | **steady green** | background — last nightly backup job OK (`mk_job`) |
| `win-dc-01` | infra | Windows Server 2022 · Active Directory DC | incident | **C: drive filling + a stopped critical service** — the one Windows host, different agent format |

## Port map (publish on `127.0.0.1`)

Each host listens on a distinct pair so the whole estate can run at once.
(`6556` is usually the demo laptop's *own* agent — left free.)

| Host | agent TCP | admin/HTTP |
|---|---|---|
| `payment-api` *(existing)* | 6557 | 8080 |
| `db-postgres-01` *(existing)* | 6557¹ | 8081 |
| `web-frontend-01` | 6560 | 8090 |
| `app-redis-01` | 6561 | 8091 |
| `app-worker-01` | 6562 | 8092 |
| `fileserver-01` | 6563 | 8093 |
| `mail-relay-01` | 6564 | 8094 |
| `db-postgres-02` | 6565 | 8095 |
| `backup-01` | 6566 | 8096 |
| `win-dc-01` | 6567 | 8097 |
| `core-gw-01` | 6568 | 8098 |
| `leaf-sw-01` | 6569 | 8100² |
| SNMP devices (`snmp/netsim.py`) | —³ | 8101 |

¹ the two original demos both publish 6557 — run one at a time, or re-map. The
new hosts use 6560–6567 so they never collide with each other or the originals.
² 8099 is taken by the piggyback delivery control panel.
³ the SNMP devices (`sw-core-01`, `sw-access-01`, `rt-wan-01`, `ups-01`) have
no agent port at all: one daemon (`snmp/netsim.py`) renders stored SNMP walks straight into the
site (`~/var/check_mk/snmpwalks/`) and Checkmk reads them via the
`usewalk_hosts` rule — see `snmp/README.md`. All four share the one control
panel on 8101.

## Topology (parents + BI)

The estate has an explicit network path for RCA to reason over:

```
core-gw-01  (gateway/router — no parent)
  ├─ leaf-sw-01  (ToR access switch)
  │    └─ every other host (servers, win-dc-01, ...)
  └─ sw-core-01  (campus core switch, SNMP)
       ├─ sw-access-01  (office access switch, SNMP)
       ├─ rt-wan-01     (warehouse WAN router, SNMP)
       └─ ups-01        (rack UPS, SNMP)
```

The parent relations are declared in the piggyback registry
(`deploy/piggyback/serve.py`) and applied as the Checkmk `parents` host
attribute by `deploy/cmk_setup.py` — which also creates a **BI pack**
("Payments platform": network path → customer entry → payment API →
processing/cache → data layer → storage) plus a `check_bi_aggr` active check
on the delivery shell, so the business service pages when any tier goes red.

## Running the estate

**The one-stop shop is `./estate.py`** (repo root): it starts the simulators,
sets up the Checkmk site (folder, hosts, rules, parents, BI pack, discovery,
activation) and tears everything down again — with `--scale
minimal|standard|full` and `--replicas N` to stamp out steady-green copies of
every replicable host class (web-frontend-02 ..., extra SNMP access
switches). The pieces below are what it orchestrates and remain usable alone.

## Two ways to run the agent hosts

1. **Per-host TCP** (the default): bring up each host's own
   `docker compose`, add each in Checkmk as a TCP host with its agent-port
   override. Independent, copy-pasteable.
2. **One piggyback delivery host** (`deploy/piggyback/`): a single "shell"
   host carries the *whole* estate as piggyback. One container runs the shell
   plus every host's `serve.py` internally; Checkmk polls only the delivery
   shell (agent **6559**, control panel **8099**) and the estate hosts are added
   as **piggyback** hosts — no per-host agent port. The shell emits only a
   minimal agent section. The site setup is one command as well:
   `deploy/cmk_setup.py` (REST API — folder, hosts, rules,
   discovery, activate; idempotent, `--remove` to tear down). See
   `deploy/piggyback/README.md`.

| Host | agent TCP | admin/HTTP |
|---|---|---|
| `cmk-demo-gateway.corp.meridian-retail.com` (piggyback delivery shell) | 6559 | 8099 |

## Conventions every host follows

- **FQDN host names.** Every host is monitored in Checkmk under its fully
  qualified name `<short>.corp.meridian-retail.com` (the AD/DNS domain implied by
  `win-dc-01`). The short label (`web-frontend-01`, …) used in the tables above is
  just the internal handle for ports, the control panel, and `ESTATE_HOSTS`
  selection. Override the domain with `ESTATE_DOMAIN` (piggyback) or `CMK_HOSTNAME`
  (per host). In piggyback mode the FQDN is the piggyback marker, so add each
  estate host in Checkmk under its FQDN.
- **Plaintext TCP agent** + the section-by-section parity rules in `CLAUDE.md`
  (full `<<<check_mk>>>` header, controller-status pretending TLS registration,
  deployed-plugin list, both `lnx_if` variants, `df_v2`, full `/proc/meminfo`,
  `systemd_units` with ~30 units, `timesyncd` with dynamic timestamps, `apt`
  sentinel, `mounts`). The SNMP devices are the exception: no agent, no port —
  one daemon (`snmp/netsim.py`) rewrites stored SNMP walks into the site
  every 30 s and Checkmk's StoredWalk backend re-reads them each poll.
- **Monotonic state-aware counters** (`Counter`), **autocorrelated gauges**
  (`gauge`/`_Wobble`), and **restart-persisted state** (`STATE_FILE`) — copied
  from `hosts/db-postgres-01/serve.py`, the reference implementation.
- **Incident hosts** carry a `/admin` control UI (state badge, time-in-state,
  per-state effect cards, toggle buttons, 5 s auto-refresh) + a curl API, plus
  a `degraded`/`broken` escalation where the timeline matters.
- **Background hosts** are steady green: same realistic sections, no toggle.
- **Low noise, one root cause** per incident: only the symptom + root-cause
  services change; everything else stays green and *corroborates*.

See each host's own `README.md` for run instructions, the Checkmk setup, and
the demo choreography.
