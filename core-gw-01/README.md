# Demo: `core-gw-01` — datacenter gateway / edge router

The Meridian Retail datacenter gateway (see `../FLEET.md`): a small 1U network
appliance (4-core Intel Atom C3558, 8 GB RAM, one 240 GB Intel DC SATA SSD)
running Ubuntu 24.04 with FRR and keepalived. It routes the whole estate to
the ISP and is the VRRP **active** member of a pair (the standby is not
monitored). **This is a steady-green background host** — the estate's gateway;
parent of `leaf-sw-01` in the Checkmk topology. It has no incident and no
break/heal toggle — it exists so the monitoring estate looks like a real
company, not a pile of test boxes.

The box looks like a router, not a server: the traffic lives on the wire
(eth0 WAN ~120/40 Mbit/s, eth1 trunk ~150/60 Mbit/s), the CPU lives in
softirq (packet forwarding), conntrack inflates the slab, and the disk and
its own TCP sessions are almost idle. All counters and gauges gently wobble
to match the texture of a live host; nothing ever crosses an alert threshold.

- **TCP 6568** — Check_MK agent output (plaintext; the Checkmk 2.5 fetcher
  sees `<<` → `TransportProtocol.PLAIN`, no TLS/registration required). The
  controller-status section pretends the host is TLS-registered, so no
  "TLS not activated" warning appears.
- **TCP 8098** — `/admin` status page (state badge, uptime, service list,
  auto-refresh every 10 s) + `/` JSON status endpoint. No toggle buttons —
  there is nothing to toggle.

## Services presented to Checkmk

All services are green in all circumstances.

| Service | Value | Notes |
|---|---|---|
| **Interface eth0** (WAN) | ~120 Mbit/s in / ~40 out | 1 Gbit FD, ISP uplink, zero errors/drops |
| **Interface eth1** (trunk) | ~150 Mbit/s out / ~60 in | downlink to `leaf-sw-01`; WAN flows + inter-VLAN |
| **Interface eth2** (mgmt) | ~20 kbit/s | near-idle management port |
| **CPU load** | ~0.15–0.3 (4 cores) | Atom C3558; well under per-core WARN of 5.0 |
| **CPU utilization** | ~13 % busy, ~6.5 % softirq | forwarding happens in softirq, iowait ≈ 0 |
| **Memory** | ~2 GiB used of 8 GiB | slab ~180 MB (nf_conntrack + nftables sets) |
| **Disk** (sda, Intel SSDSC2KB240G8) | healthy SMART, near-idle I/O | ~0.25 ms latency, < 1 % util — DC SATA SSD physics |
| **SMART (sda)** | PASSED, temp ~32 °C | wandering ±1.3 °C, below 35/40 °C WARN/CRIT |
| **TCP connections** | ~15 ESTABLISHED | sshd/agent/FRR vtys — routers forward, they don't terminate |
| **Filesystem /** | ~6 % of 220 GiB | glacial creep + small daily sawtooth |
| **Filesystem /var/log** | ~29–38 % of 8 GiB | 24 h logrotate sawtooth, `noatime` |
| **Systemd Service Summary** | OK (30 units) | frr, keepalived, conntrackd running; nftables oneshot |
| **Process table** | consistent | watchfrr/zebra/mgmtd/staticd, 3× keepalived, conntrackd, smartd … |
| **Time sync** | stratum 2, offset ≈ 0 ms | dynamic timestamps — never stale |
| **APT** | no pending updates | exact sentinel text |
| **Job: nft-ruleset-backup** | exit 0, ~4.7 s | nightly ruleset dump at 01:45 UTC |
| **Uptime** | ~140 days | routers don't get rebooted often |

## 1. Run it

```bash
cd core-gw-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6568** (agent) and **8098** (admin).
Stdlib-only Python — no dependencies. Without Docker:

```bash
python3 serve.py            # defaults to 6568 / 8098 already
```

Admin UI: `http://localhost:8098/admin`
JSON status: `http://localhost:8098/`

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `core-gw-01.corp.meridian-retail.com`,
   IP `127.0.0.1`, **Checkmk agent port → 6568**.
2. Service discovery (any time — no discovery-time baselines to worry about;
   the SMART raw values are all zero and stay there).
3. Activate changes. Every service will be green.
4. Topology: set this host as the **parent of `leaf-sw-01`** (and, via the
   switch, the rest of the estate) — if the gateway goes down, everything
   behind it becomes unreachable, which is exactly the story.

No extra monitoring rules are needed: all relevant checks use their default
thresholds and this host is well inside them.

## 3. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `core-gw-01.corp.meridian-retail.com` | hostname in `<<<check_mk>>>` |
| `AGENT_PORT` | `6568` | agent TCP port (published 1:1 by compose) |
| `HTTP_PORT` | `8098` | admin HTTP port (published 1:1 by compose) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the agent header |
| `STATE_FILE` | `/var/tmp/cmk-demo-core-gw-01.json` | counter/uptime persistence across restarts (`""` = disabled) |

## Notes

This host has no incident, no `START_STATE`, no break/heal toggle, and no
auto-escalation watchdog. It is the steady-green network backbone of the
estate: when an incident fires on `db-postgres-01` or `app-worker-01`, the
viewer sees the gateway (and the wall of green behind it) calmly routing on —
exactly one or two reds, one root cause.

Router realism details: eth1's transmit wobble shares eth0's receive phase
(and vice versa), so the forwarded trunk traffic never instantaneously drops
below the WAN traffic it carries — packets don't vanish. The VRRP standby
gateway exists in the story (keepalived + conntrackd state sync) but is not
monitored, so no second host is needed.
