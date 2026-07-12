# snmp/ — the SNMP side of the estate

Fake **network equipment** for the Meridian Retail demo estate. Where the
server hosts fake a Checkmk *agent* over TCP, network gear is monitored via
SNMP — and `netsim.py` **answers SNMP v2c live** on ONE UDP port
(`127.0.0.1:1161`), routing to a device by its **unique community string**
(the device short name); Checkmk polls the single address with a per-host
community. One port means netsim ports-maps into a normal container like the
gateway (no `--network host`). No site filesystem, **no sudo**. The SNMP
server itself is `snmpserver.py` — a stdlib-only BER codec +
GET/GETNEXT/GETBULK responder (`--selftest` validates it); it serves every
value as an OCTET STRING, which Checkmk stringifies exactly like the "real"
type, so no per-OID type table is needed. A daemon that re-renders each device
on demand with advancing counters produces **live traffic graphs**, real rate
checks, and stageable incidents.

`netsim.py` is a stdlib-only daemon with monotonic counters (`Counter`) and
autocorrelated gauges (`gauge`), exactly the physics of the agent hosts (see
the repo `CLAUDE.md`). Break/heal control panel on **:8101/admin**, persisted
state so restarts are invisible. `--access-switches N` stamps out N access
switches (replicas are steady green; the incident stays on the first).

A **legacy `--transport walk`** still writes stored-walk files into the site's
`~/var/check_mk/snmpwalks/` (the `usewalk_hosts` rule) for anyone who wants
it — but that path needs the site user (sudo), which is exactly what the live
default avoids.

The Checkmk side (hosts as SNMP v2 / no-agent sharing `ipaddress` 127.0.0.1,
each with a per-host `snmp_community` = its device name + one folder port
rule, plus discovery + activation) is done by **`../deploy/cmk_setup.py`**,
which `../estate.py` drives for you.

## The replay fleet (`--fleet`, company scale)

At `--scale company` netsim additionally replays **~110 devices from
anonymized real walks** (`walklib/*.walk`, 24 models: Aruba/HP/Huawei
switches, Fortigate + ASA firewalls, Kemp load balancers, Extreme WLCs,
Ricoh/Canon/Zebra printers, APC/Raritan/Gude power, AKCP/AVTECH sensors,
Synology NAS, Brocade FC, Dell iDRAC, Meinberg NTP). Each instance gets its
own sysName/sysLocation, an advancing uptime, and live interface counters
whose base rate derives from the *recorded* counter over the *recorded*
uptime — busy ports stay busy, dead ports stay dead. Roster in
`REPLAY_ROSTER` (netsim.py).

The walklib is produced by **`curate_walks.py`** from `~/git/zeug_cmk/walks`
— strip + scrub + audit, so no customer-identifying data enters this repo
(see `../CLAUDE.md`, "Anonymizing real walks"). Re-run it only when adding
models; the curated files are checked in.

## The synthetic devices

This is the estate's network layer: `sw-core-01` tops the parent topology
(every server hangs off it — applied by `../deploy/cmk_setup.py` when the
SNMP layer is deployed).

| Host | Device | State | Story |
|---|---|---|---|
| `sw-core-01` | Catalyst 9300 campus core switch (12 × 10G) | steady green | background — CPU/mem/temp/PSU/fans + per-port traffic |
| `sw-access-01` | Catalyst 9200 access switch (48 × 1G + 2 × 10G uplinks) | **incident** | CRC error storm on uplink Te1/1/1 (WARN), then the link dies (CRIT) and traffic fails over to Te1/1/2 |
| `rt-wan-01` | Cisco ISR 2921 warehouse WAN router | **incident** | WAN saturation (runaway inventory replication): Gi0/1 ramps 180 → ~940 Mbit/s, CPU climbs past the cisco_cpu defaults (WARN 80 / CRIT 90), output discards appear |
| `ups-01` | APC Smart-UPS 3000 (AP9631 card) | steady green | battery status/capacity/temp, runtime, output load, self test |

Services per device (all from real Checkmk SNMP plugins, no rules needed):
`SNMP Info`, `Uptime`, `Interface NN`, `CPU utilization`, `Memory <pool>`,
`Temperature <sensor>`, `Power <psu>`, `FAN <fan>` on the Cisco boxes;
`APC Symmetra status`, `Self Test`, `Phase Input/Output/Battery`,
`Temperature Battery` on the UPS.

## Quick start

