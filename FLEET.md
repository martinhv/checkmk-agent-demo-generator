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
| `sw-core-01` | network | Cisco Catalyst 9300 campus core switch · **SNMP** | **steady green** | background — the estate's backbone, 12 × 10G, top of the parent topology *(via stored SNMP walk, `snmp/`)* |
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

## Company scale — the 300-host estate (`--scale company`)

`./estate.py up --site --scale company` grows the estate to **exactly 300
monitored hosts**, shaped like a real mid-sized online retailer (researched
against enterprise fleet surveys, network-architecture sizing guides and
monitoring-vendor sizing docs — see the composition rationale below). The
classic roster above stays intact and keeps ALL the incident stories; the
added ~275 hosts are **steady green** background.

| Layer | Hosts | What it is |
|---|---|---|
| Classic roster + shell | 11 | the hand-crafted incident hosts above + the delivery shell |
| Server fleet (`fleet/`) | 173 | ONE process (`fleet/serve.py`) synthesizes 124 Linux + 49 Windows hosts from declarative profiles (`fleet/profiles.py`) |
| Synthetic SNMP (`snmp/netsim.py`) | 4 | the classic network layer (core/access switch, WAN router, UPS) with its incidents |
| Walk-replay SNMP (`snmp/walklib/`) | 112 | anonymized REAL device walks replayed with live counters |

### Server fleet composition (the researched shape)

- **12 physical KVM hypervisors** (`kvm-01..12`, Dell R760, 48 cores/384 GiB)
  carrying the ~160 VMs at 13.3 VMs/host — each hypervisor's `ps` lists its
  actual guests as qemu processes (cross-checkable against the VM list).
- **Shop platform** (Linux): svc-catalog/checkout/order/account/inventory,
  api-gw, shop-search (Elasticsearch), shop-media, queue (RabbitMQ), cache
  (memcached) — a 2026-credible microservice stack around the classic
  payment-api story.
- **Kubernetes**: 3 control-plane + 18 workers (the platform under the
  microservices).
- **Back office**: ERP app+DB (MariaDB), BI (Metabase + PostgreSQL warehouse),
  WMS app+DB, staging copies of the platform (short uptimes — staging gets
  rebuilt), CI (GitLab + 6 runners + registry), dev sandboxes.
- **Shared infra** (Linux): DNS, egress proxy, LDAP, VPN, bastion, SFTP, APT
  mirror, log cluster (OpenSearch), the Checkmk server itself
  (`monitoring-01`), AWX, Vault, chrony NTP.
- **Windows (~40 %)**: 2 extra DCs, file/print, 8 RDS session hosts, 5 MSSQL,
  22 IIS LOB app servers, Dynamics BC (ERP finance), WSUS, PKI, Veeam
  (physical).
- **Warehouse edge**: 3 Linux scanner-gateway servers + 1 Windows warehouse
  control system per fulfillment center.

DB fleet hosts run their engines as *processes only* (no mk_postgres/mysql
plugin sections) — realistic for boxes where the plugin simply isn't deployed,
and it keeps the section surface honest.

### SNMP device fleet (replayed real walks, anonymized)

Curated by `snmp/curate_walks.py` from `~/git/zeug_cmk/walks` into
`snmp/walklib/` — identifying subtrees stripped (LLDP/CDP neighbors, ARP/
routes, bridge FDBs, RMON, process tables), sysName/contact/location/ifAlias/
serials/MACs rewritten, IPs remapped, org-specific tokens renamed; the string
audit (`--audit`) reviews everything that remains. netsim replays each model
per instance: own identity, advancing uptime, and interface counters whose
rate derives from the RECORDED counter over the RECORDED uptime (busy ports
stay busy, dead ports stay dead), wobbled and restart-persisted.

- **DC fabric**: 2 HP 5406R distribution (the only DC uplinks to the core),
  6 Aruba 6200F + 2 Huawei CloudEngine ToR behind them, an HP 2530-48G OOB
  management switch (`sw-dc-oob-01` — iDRACs, rack PDUs, env sensors),
  Fortigate HA pair + ASA DMZ firewall, 2 Kemp load balancers,
  internet-edge router.
