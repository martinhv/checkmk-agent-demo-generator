# Demo: `leaf-sw-01` — whitebox top-of-rack access switch (Cumulus Linux)

The network heart of the Meridian Retail estate (see `../FLEET.md`): an
Edgecore AS5812-54X-class whitebox switch (Intel Atom C2538, 8 GB RAM, 16 GB
InnoDisk SATA-DOM 3IE3) running Cumulus Linux 5.9 — Debian-12-based, so it
carries a completely normal Linux Checkmk agent (monitoring Cumulus with the
Linux agent is a real, documented practice). **This is a steady-green
background host: the ToR switch every estate host hangs off (parent:
`core-gw-01`).** It has no incident and no break/heal toggle — it exists so
the estate has a believable network layer and the parent/child topology gives
RCA a real path to reason over.

The interface table is the point of this host: ten 10G access ports each
carrying the distinct traffic of the server behind them, two 40G uplinks
bonded towards `core-gw-01`, four dark ports. Most traffic is east-west
inside the rack (app ↔ db ↔ storage ↔ backup), so the uplink carries far less
than the access-port sum — as on a real ToR.

- **TCP 6569** (published and container) — Check_MK agent output (plaintext;
  the Checkmk 2.5 fetcher sees `<<` → `TransportProtocol.PLAIN`, no
  TLS/registration required). The controller-status section pretends the host
  is TLS-registered, so no "TLS not activated" warning appears.
- **TCP 8100** (published and container) — `/admin` status page (state badge,
  uptime, port map, auto-refresh every 10 s) + `/` JSON status endpoint. No
  toggle buttons — there is nothing to toggle. (8100 because 8099 is taken by
  the piggyback delivery control panel.)

## Services presented to Checkmk

All services are green in all circumstances.

| Service | Value | Notes |
|---|---|---|
| **Interfaces swp1–swp10** | 10 Gbit FD, up | one per server, ~5–385 Mbit/s each, distinct wobble; errors/drops 0 |
| **Interface bond0** (swp49+swp50) | 2× 40 Gbit LACP uplink | ~160/70 Mbit/s aggregate to core-gw-01; bond counters = exact member sum |
| **Interfaces swp11–swp14** | oper down | dark ports — default discovery ignores them (realistic) |
| **Interfaces eth0 / br_default** | 1 G mgmt / bridge | near-idle |
| **CPU load** | ~0.3 (4 cores) | switchd keeps one Atom core mildly busy |
| **CPU utilization** | ~9 %, softirq visible | control-plane punt path; iowait ≈ 0 |
| **Memory** | ~2 GiB used of 8 GiB | switchd ~1.2 GB RSS (ASIC tables in RAM); no swap |
| **Disk** (sda, InnoDisk SATA-DOM) | near-idle, sub-ms | a few log writes/s; utilization < 1 % |
| **SMART (sda)** | PASSED, temp ~33 °C | stays under the 35/40 °C WARN/CRIT levels |
| **Filesystem /** | ~33 % of 14.65 GiB | image-based OS, glacial growth |
| **Filesystem /var/log** | ~35–42 % of 2 GiB | log creep + midnight logrotate sawtooth |
| **Systemd Service Summary** | OK (30 units) | switchd, mstpd, ptmd, ledmgrd, portwd, nvued, FRR, lldpd … |
| **TCP connections** | ~10 ESTABLISHED | a switch terminates almost nothing itself |
| **Time sync** | stratum 3, corp NTP | dynamic timestamps — never stale |
| **APT** | no pending updates | exact sentinel text |
| **Job: nvue-config-backup** | exit 0 | nightly NVUE config export, 02:15 UTC |

## 1. Run it

```bash
cd leaf-sw-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6569** (agent) and **8100** (admin).
Stdlib-only Python — no dependencies. Without Docker:

```bash
python3 serve.py            # defaults: agent 6569, admin 8100
```

Admin UI: `http://localhost:8100/admin`
JSON status: `http://localhost:8100/`

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `leaf-sw-01.corp.meridian-retail.com`,
   IP `127.0.0.1`, **Checkmk agent port → 6569**.
2. Set its parent to `core-gw-01.corp.meridian-retail.com` and make it the
   parent of all rack hosts (see `../FLEET.md` topology).
3. Service discovery (any time — no discovery-time baselines here). The down
   ports swp11–swp14 and `lo` are skipped by default discovery; 15 interfaces
   are discovered.
4. Activate changes. Every service will be green.

No extra monitoring rules are needed: all relevant checks use their default
thresholds and this host is well inside them.

## 3. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `leaf-sw-01.corp.meridian-retail.com` | hostname in `<<<check_mk>>>` |
| `AGENT_PORT` | `6569` | agent TCP port (same inside and outside the container) |
| `HTTP_PORT` | `8100` | admin HTTP port (same inside and outside the container) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the agent header |
| `STATE_FILE` | `/var/tmp/cmk-demo-leaf-sw-01.json` | counter/uptime persistence across restarts (`""` = disabled) |

## Notes

This host has no incident, no `START_STATE`, no break/heal toggle, and no
auto-escalation watchdog. It is the steady-green network backbone: when an
incident fires on `db-postgres-01` or `app-worker-01`, the switch in between
stays green — which is itself a diagnostic signal (the path is fine, the
endpoint is not).