```bash
# the one-stop shop does all of this: ../estate.py up --site
# by hand instead:

# 1. start the SNMP responder — runs as you, no sudo, no site access
python3 snmp/netsim.py                             # foreground; or use &
#    (answers SNMP live on 127.0.0.1:1161, routed by community; --selftest
#     checks snmpserver.py)

# 2. bootstrap Checkmk (hosts + per-host community + port rule + discovery)
deploy/cmk_setup.py --site heute

# 3. drive the incidents
open http://localhost:8101/admin
curl localhost:8101/admin/sw-access-01/degrade     # CRC storm  -> WARN
curl localhost:8101/admin/sw-access-01/break       # link down  -> CRIT
curl localhost:8101/admin/rt-wan-01/break          # saturation -> CPU CRIT
curl localhost:8101/admin/sw-access-01/heal
```

**Discover while HEALTHY.** Two reasons, both baked into the if64 plugin:
down interfaces are never discovered (default discovery matches
`ifOperStatus == up` only), and the interface check's target state is the
one *recorded at discovery* — discovering while the uplink is down would
bake "down" in as the expected state and the flap would never alert.

Rates need **two poll cycles**: the first check after discovery shows no
traffic numbers (Checkmk needs a counter delta) — by the second minute the
graphs are live.

## The incident choreography

### sw-access-01 — dying uplink (`degrade` ~20 min before showtime)

1. `degrade`: Te1/1/1 (service `Interface 49`) develops CRC errors —
   ~0.04 % of inbound packets, ramping in over ~2 min. That is squarely
   between the if64 defaults (WARN 0.01 % / CRIT 0.1 % of packets):
   **WARN**, the classic dying-SFP/bad-patch-cable picture. Traffic still
   flows; everything else stays green.
2. auto-escalation (default 20 min, `AUTO_BREAK_AFTER_MIN`) or `break`:
   the link goes **down** → `Interface 49` **CRIT** (oper status ≠
   discovered state). Te1/1/2's load roughly doubles — the failover is
   visible in its graph, corroborating the story without another alert.

### rt-wan-01 — WAN saturation (`degrade`, then `break` at showtime)

1. `degrade`: Gi0/1 climbs 180 → ~600 Mbit/s, CPU to ~70 %. Graphs move,
   nothing is red (interface bandwidth has **no default levels** — by
   design the alert comes from the CPU).
2. `break`: ~940 Mbit/s of the 1G link, CPU ~93 % → `CPU utilization`
   goes **WARN at 80, CRIT at 90** (cisco_cpu defaults), and output
   discards appear on the WAN port (visible in the graph, no extra alert).
   One red service, and the graph next to it explains it.

## How the fake stays honest

- **Walk format** verified against the parser (`stored_walk.py` +
  `_utils.py`): `.oid value` lines in strict numeric OID order (the backend
  binary-searches), printable strings raw, binary values (MACs) as quoted
  uppercase hex **with a trailing space** (`"00 1B 2C 02 00 31 "`) — without
  that space the parser keeps literal ASCII instead of decoding bytes.
  Files are written atomically (tmp + rename) so a poll never reads a torn
  walk.
- **Counters never go backwards** — same `Counter` accumulator approach as
  the agent hosts, persisted across restarts (`/var/tmp/cmk-demo-netsim-state.json`).
  sysUpTime advances continuously; if64 uses it as the rate timestamp.
- **Traffic conservation**: the access ports' aggregate matches the uplinks
  (in ↔ out swapped), and the core switch's ports mirror the devices their
  `ifAlias` names as peers. A network person *will* sum these.
- **Exactly one plugin family per signal**: the Catalysts expose
  `cpmCPUTotalPhysicalIndex` → `cisco_cpu_multiitem` (per-entity), the ISR
  exposes only `cpmCPUTotal5minRev` → classic `cisco_cpu`; enhanced-64
  vs legacy memory pools likewise. The UPS keeps sysObjectID under
  `.1.3.6.1.4.1.318` (APC) so the `apc_symmetra` family fires and the
  generic RFC1628 `ups_*` plugins never do.
- **Green means green**: every wandering gauge stays clear of its default
  levels (UPS capacity ≥ ~98 vs lower-levels 95/80, battery temp ~25 vs
  50/60, output voltage ~231 vs lower-level 220, switch temps vs device
  thresholds 65/75, …) — checked against each plugin's
  `check_default_parameters` in the Checkmk source.

## Knobs (env)

| Variable | Default | Meaning |
|---|---|---|
| `ESTATE_DOMAIN` | `corp.meridian-retail.com` | FQDN suffix (must match the hosts in Checkmk) |
| `HTTP_PORT` | `8101` | control panel port |
| `RENDER_INTERVAL` | `30` | seconds between walk rewrites |
| `AUTO_BREAK_AFTER_MIN` | `20` | degraded → broken auto-escalation (0 = off) |
| `STATE_FILE` | `/var/tmp/cmk-demo-netsim-state.json` | counter/incident persistence |

## Eyeballing without a site

```bash
python3 netsim.py --walks-dir /tmp/walks --once
head /tmp/walks/sw-access-01.corp.meridian-retail.com
```