- **HQ**: an HP 5406R distribution switch (`sw-hq-dist-01`) to the core, 14
  floor switches (Aruba 2930F / HP 2530) + 2 Extreme WLCs + UPS/PDUs/room
  sensor behind it; the 12 office printers (Ricoh/Canon) each hang off their
  own floor's switch.
- **Warehouses ×2**: a local CPE router each (`rt-wh1-01` / `rt-wh2-01`,
  Lancom), 5 access switches each (ProCurve/HP), 4 Zebra label printers each,
  UPS/PDU/AKCP sensor each.
- **DC power/environment**: 3 APC Symmetra UPS, 8 rack PDUs (APC NetShelter,
  Raritan, Gude), 8 environment sensors (AKCP, AVTECH).
- **Storage & OOB**: 2 Synology NAS, 2 Brocade FC SAN switches, 16 Dell iDRACs
  (one per physical box + spares), Meinberg LANTIME GPS NTP appliance.

### Topology

`sw-core-01` is the root, and its 12x10G ports are all uplink trunks — only
the distribution switches (`sw-dc-dist-01/02`, `sw-hq-dist-01`), the edge
firewalls, the WAN/internet routers and the server-access switch hang off it
(fan-out ~10, not ~90). Below that:
- **DC**: `core -> sw-dc-dist-0{1,2} -> sw-dc-tor-0N -> servers`; storage,
  load balancers and NTP on the distribution pair; iDRACs/rack-PDUs/env
  sensors on the OOB management switch (`sw-dc-oob-01`).
- **HQ**: `core -> sw-hq-dist-01 -> sw-hq-fNN -> that floor's printer`
  (+ WLCs and comms-room power on the HQ distribution).
- **Warehouses**: `printer -> hall switch -> local CPE (rt-wh{1,2}-01, only
  the 3 hall switches on its LAN ports) -> DC WAN head-end rt-wan-01 (keeps
  its saturation incident) -> core`; mezzanine switches daisy-chain off
  hall 1, comms-room UPS/PDU/sensor share hall 1's switch.

Every parent is applied as the Checkmk `parents` attribute, so RCA has a real
path to reason over.

### Why these numbers read as real (research summary)

- ~55-60 % of monitored hosts are servers, and 70-80 % of servers are VMs at
  12-15 VMs per hypervisor (enterprise virtualization surveys).
- Windows holds ~40 % of the server fleet (AD/file/RDS/MSSQL/LOB inertia),
  Linux the rest (the retailer's own platform).
- Network is 3-tier (core → distribution/ToR → access) with ~20 access
  switches per campus + firewall HA pairs + WLC pairs; APs are managed by the
  WLCs, not monitored individually.
- Power chain per rack (PDU) + per room (UPS, sensors) is SNMP-monitored —
  its absence is a "fake estate" tell.
- Printers: ~1 monitored office printer per 10-15 HQ staff + label printers
  per packing line.
- Out-of-band boards (iDRAC) exist for every physical box.

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
| SNMP devices (`snmp/netsim.py`) | —³ | 8101 |

¹ the two original demos both publish 6557 — run one at a time, or re-map. The
new hosts use 6560–6567 so they never collide with each other or the originals.
³ the SNMP devices (`sw-core-01`, `sw-access-01`, `rt-wan-01`, `ups-01`) have
no agent port at all: one daemon (`snmp/netsim.py`) answers SNMP v2c live on a
single UDP port (127.0.0.1:1161 — no site filesystem, no sudo), routing to a
device by its unique community, and Checkmk polls it directly — see
`snmp/README.md`. All four share the one control panel on 8101.

## Topology (parents + BI)

The estate has an explicit network path for RCA to reason over:

```
sw-core-01  (DC core switch, SNMP — no parent)
  ├─ sw-access-01  (DC server-access switch, SNMP)
  │    ├─ every server (web-frontend-01, payment-api, db-postgres-01, ...)
  │    ├─ cmk-demo-gateway (the delivery shell)
  │    └─ ups-01   (rack UPS, SNMP — its mgmt NIC)
  └─ rt-wan-01     (DC WAN head-end router, SNMP)
```

The parents only apply when the SNMP layer is deployed (`--scale full`);
without it the servers simply have no parent.

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
