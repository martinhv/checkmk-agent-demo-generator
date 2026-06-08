# Demo: the "dying disk" database host (Theme 2 — Explain with AI)

A throwaway container that impersonates **one of Anna's PostgreSQL servers**
(`db-postgres-01`) for the Theme 2 demo: the **CPU load CRITICAL** page that is
really a **dying data disk** — the trap that "Explain with AI" untangles.

- **TCP 6556** — emits real Check_MK agent output (plaintext, same trick as the
  payment-api demo: the fetcher sees `<<` → `TransportProtocol.PLAIN`, no TLS,
  no registration). Includes the full **mk_postgres plugin sections**, so the
  host carries real PostgreSQL services. The controller-status section
  **pretends the host is TLS-registered** (`allow_legacy_pull: false` + a
  registered pull connection with a cert expiring in ~330 days), so the
  Check_MK Agent service shows **no "TLS is not activated" warning**.
- **TCP 8080** — the **control UI** at `/admin` (state badge, time-in-state,
  one card per state with its effects, toggle buttons; auto-refreshes every
  5 s) plus the same toggles as a curl API. Nothing here is monitored — this
  story has no HTTP check.

## The incident it fakes (matches the storyline verbatim)

| Service | Healthy | Broken | Role in the story |
|---|---|---|---|
| **CPU load** | ~0.9 | **CRIT** — 15-min load ~44 on 4 cores (default levels: 5/10 per core ⇒ CRIT > 40) | the misleading **symptom** that pages Sam |
| **CPU utilization** | ~85 % idle | compute nearly idle, **~80 % I/O wait** | the first clue: it's not compute |
| **Disk IO SUMMARY** | reads ~0.4 ms (healthy SSD) | **read latency ~200 ms** (read-retry storms), utilization ~99 %, queue ~12 | the smoking gun, one service over |
| **SMART SAMSUNG MZ7L3… Stats** | all zero | **stays GREEN through the incident** (fail-slow: retry storms don't show in SMART attributes) — then, ~8 min *after* the load page, the drive finally "confesses": pending sectors → **CRIT**, cascading (uncorrectable, then reallocated) | the twist: **no service points at the root cause** — the AI must diagnose it from cross-signals; SMART vindicates it later |
| **Temperature SMART SAMSUNG …** | 30 °C | **WARN** ~2-3 min into degraded — climbs 33 → 38 °C past the default 35/40 levels (controller burns power on retries), then plateaus at 38 (stays WARN, never CRIT) | the only breadcrumb, and one a human blames on the rack AC |
| **PostgreSQL Query Duration MAIN/payments** | 0–3 s | longest query **stuck since the break — duration grows live** across re-polls | the app-level symptom AI can quote |
| **PostgreSQL DB MAIN/payments Statistics** | ~380 commits/s | commits collapse **~10×** (~35/s) while CPU is idle | throughput starving on I/O |
| **PostgreSQL Daemon Sessions MAIN** | ~8 (2 running) | **24 running** — queries piling up | matches the ps backend pile-up (8→26) |
| **PostgreSQL Connection Time MAIN** | ~0.01 s | ~1.8–2.4 s | even connecting crawls |
| **Memory** | OK, Dirty ~20 MB | **still OK**, but Dirty **piles up live** (~200 MB+, growing ~1.5 MB/s) and Writeback ~190 MB | flushes can't drain to the dying disk |
| everything else (Filesystems, postgres unit/instance, connections %, locks, vacuum/analyze, bloat, time sync, APT, jobs, network) | OK | **still OK** | the trap: the host "looks fine", postgres is *up* |

The memory section is a full `/proc/meminfo` (58 keys, identical key set to a
real Ubuntu 24.04 host), so the Memory service yields the complete metric set.
The overall section layout is diffed against a real 2.5 Linux agent dump — incl.
agent controller status, deployed-plugin list (`mk_postgres`), both `lnx_if`
variants, mounts, systemd-timesyncd (dynamic sync timestamps) and APT.

**Three states, because the timeline is part of the story** ("the disk-health
warning that fired twenty minutes earlier"):

- `healthy` → all green. **Discover the host in this state** — the SMART check
  snapshots raw attribute values at discovery and goes CRIT only when they
  later *exceed* that baseline.
- `degraded` → the disk starts failing **the fail-slow way**: the SMART
  attributes stay clean (firmware read-retry/ECC storms destroy latency but
  only *completed* failures show up in SMART counters — a famous, real
  failure class), the overall self-assessment stays PASSED. The only signals:
  the **temperature WARN ~2-3 min in** (climbs 33 → 38 °C — looks like an AC problem)
  and a read-latency **stutter** in the Disk IO graph: ~1.2 ms baseline with
  read-retry storms to ~8 ms (util ~40 %) in 1–2 min clumps every few
  minutes — fail-slow's classic signature: the average barely moves, the
  spikes are the tell. **Trigger ~24 min before you want the page.**
- `broken` → adds the performance impact: load CRIT, I/O wait pinned, 200 ms
  reads, postgres backends piling up (8 → 26). **`degraded` auto-escalates to
  `broken` after 20 min** (`AUTO_BREAK_AFTER_MIN`, 0 disables); the /admin
  screen shows the countdown. The impact is **no vertical cliff**: everything
  ramps over ~4 min (`BREAK_RAMP_MIN`) with per-signal lag — disk latency
  climbs fastest (it's the cause), I/O wait follows, the loadavgs trail in
  1-min < 5-min < 15-min order like the kernel's real smoothing. CPU load
  crosses **WARN (20) at ~1.8 min and CRIT (40) at ~3.6 min** after the break;
  backends pile up poll by poll (2 → 24 running), commits collapse as a curve,
  Dirty grows ~1.5 MB/s and Writeback ramps 0 → ~190 MB. So: **degrade ~24 min
  before you want the page** (20 min auto-escalation + ~3.6 min load climb).
  Crucially, **SMART is still green at page time** — the AI has to fuse
  load + iowait + read latency + Dirty/Writeback + the postgres collapse (plus
  the 20-min-old temperature WARN) into "the disk is failing in a way SMART
  doesn't report". Then, `SMART_CONFESSION_MIN` (~8 min) into the incident,
  the drive finally admits it: pending sectors → **CRIT** above the discovery
  baseline, cascading causally (uncorrectable ~5 min later, reallocated ~10,
  remaps consuming a few pending) — **SMART confirms what the AI already
  said.**

The data disk is a **SATA datacenter SSD (Samsung PM893 480 GB)** — believable
2026 hardware, and a dying SSD's read-retry/ECC storms genuinely produce
100×+ latency cliffs. SATA SSDs report the same SMART ATA attributes
(5/187/197), so the discovery-baseline CRIT works unchanged.

---

## 1. Run it

```bash
cd demo_dying_disk_db
docker compose up --build -d
docker compose logs -f          # watch [boot] / [ctl] lines
```

It starts **healthy** (you must discover before breaking). Ports are published
on `127.0.0.1` as **`6557`** (agent) and **`8081`** (toggle) — 6556 is the
laptop's own agent. ⚠️ The payment-api demo (`demo_broken_http_service`) also
publishes 6557: stop it first, or change one side's host port in
`docker-compose.yml` if you want both demos running at once.

No Docker? Stdlib-only Python (AGENT_PORT is then the listen port directly):

```bash
AGENT_PORT=6557 HTTP_PORT=8081 START_STATE=healthy python3 serve.py
```

### Variant B: take over a real, TLS-registered agent host

For a host that already runs a real Checkmk agent (e.g. the EC2 demo box),
keep the genuine agent-controller TLS transport and swap only the payload:

```bash
scp -r demo_dying_disk_db ubuntu@<host>:
ssh ubuntu@<host> sudo '~/demo_dying_disk_db/install-native.sh'
# undo: sudo ~/demo_dying_disk_db/install-native.sh restore
```

`install-native.sh` is idempotent (re-run it after agent package updates,
which overwrite the relay, or after copying a new `serve.py`). It installs
`serve.py` as the systemd unit `cmk-demo-dying-disk.service` (no docker
needed) and replaces `/usr/bin/check_mk_agent` with a relay to it — backing
up the original to `check_mk_agent.orig` once. The relay must consume the
remote-address line the controller writes into the socket
(`MK_READ_REMOTE=true`); skipping that read resets the connection and
`cmk-agent-ctl dump` fails. The toggle UI then listens on `localhost:8081`
on that host — reach it via `ssh -L 8081:localhost:8081 ubuntu@<host>`.

Sanity checks:

```bash
nc 127.0.0.1 6557 | head             # should print <<<check_mk>>> ...
curl http://127.0.0.1:8081/          # {"state": "healthy", ...}
```

---

## 2. Set it up in Checkmk (before the talk)

1. *Setup → Hosts → Add host*. Name `db-postgres-01.corp.meridian-retail.com`, IP `127.0.0.1` (or the
   Docker host's IP), **Checkmk agent port → `6557`**.
2. Run **service discovery while the container is `healthy`** — this baselines
   the SMART raw values at zero. (Discovering while degraded/broken bakes the
   bad values into the baseline and the SMART service will never go CRIT.)
3. Optional but recommended rules, so every story beat has a colored service:
   - **"Disk IO levels"** rule for `db-postgres-01`: set *read latency* levels
     (the prefill 30 ms / 50 ms is perfect → 200 ms shows hard CRIT). Without
     it the Disk IO service shows the 200 ms metric but stays OK.
   - **"CPU utilization on Linux/Unix"** rule with *Levels on IO wait* (prefill
     5 % / 10 %) → the CPU utilization service itself goes CRIT on ~80 % wait.
     (Without it the I/O-wait clue is visible in the graph/details only.)
4. Activate changes. Everything is green — that's the starting picture.

---

## 3. Demo choreography (matches Theme 2)

| When | Action | What Checkmk shows |
|---|---|---|
| T−24 min | `curl localhost:8081/admin/degrade` | nothing turns red. ~2-3 min later the **disk temperature goes WARN** (climbing 35 → 38 °C) — who cares, probably the AC. SMART Stats: green, drive says PASSED. |
| T−4 min | *nothing* — degraded **auto-escalates to broken after 20 min** (or `curl localhost:8081/admin/break` to force it early) | the impact ramps in: disk latency climbs first, then I/O wait, then load (1-min ahead of 15-min) |
| showtime | *nothing* | **CPU load → CRIT** (15-min load crosses 40 ~3.6 min after the break, after a WARN at ~1.8 min) — the page Sam gets |
| set the trap | open the CPU load alert | "load through the roof" — the instinct says runaway process / more cores. **Nothing red points at the disk** — SMART is green. |
| the reality | CPU utilization + Disk IO SUMMARY | compute idle, ~80 % **I/O wait**; read latency **~200 ms** at ~99 % utilization — but the disk's own health says fine |
| ⭐ Explain with AI | one click on the CPU-load alert | fuses load + iowait + disk latency + Dirty/Writeback + postgres collapse + the dismissed temp WARN → *"the disk is failing in a way SMART doesn't report (fail-slow); replace it — adding cores won't help"* |
| ~T+4 min | *nothing* — `SMART_CONFESSION_MIN` after the break | **SMART … Stats → CRIT** ("Pending sectors: 4 (during discovery: 0)") — the drive finally confesses, **vindicating the AI's diagnosis** |
| resolve | `curl localhost:8081/admin/heal` ("disk replaced") | next poll: everything calms down |

After the confession, pending sectors keep **rising (~+1 per 3 min)**, and the
stuck SELECT's duration **grows every poll** — re-polls during the talk show
the incident actively getting worse.

**Control UI:** open `http://localhost:8081/admin` — current state (color
badge), how long it's been in that state (plus "disk dying for…" / "stuck
query running for…"), and one card per state listing exactly what toggling to
it will change in Checkmk. Click the button on a card to switch; the page
auto-refreshes every 5 s. Handy as a stage tab next to the Checkmk UI.

> Tip: as with the other demo, hit *Reschedule* on the affected services right
> after each toggle so states flip on stage, not a minute later. The CPU load
> CRIT rides on the **15-min average**, which now climbs realistically — it
> crosses WARN ~1.8 min and CRIT ~3.6 min after the break (the /admin screen
> shows the ramp progress), and the graphs show curves, not steps.

Toggle endpoints (same as the UI buttons):

```bash
curl http://localhost:8081/admin/degrade   # disk starts dying (SMART only)
curl http://localhost:8081/admin/break     # full incident (load page)
curl http://localhost:8081/admin/heal      # disk replaced, all green
curl http://localhost:8081/                # JSON: state, in_state_for_s, stuck_query_seconds, ...
```

---

## 4. Why the states produce exactly those service results

- **CPU load**: default levels are **5.0/10.0 per core on the 15-min load**
  (`cmk/plugins/cpu/agent_based/cpu_load.py`) → 4 cores ⇒ CRIT above 40; the
  fake reports ~44. Blocked-on-I/O processes count into loadavg, so "huge load,
  idle CPU" is technically honest.
- **CPU utilization**: the `<<<kernel>>>` `cpu` line is
  `user nice system idle iowait …`; broken shifts ~80 % of ticks into iowait.
  No default levels — hence the optional iowait rule above.
- **Disk IO**: read latency = Δread_ticks / Δread_ios from `<<<diskstat>>>`
  (fields 6 and 3); broken accumulates 11 000 ms of read time per 55 reads/s
  ⇒ ~200 ms. Field 12 (io_ticks) drives utilization ⇒ ~99 %. No default
  levels — hence the Disk IO rule above.
- **SMART**: `<<<smart_posix_all:sep(0)>>>`, one `smartctl --json` document per
  line. The ATA check (`smart_ata.py`) stores raw values of attributes
  5/187/197/199 … at **discovery** and goes **CRIT when a value exceeds its
  discovered baseline** — that's why discovery must happen while healthy.
  The attributes stay zero through the incident (**fail-slow**: read-retry
  storms only cost latency; SMART counts *completed* failures, and dying
  drives famously report PASSED until the end). Only `SMART_CONFESSION_MIN`
  (~8 min) into `broken` do they start a staggered, causally ordered cascade
  (`smart_attrs()` in `serve.py`): pending first, uncorrectable from ~5 min,
  reallocated from ~10 min — single attributes stepping in bursts is how real
  SMART counters move; all jumping in the *same minute* would read as a
  flipped switch. Temperature has default levels 35/40 °C and creeps
  33 → 38 °C ⇒ WARN ~2-3 min into degraded, plateauing at 38 — the lone early breadcrumb.
- **PostgreSQL**: the full `mk_postgres` section family (`postgres_instances`,
  `_version`, `_sessions`, `_stat_database`, `_connections`,
  `_query_duration`, `_locks`, `_stats`, `_bloat`, `_conn_time`). Instance markers
  `[[[main]]]` are uppercased into service items (`MAIN`, `MAIN/payments`).
  None of these have alarming default levels — they stay green and
  *corroborate* (24/100 connections is far below the 80 %/90 % defaults).
- **Memory**: usage stays green (it's not a memory problem), but Dirty/
  Writeback behave like real dirty-page physics on a disk that can't drain
  writes — visible in the Memory service's metrics/graphs.
- All counters are strictly monotonic and integrate state-dependent rates, so
  toggling never produces counter-wrap artifacts and graphs always move.

## 5. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `db-postgres-01.corp.meridian-retail.com` | name baked into `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published as 6557) |
| `HTTP_PORT` | `8080` | admin toggle port (published as 8081) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the agent header |
| `AUTO_BREAK_AFTER_MIN` | `20` | minutes in `degraded` before auto-escalating to `broken` (`0` = never) |
| `BREAK_RAMP_MIN` | `4` | minutes for the broken impact to reach full force (`0` = instant spike); the load CRIT lands at ~90 % of this |
| `SMART_CONFESSION_MIN` | `8` | minutes into `broken` before the SMART attributes admit the failure (the fail-slow confession) |
| `STATE_FILE` | `/var/tmp/cmk-demo-dying-disk-state.json` | persists counters/uptime/incident state across restarts — without it every restart resets the counters backwards, and Checkmk's rate-based services (postgres Statistics, Disk IO, Kernel Performance) go stale via `IgnoreResults` until two fresh samples arrive (`""` disables) |
