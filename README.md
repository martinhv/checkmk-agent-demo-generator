# checkmk-agent-demo-generator

A fake **company estate for Checkmk live demos** — believable servers,
network gear and incidents, staged on one laptop or throwaway VM, in one
command:

```bash
./estate.py up --site        # simulators + full Checkmk setup on the newest dev site
./estate.py break sw-access-01
./estate.py down --site      # everything gone again
```

Born as demo plumbing for a Checkmk product keynote, grown into a fictional
**mid-sized company** ("Meridian Retail", an online retailer with an in-house
payments platform): ~14 hosts across edge/app/data/infra/network tiers,
parent topology, a pageable BI business service, and one staged incident per
host — everything stdlib-only Python, no real agents, no SNMP stack. See
**`FLEET.md`** for the company story, roster, and port map.

## The one-stop shop: `estate.py`

`estate.py up` starts the simulators and configures the site (folder, hosts,
rules, parents, BI pack, discovery, activation — idempotent, re-run any
time); `down` removes all of it.

```bash
./estate.py up --site                          # newest local v* dev site
./estate.py up --site v300 --scale minimal     # the classic 2-host demo
./estate.py up --site --scale standard         # 10 agent hosts, no SNMP
./estate.py up --site --replicas 5             # ~50-host estate, same stories
./estate.py up --site-url http://host/prod --user automation --secret ...
./estate.py status                             # what runs, who's broken
./estate.py degrade rt-wan-01                  # stage incidents from the CLI
```

| `--scale` | what you get |
|---|---|
| `minimal` | the two classic demos: `payment-api` + `db-postgres-01` |
| `standard` | the full agent estate: 10 server hosts + BI pack |
| `full` *(default)* | standard + SNMP network gear (answered live by netsim, no sudo) |
| `company` | the researched **300-host company**: full + ~170 steady-green servers (`fleet/`, one process) + ~110 SNMP devices replayed from anonymized real walks (`snmp/walklib/`) — see `FLEET.md` |

`--replicas N` stamps out every replicable host class N times
(`web-frontend-02` …, extra SNMP access switches) — steady-green copies for
scale-feel; each incident story stays unique. Break/heal interactively on the
control panels: **:8099/admin** (servers), **:8101/admin** (network).

## What's in the box

| Dir | What |
|---|---|
| `estate.py` | the CLI — orchestrates everything below |
| `deploy/` | deployment machinery: `cmk_setup.py` (REST-API site setup/teardown engine, also usable standalone) and `piggyback/` (ONE container that runs every agent host and delivers them as piggyback — agent :6559) |
| `hosts/` | the agent-based simulators, one dir per host (`serve.py` + Dockerfile + README with the demo choreography) — each still runs standalone via its own `docker compose` |
| `fleet/` | the company-scale server bulk: `profiles.py` (declarative roster, ~170 Linux/Windows hosts on 12 KVM hypervisors) + `serve.py` (ONE process synthesizing every agent output) |
| `snmp/` | the SNMP simulator: `netsim.py` answers SNMP v2c **live** on a UDP port per device (127.0.0.0/8:1161 — stdlib responder `snmpserver.py`, no sudo, no stored-walk files); real if64/cisco/apc plugins, live graphs. `walklib/` holds ~24 anonymized real device walks (made by `curate_walks.py`) that netsim replays as ~110 estate devices |
| `CLAUDE.md` | the engineering knowledge base (see below) |
| `FLEET.md` | the company story, host roster, topology, port map |

The estate is deliberately **mostly green** — a wall of green with one or two
reds tells one clean story. Incident highlights: a payment API 503 with a
failed-unit root cause, a fail-slow dying SSD ("Explain with AI"), a memory
leak → OOM-kill flap, a Redis eviction storm, a CRC-storming uplink that then
dies, a saturated WAN link pushing the router CPU past its levels, a filling
Windows C: drive. Every host's README has the choreography.

## Engineering knowledge

**`CLAUDE.md`** is the distilled knowledge base for building fake hosts that
survive scrutiny by Checkmk itself *and* by Linux/PostgreSQL/Windows/network
specialists: transport tricks, monotonic state-aware counters, restart
persistence, autocorrelated gauges, incident design rules, physics
cross-checks, section-by-section parity with a real agent, and the stored-
SNMP-walk file format. All of it verified against the Checkmk source, not
memory.

For calibration, diff a fake host against a real 2.5 Linux agent dump from
your own environment (`check_mk_agent` on an Ubuntu 24.04 box). The Windows
host is calibrated against the real 2.3 Windows agent dump shipped in the
Checkmk source tree (`tests/gui_e2e/data/windows-2.3.0p10`).

## Running pieces by hand

Everything `estate.py` does decomposes into parts you can run alone:

```bash
cd hosts/app-worker-01 && docker compose up --build -d   # one host, own TCP port
nc 127.0.0.1 6562 | head                                 # its agent stream
cd deploy/piggyback && docker compose up --build -d      # the whole estate, one container
../cmk_setup.py --site                                   # site setup only
python3 snmp/netsim.py --walks-dir /tmp/walks --once     # eyeball SNMP walks
```

`hosts/db-postgres-01/install-native.sh` deploys that demo behind a *real*
TLS-registered agent on a Linux box (relay trick — see `CLAUDE.md`).
