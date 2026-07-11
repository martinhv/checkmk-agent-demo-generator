# CLAUDE.md

## Faking a Checkmk agent / agent sections (demo hosts)

How the demo containers (`hosts/payment-api/`, `hosts/db-postgres-01/`) impersonate real hosts. Reference for building more fake hosts.

### Transport: plaintext TCP, no registration

Listen on a TCP port and write the raw section text, then close. The Checkmk 2.5 fetcher reads the **first two bytes**; `<<` (from `<<<check_mk>>>`) means `TransportProtocol.PLAIN`, and with no encryption ruleset the server default is `ANY_AND_PLAIN` — so a plain, unregistered fake agent is accepted. No TLS, no agent controller, no one-time token. (Source: `check_mk:packages/cmk-check-engine/cmk/fetchers/_tcp.py`.) In Checkmk just add the host and override the agent port.

### Variant: hijack a real TLS-registered agent instead of plain TCP

Better than the plaintext-TCP trick when a host with a real 2.5 agent + registered agent controller is available (e.g. the EC2 demo box): keep the genuine TLS transport and replace only `/usr/bin/check_mk_agent` with a relay that fetches from the local fake-agent server (`exec cat < /dev/tcp/127.0.0.1/<port>`). Scripted idempotently in `hosts/db-postgres-01/install-native.sh` (systemd unit for serve.py — stdlib-only, no docker — + relay + one-time `.orig` backup + `restore` subcommand). **The trap:** the controller invokes the agent via the `check-mk-agent@.service` socket unit with `MK_READ_REMOTE=true` and *writes the remote address into the socket first*; a relay that doesn't `read -r REMOTE` that line resets the connection — `cmk-agent-ctl dump` fails with "Connection reset by peer" while running the script by hand works fine. Mirror the real agent's `set_up_remote()`: `[ -z "$REMOTE" ] && [ "$MK_READ_REMOTE" = true ] && read -r -t 5 REMOTE`. Always verify the controller path with `sudo cmk-agent-ctl dump`, not just by executing the script. Agent package updates overwrite the relay → re-run the installer.

### Always verify against the Checkmk source, not memory

For every section, read the parser + check plugin in `~/git/check_mk/cmk/plugins/<name>/agent_based/` before writing fake data. Three things to extract:

1. **Exact line format / field order** (e.g. `<<<kernel>>>` cpu line is `cpu user nice system idle iowait irq softirq steal guest guest_nice`; `<<<diskstat>>>` is `/proc/diskstats` order: field 3 = read ios, 6 = ms reading, 12 = io_ticks).
2. **Default parameters** (`check_default_parameters` in the `CheckPlugin`) — they decide whether the fake values actually produce WARN/CRIT or need a rule:
   - CPU load: `levels15 = (5.0, 10.0)` **per core** → 4 cores go CRIT above 15-min load 40. `levels1`/`levels5` are `None` by default.
   - SMART temperature: `(35.0, 40.0)` °C.
   - diskstat latency/utilization and kernel iowait: **no defaults** — need a "Disk IO levels" / "CPU utilization on Linux/Unix" rule, or the value is display-only.
3. **Discovery-time baselines.** Some checks snapshot values at *discovery* and only alert on later deviation. SMART ATA (`smart_ata.py`) stores raw attribute values (ids 5/10/184/187/196/197/199) as discovered parameters and goes **CRIT when value > discovered baseline**. Consequence for demos: **discover while healthy, break later** — discovering in the broken state bakes the bad values into the baseline and nothing ever alerts.

### JSON-line sections need `sep(0)`

Sections whose payload is one JSON document per line (e.g. `<<<smart_posix_all:sep(0)>>>`) must use `sep(0)` so the whole line arrives as a single column — with default whitespace splitting the parser gets only the first token and crashes. Pydantic parse models **ignore extra JSON keys**, so cosmetic fields (`smart_status`, `device.type`) are safe. Validate fake JSON against the real models without a site: strip the `cmk.agent_based.v2` import off the parser file, `exec` the model classes, call `ParseSection.model_validate_json(line)`.

### Counters: integrate state-dependent rates, never go backwards

**…including across process restarts.** Rate checks (postgres `…Statistics`, Disk IO, Kernel Performance) call `get_rate(..., raise_overflow=True)`: a counter that goes backwards raises `IgnoreResultsError`, which aborts the whole check → the service keeps its old result and goes **stale**. Worse, the abort happens at the *first* backwards counter in the check's loop, so the remaining counters' stored samples are NOT updated that cycle — after one reset of N counters, the check repairs **one counter per cycle** and stays stale for ~N minutes (observed: ~6 min for `postgres_stat_database` with its 6 rates; the busy-DB item stales long while the near-idle item recovers fast because its slow int-truncated counters compare equal). Each redeploy/restart of the fake agent = one such episode. Fix: persist all counter accumulators (`acc`+`last`, registry keyed by **stable counter names**, not creation order — then a code update that adds/removes counters still restores the survivors and only the new ones cost a cycle), `START` (uptime continuity) and the incident state + timestamps to a JSON state file (atomic tmp+rename; save on every agent poll + on toggle, load at boot). Put `PYTHONUNBUFFERED=1` in the systemd unit or the `[state]`/`[boot]` prints never reach the journal. Restoring `last` from before the downtime makes the next sample integrate the current rate across the gap — the restart becomes invisible. Bonus: a redeploy mid-demo no longer resets the running incident. Push-mode wrinkle: checks whose rate timestamps are *agent-embedded* (kernel, diskstat) additionally get `Δt=0 → IgnoreResults` whenever the site's checker cycle re-processes the same pushed payload (push interval ≈ check interval + phase drift) — that part is site-side; mitigate with a longer check interval or a higher staleness multiplier.

Checkmk derives rates as `Δcounter/Δtime`, so counters must be **strictly monotonic** even when the break/heal toggle changes the underlying rate. Don't compute `rate * elapsed` per state (flipping states makes the counter jump backwards → counter-wrap artifacts). Instead keep an accumulator per counter and integrate the *current* rate over the time since the last poll (`acc += rate(state) * dt`) — flipping changes the slope from now on, never the accumulated value. See `Counter` in `hosts/db-postgres-01/serve.py`.

- Wobble the instantaneous rate (±30 %, distinct phase per metric) — a dead-constant derived rate looks stale; identical phases make all graphs peak together. **A single fixed-period sine is wrong two ways:** (1) it renders as a synthetic clockwork sawtooth (one identical peak every `period` s), and (2) **if `period` < 2× the monitoring poll interval (~60 s) it ALIASES into high-frequency jigsaw on a 1-min graph** (Nyquist — this was the visible "jigsaw" on the interface bandwidth graph; the fix was period 150→1200 s). Use **incommensurate harmonics with LONG periods** (e.g. ω, 2.7ω, 0.41ω at a 20-min fundamental — no repeat, no aliasing) **plus an AR(1) random walk** (`noise = clamp(0.9*noise + gauss(0, 0.25))`) for the aperiodic texture a pure sine can't produce. **Clamp the composite to [-1, 1]** so the rate stays in `rate*[1-amp, 1+amp] > 0` — otherwise a noise spike makes the counter go backwards and re-triggers the staleness cascade.
- **Filesystem usage should grow AND get cleaned, not sit static.** A flat `df` line is a giveaway. Real volumes show secular growth with periodic cleanup sawteeth — model them as pure functions of wall-clock `now` (+ persisted START) so the curve is continuous across re-polls and restarts: a DB data volume = WAL recycled every ~12 min (checkpoint_timeout) as a ~1.5 GiB sawtooth + daily base-backup retention purge (~8 GiB) + slow forever table growth (~2 kB/s, matching the datsize trend); a root fs = slow log creep + daily logrotate trim. Keep it well under the 80/90 % df defaults (green corroboration, never an alert). Also emit the `[df_inodes_start]…[df_inodes_end]` block the real agent carries — a DB volume holds few huge files so inode use is ~1 %.
- **Instantaneous gauges (temperature, loadavg, memory free/dirty, conn time, tcp conn counts) need the SAME treatment — not static, not white noise.** A flat line (SMART temp pinned at 30 °C) screams "fake"; `random.uniform`/`randint` every poll is the opposite failure — uncorrelated white-noise spikes. Reuse the harmonic+AR(1) wobble as a smooth **autocorrelated wander** around a (possibly drifting/ramping) baseline, scaled by an absolute (±1.3 °C) or fractional (±1.5 %) amplitude. Gauge state is ephemeral — a small jump at restart is invisible for an instantaneous value (unlike counters, which must persist). Give faster signals shorter periods (1-min loadavg period ~300 s, 15-min loadavg ~2400 s) so they're correctly more/less jumpy. Keep a wandering value clear of its alert threshold (broken SMART temp base 38 ±1.3 stays in the 35–40 WARN band, never trips CRIT).
- **Cap the amplitude where the derived value has a hard ceiling**: diskstat `io_ticks` accumulates ms of busy time per second, so at ~990 ms/s (≈99 % utilization) a ±30 % swing renders an impossible 122 % utilization. Use amp ≈ 0.
- Seed accumulators with `rate * fake_uptime` so a "12-day-old" host has plausible totals.

### Designing the incident

- **Low noise, one root cause** (storyline rule): keep everything irrelevant green in *all* states; flip only the symptom + root-cause services. A wall of green with two reds tells one story.
- Engineer service states from the thresholds, not vibes: pick fake values that clear the default levels you looked up (e.g. 15-min load 44 > 40 → CRIT; 38 °C between 35/40 → WARN; pending sectors 8 > baseline 0 → CRIT).
- If the *timeline* matters (event A "fired 20 min before" event B), model intermediate states (healthy → degraded → broken), not a binary toggle. Let the timeline run itself: a watchdog thread that auto-escalates degraded → broken after N minutes (env-configurable, countdown shown on /admin) means one toggle at T−20 and the symptom fires at showtime hands-free.
- **Don't serve the root cause on a plate.** If a service has been CRIT pointing at the root cause for 20 minutes, nobody needs AI to find it. The realistic dodge for disks is **fail-slow**: firmware read-retry/ECC storms destroy latency while the SMART attributes stay clean and the drive reports PASSED (SMART only counts *completed* failures — a real, documented failure class). Keep the early breadcrumbs ambiguous (disk temp WARN reads as "rack AC", latency creep visible in graphs only), make the AI fuse cross-signals (iowait + latency + Dirty/Writeback + app collapse), and let SMART "confess" N minutes *after* the symptom page — vindicating the AI's diagnosis instead of preempting it.
- **Pre-incident degradation should stutter, not creep.** A flat 3× latency plateau before the break reads as synthetic; real read-retry onset is bursty — baseline ~1.2 ms with 1-2 min storms to ~8 ms every few minutes (average barely moves, the spikes are the tell, and the AI can cite "intermittent spikes began ~20 min before"). Make bursts deterministic per wall-clock minute (`random.Random(int(now // 60))`, OR-ing the previous minute to stretch storms) so re-polls within a minute agree, and blend them out via the same lerp once the break ramps in.
- **Discrete event counters (SMART attrs) may step, but not in lockstep.** One attribute jumping 0→8 between two polls is realistic (sectors get flagged in bursts); *all* attributes jumping in the same minute reads as a flipped switch. Stagger them causally from the degraded-age: pending sectors first (CRIT on first poll), uncorrectable errors from ~5 min, reallocated from ~10 min — and let reallocations *consume* a few pending (remap happens on write), with temperature creeping past the WARN level minutes after the CRIT. A cascading event history ≫ one synchronized cliff.
- `systemd_units` quirk: emit an empty `[status]` followed by `[all]` — the parser falls back to `[all]`.

### Testing without a Checkmk site

Poll the fake agent twice a few seconds apart and compute the deltas exactly like Checkmk does:

```bash
nc 127.0.0.1 <port> | head        # eyeball the sections
# read latency = Δrd_ticks/Δrd_ios, util % = Δio_ticks/Δt/10, iowait % from kernel cpu deltas
```

Assert the derived numbers (not the raw counters) match the story: reads/s, ms per read, utilization %, iowait %, loadavg vs. the per-core levels.

### PostgreSQL sections (mk_postgres plugin family)

Verified against `cmk/plugins/postgres/` (parsers: `lib.py:parse_dbs`, `agent_based/postgres_*.py`):

- **Instance markers, not flat sections.** Every `postgres_*` section starts with `[[[instance]]]`; parsers **uppercase** the name into service items: `[[[main]]]` → `PostgreSQL Instance MAIN`, `PostgreSQL Connections MAIN/payments`.
- **Two layouts.** DB-list sections (`_connections`, `_query_duration`, `_locks`, `_stats`) are `sep(59)` (semicolon) with `[databases_start]`/db names/`[databases_end]` followed by **one header row** (consumed by `parse_dbs`, first column `datname` dropped) then data rows. `postgres_stat_database` is also `sep(59)` but header-row-only (`datid;datname;...` — the row is detected by `line[0]=="datid"`); despite the old space-separated docstring in the check, the real agent emits semicolons (`run_sql_as_db_user(field_sep=";")`).
- **Oddballs:** `postgres_instances` needs a PID line with ≥4 whitespace columns (`<pid> <binary> -D <datadir>`) — PID present = OK, missing = CRIT. `postgres_sessions` lines are `t <n>` (idle) / `f <n>` (running); the check sums them for "Total". `postgres_version` is `sep(1)` (whole line one column). `postgres_conn_time` is a bare float.
- **Defaults:** almost no postgres check alarms by default — connections at 80 %/90 % *of max_connections* is the only real one. So postgres services are **green corroboration**, not alarms: perfect for low-noise incident design (commits/s collapse, sessions pile up, query duration grows — all visible, nothing red).

### Realism details that sell a fake host

- **Model the secondary physics.** A dying disk doesn't just slow `diskstat` — dirty pages can't drain, so `/proc/meminfo` should show **Dirty piling up (live-growing) and Writeback nonzero**. Domain experts notice when these cross-signals are missing.
- **Emit the full `/proc/meminfo`** (~50 lines incl. Active/Inactive, Slab, Shmem, PageTables, HugePages), not a minimal subset — the Memory service then yields the complete metric set like a real host. Use the real kernel formula `CommitLimit = SwapTotal + RAM/2`. Watch the Memory check's quieter default levels when picking values: **shared memory warns at 20 % / crits at 30 % of RAM** (keep Shmem under 20 % unless the alert is wanted; ~15 % of RAM is a plausible postgres `shared_buffers`).
- **Live-growing values tied to state timestamps** beat static fakes: record `broken_since`/`degraded_since` and derive "longest query: N s" (grows every poll), "pending sectors +1 per 10 min". Re-polls on stage show the incident actively worsening.
- **Give the demo a control UI**: serve a small HTML page on the toggle port (`/admin`) showing current state, time-in-state, and per-state cards listing exactly which Checkmk services will change — with toggle buttons (303-redirect back) and meta-refresh. Much better on stage than remembering curl commands.

### Specialist cross-checks for fake hosts (a Linux/PostgreSQL person WILL sum these)

Review checklist that caught real bugs in the demo data — run it against any fake host:

- **Disk physics must match the device class.** A 7200 rpm HDD cannot do 75 random IOPS at 2 ms / 6 % util (rust costs 6–10 ms/read → ~40 % busy); 2 ms @ 6 % is SSD behavior. Pick the device first (model name in SMART!), then derive latency/util. A dying SATA SSD is the better 2026 story anyway: read-retry/ECC storms give genuine 100×+ latency cliffs, and SATA SSDs report the same SMART attrs 5/187/197 (plus add 177/179 wear attrs for smartctl literacy).
- **VSZ ≥ mapped shared memory.** Every postgres process maps shared_buffers, so ps VSZ must exceed Shmem (~2.9 GB for 2.5 GiB shared_buffers). VSZ 400 MB with Shmem 2.5 GiB is physically impossible.
- **ps must agree with pg_stat_activity.** Idle backends show `... <db> <user> <ip>(port) idle` in their cmdline, only running ones show a query verb — and the counts must equal the sessions section (`t`/`f`) and `numbackends`. Derive all three from the same variables.
- **meminfo LRU arithmetic:** `Active(anon)+Inactive(anon) ≈ AnonPages+Shmem` (shmem sits on the anon LRU since ~kernel 4.8); `Active(file)+Inactive(file) ≈ Buffers+Cached−Shmem`; `MemAvailable ≈ MemFree + file LRU + SReclaimable`. KernelStack ≈ threads × 16 KiB (match the loadavg total).
- **Every running unit needs its process** (pgbouncer.service active ⇒ pgbouncer in ps), and a real Ubuntu server has **~30 service units** (incl. `active/exited` oneshots like apparmor/keyboard-setup) plus system daemons in ps (journald, udevd, resolved, sshd, cron, smartd…) — 5 units / 14 processes reads as fake instantly.
- **WAL flush rate ~ commit rate**: ~120 sequential writes/s at ~0.5 ms for ~380 commits/s; 30 w/s would imply implausibly aggressive group commit.

### Calibrate against a real agent dump

Diff any fake host against a real 2.5 Linux agent dump (Ubuntu 24.04, OMD/site sections stripped — produce one with `check_mk_agent` on a real box): `comm -23 <(grep -o "^<<<[a-z_0-9]*" real | sort -u) <(… fake …)` plus a meminfo key diff. Gaps that round of comparison caught:

- **Both `lnx_if` variants**: the real agent emits a plain `<<<lnx_if>>>` with an `[start_iplink]…[end_iplink]` ip-link block *and* the `<<<lnx_if:sep(58)>>>` counters.
- **Unit ↔ section consistency**: running `systemd-timesyncd.service` ⇒ must emit `<<<timesyncd>>>`/`<<<timesyncd_ntpmessage:sep(10)>>>`. ⚠️ The timesyncd check compares **two timestamps against wall clock** — the `[[[<epoch>]]]` marker (last sync, defaults 7500/10800 s) and the NTPMessage `ReceiveTimestamp` (defaults 3600/7200 s) — so both must be **generated dynamically**, not static text. Timestamp parsing happens server-side via dateutil with the `Timezone=` IANA id, so emitting UTC timestamps + `Timezone=UTC` avoids tzdata in the container. Offset/jitter defaults 200/500 ms, stratum WARN ≥ 9.
- **`<<<apt:sep(0)>>>` defaults alert**: any pending normal update → WARN, security update (`…-security` repo in the line) → CRIT. For a green box emit the exact sentinel `No updates pending for installation` (cmk/plugins/apt/lib.py).
- **Plugin provenance**: if you fake plugin sections (mk_postgres), also emit `<<<checkmk_agent_plugins_lnx:sep(0)>>>` listing the plugin file + `CMK_VERSION`, and `<<<cmk_agent_ctl_status:sep(0)>>>` (controller JSON) — the Check_MK Agent service reads both.
- **Pretending TLS registration**: the "TLS is not activated" WARN comes *only* from the controller JSON (`cmk/plugins/checkmk/agent_based/checkmk_agent.py:_check_transport`): it fires iff `allow_legacy_pull` is true and the socket is operational — the actual transport (plaintext!) is never checked. Emit `allow_legacy_pull: false` plus a `connections` entry (`site_id`, `local.cert_info.{issuer,to}`, `to` in `"%a, %d %b %Y %H:%M:%S %z"` format) and the host reads as TLS-registered; the cert `to` is compared to wall clock (WARN/CRIT below 30/15 days) so generate it dynamically (~330 days out).
- **Full check_mk header**: real 2.5 agents add `InstallationDirectory`/`PackageDirectory`/`RuntimeDirectory`, `FailedPythonReason:`, `SSHClient:`.
- **`<<<mounts>>>`** feeds mount-option checks; `noatime` on a DB volume is the DBA-credible choice.
- **`postgres_bloat:sep(59)`** completes the mk_postgres family (header `db;schemaname;tablename;tups;…;totalwastedbytes`, instance marker + db list like the other sections); defaults alert at bloat factor 180/200 %, healthy tables sit at 1.1–1.6.
- Ubuntu 24.04 `/proc/meminfo` has **58 keys** incl. `Zswap`, `Zswapped`, `Unaccepted`, `Balloon`, `DirectMap4k/2M/1G` — match the key set exactly.

## Scaling to the 300-host company (`fleet/` + `snmp/walklib/`)

`--scale company` = the classic roster + ~170 synthesized servers + ~110 SNMP
devices replayed from real walks. Composition is research-backed (FLEET.md).

### Server fleet: ONE process, declarative profiles

Never one process per host at this scale (170 × ~25 MB Python = gone).
`fleet/serve.py` builds every host's agent output on demand from
`fleet/profiles.py` (role, hw, processes, units/services); the shell fetches
per host over HTTP (`/agent/<short>`) and delivers as usual (files or
piggyback). Per-instance variation is seeded from the host name
(`random.Random(f"{short}:fleet-v1")`) — uptime, load jitter, MAC, serials,
UUID differ per instance AND are stable across restarts; counters persist in
one shared state file. The Linux builder is web-frontend-01 parametrized, the
Windows builder win-dc-01's healthy path — all CLAUDE.md parity rules apply
automatically to every instance. Fleet DB hosts run their engine as a
*process only* (no mk_postgres/mysql sections): "plugin not deployed" is
realistic and cheap. Cross-ref realism: VMs are round-robined onto the
`kvm-*` hypervisors **at their own site** (`expand_roster`) and each
hypervisor's ps lists its actual guests as qemu processes (name + memory
match the guest profile); hypervisor AnonPages is derived from the guest RSS
sum, not a static fraction.

### Network topology (parent/child): tier it, don't flat-fan the core

Checkmk validates a host's `parents` at **creation** time (`POST` 400s if the
parent host doesn't exist yet) — so the SNMP network layer is created before
the servers/shell (`setup()`), `setup_snmp` creates devices in **topological
order** (`_topo_order`, parents first; teardown deletes in reverse), and the
host loop creates hypervisors before the VMs that name them (sort by
`parent in carried_fqdns`). Model the path from the monitoring server outward,
**properly tiered — never flat-fan the 12-port core** (its ports are all
uplink trunks; only distribution switches + edge firewalls + WAN/internet
routers hang off it, ~10 children):
- **DC**: `core → sw-dc-dist-0{1,2} → sw-dc-tor-0N → servers`; storage, LBs and
  NTP on the distribution pair; **iDRACs/rack-PDUs/env sensors on the dedicated
  OOB switch** `sw-dc-oob-01` (mgmt network, not the production fabric).
- **HQ**: `core → sw-hq-dist-01 → sw-hq-fNN → endpoints`; a floor's printer
  parents its floor switch (1:1 index), not the comms-room dist.
- **Warehouses**: `device → hall switch → local CPE rt-wh{1,2}-01 (only the 3
  hall switches on its 4-5 LAN ports) → DC head-end rt-wan-01 → core`;
  mezzanine switches daisy-chain off hall 1, comms-room power shares hall 1.
- **A VM is a child of its hypervisor** (`net_parent` in the fleet roster JSON,
  not the profile's vestigial `parent=`); each warehouse has its own hypervisor
  (`wh{1,2}-kvm-01`) so no VM parents across the WAN.

Leaf-placement heuristics that keep it credible: a device parents the switch
in its OWN location string (floor printer → floor switch, not the dist);
mgmt NICs (UPS, PDU, iDRAC) never sit on 10G core/dist trunk ports; a small
CPE router has 4-5 LAN ports, not 12 children; a passive HA firewall port is
near-idle. The SNMP tiering lives in `snmp/netsim.py` `REPLAY_ROSTER` — its
parent column supports per-instance `{n:02d}` patterns (same running index as
the device name, e.g. printers → `"sw-hq-f{n:02d}"`) — plus `SwCore`'s
interface aliases (they name the real uplink peers; sw-access has TWO uplinks,
so the core must show two matching ports). Bump `SCHEMA_VERSION` when the
topology changes. **Applying changes:** agent-host parents update every `up`
(the shell is rebuilt); SNMP parents need netsim to restart — `up` does so on
`--force`, a scale change, or when the netsim code hash changed since launch
(`_netsim_sig` vs `NETSIM_SIGFILE`; else a stale netsim serves old parents and
the fingerprint fast-path skips the update).

### SNMP transport: answer live, don't write walks into the site (no sudo)

netsim's default `--transport snmp` (`snmp/snmpserver.py`) answers SNMP v2c
LIVE: each device binds `127.0.0.<n>:1161` (the whole 127.0.0.0/8 routes to
loopback, so any address binds with no root and no `ip addr add`), and Checkmk
polls it like a real device. This needs NO site filesystem and NO sudo — the
whole reason it exists (the legacy `--transport walk` wrote stored-walk files
into the site-owned `var/check_mk/snmpwalks`, which meant being the site user).
Key points:
- **stdlib-only SNMP** — a compact BER codec + GET/GETNEXT/GETBULK. NO type
  table: every value is served as OCTET STRING, because Checkmk's SNMP layer
  hands check plugins the STRINGIFIED value and an OCTET STRING "12345"
  stringifies identically to a Counter32 12345. Binary walk values (`"AA BB "`
  uppercase-hex) are emitted as raw bytes so Checkmk renders them back to hex.
  Self-test: `python3 snmp/snmpserver.py --selftest` (validated end to end —
  Checkmk discovers if64/CPU/mem/power/UPS services, all green).
- **cmk_setup wiring**: live-SNMP hosts get `ipaddress` (the loopback IP from
  the panel) + `ip-v4-only`, plus ONE folder-wide community rule + port rule
  (`snmp_communities` / `snmp_ports`) instead of a `usewalk_hosts` rule.
- **estate.py** runs netsim as the caller (background process, `PIDFILE_NETSIM`,
  `/admin/shutdown`) — no sudo, no docker-as-site-uid, no /var/tmp copy.
- Cloud/SaaS still can't use SNMP: the responder is on loopback, unreachable
  from a remote Checkmk server (a reachability limit now, not a filesystem one).

### SNMP walk replay (`snmp/curate_walks.py` -> `walklib/` -> netsim)

Real walks are the cheapest realism (real ENTITY trees, sensors, firmware
strings) — replay them instead of synthesizing more vendors. Mechanics:

- **Load once per model into a segment template** (static text chunks +
  sparse dynamic slots); instances render identity (sysName/Location),
  advancing sysUpTime/hrSystemUptime, and counters. 112 devices render in
  <0.5 s. TimeTicks trap: some walks store uptime as `d:hh:mm:ss.cc`, not
  raw ticks — parse both.
- **Counter rates from the recording**: rate = recorded value / recorded
  uptime → busy ports stay busy, dead ports stay dead, totals stay plausible
  against the uptime. Only octets + ucast (32-bit AND HC columns driven by
  the SAME accumulator, 32-bit emitted mod 2^32); errors/discards stay frozen
  (rate 0 = green). hrProcessorLoad gets a gentle wobble; everything else is
  served as recorded.
- netsim reads `walklib/` straight from the repo (it runs as the caller now,
  not the site user) and passes `--walklib snmp/walklib`; no world-readable
  copy is needed since the live transport never touches the site.

### Anonymizing real walks (MANDATORY for zeug_cmk data)

Strip + rewrite at curation time so the checked-in walklib is already clean;
the audit (`curate_walks.py --audit`) prints every surviving string value for
review. What actually leaked in practice (all found by the audit):

- **sysName recurs inside other values** (sysDescr, Kemp/Synology "Linux
  <host> ..." kernel strings) → scrub the recorded sysName everywhere, not
  just OID .1.5.0.
- **Neighbor/client tables**: LLDP/CDP, ARP/routes, bridge FDBs — and the
  non-obvious ones: Aruba CX 802.1X client tables (Kerberos principals
  `host/NB007.<org>.com` + client MACs in the OID index!), Brocade swEvent
  logs (login lines with real admin workstation FQDNs).
- **Org tokens in names**: AP names/SSIDs on the WLC carry site codes,
  HP/ProCurve config tables carry the org's switch-naming scheme, firewall
  cluster/VPN names carry site abbreviations → per-model regex subs with
  deterministic renaming (`{h}` hash keeps distinct names distinct).
- **Serials hide in strings**: APC puts `SN: xxx` inside sysDescr, Fortigate
  `Serial#: xxx` in a version string — OID-based serial rewriting alone is
  not enough, regex the values too.
- **Oddballs**: Meinberg exposes the admin's NAME and the site's GPS
  COORDINATES in its enterprise tree; Synology's kernel cmdline embeds the
  real NIC MACs (`mac1=...`).
- IPv4 values are remapped deterministically into 10.77.0.0/16 with
  lookarounds so dotted OID-valued strings survive.

Trimming the privacy trees is also a 60-75 % size cut (RMON alone was half
the Aruba walk). Never read checks from `.2.1.4/.5/.6/.7/.10/.14/.15/.16/
.17/.25.4-6/.26` — verified unused by cmk plugins before stripping.

### The green pass: real walks alert (and pend!) out of the box

Real devices were recorded in real states — deploy, list every non-OK
service, then fix values per model (`set`/`strip` in curate_walks.py).
What one round of 24 models actually needed:

- **Recorded alerts**: HP switch CPU at 94 % (CRIT at 80/90), unpowered aux
  PSU slots (state 9 -> CRIT), empty printer toners + active prtAlert tables,
  ASA mempools at 95/100 % (normal for an ASA, red for cmk), AKCP dry contact
  in error state, Meinberg GPS antenna disconnected + 90 % flash, iDRAC OS
  volume at 85 %.
- **Recorded timestamps age forever**: Fortigate AV/IPS signature ages go
  CRIT-old permanently -> strip the section; the APC last-self-test date must
  be rendered dynamically (netsim `apcdate`) or the service is UNKNOWN.
- **Static counters don't just flatten graphs — they PEND services forever.**
  `ucd_cpu_util` raises IgnoreResults on a zero jiffie delta EVERY cycle, so
  a static UCD CPU counter means the service never leaves PEND (staleness
  shows the never-checked sentinel, output empty). Dynamize `.2021.11.50-66`
  like the if-counters. Same symptom, different cause: `synology_update`
  status 3 ("Connecting") makes the check yield NOTHING -> pends forever;
  set the recorded status to 2.
- **Defaults that WARN on absent hardware**: raritan_px2 residual current
  WARNs when the PDU has no such sensors (warn_missing_data=True default) —
  tune via one estate-folder rule (cmk_setup), like a real admin would.
- **Upstream bugs surface**: `parse_apc_symmetra_output` crashes on the
  3.0.0 dev branch (ElPhase.from_dict re-parses ReadingWithState ->
  TypeError, from c1802b42504) — output-phase rows removed from BOTH the
  replay walk and the synthetic UPS until fixed upstream.

Verification loop: `estate.py up --force` after service-set changes (the
per-host discovery skip would otherwise keep stale service sets), plain
netsim restart for pure value changes.

## Deploying the estate to a site (`estate.py` + `deploy/cmk_setup.py`)

`estate.py` is the one-command entry (`up`/`down`/`replace`/`status`/`break|degrade|heal`). It runs the delivery shell (`deploy/piggyback/serve.py`, which spawns every `hosts/*/serve.py` as an internal TCP child + a combined `/admin` panel on :8099), optionally the SNMP responder (`snmp/netsim.py`, :8101 panel; answers live SNMP on 127.0.0.0/8:1161, no sudo), then delegates all Checkmk REST wiring to `deploy/cmk_setup.py` (folder tree → hosts → rules → BI pack → discovery → activation; `--remove` tears down). Everything is stdlib + REST (urllib, no redirect-following so async runs can be polled).

### Deployment modes (`--mode`, default self-hosted)

- **self-hosted**: full site-filesystem access. SNMP layer available; agent hosts use **datasource delivery** (below).
- **cloud** (Checkmk Cloud/SaaS): no filesystem access → SNMP layer forced off (`--snmp on` is *rejected*), agent hosts stay on **piggyback delivery** (shell is a TCP host; children arrive as `<<<<host>>>>` blocks). `estate.py:delivery_for()` maps self-hosted→datasource, cloud→piggyback. Mode is in the fingerprint, so switching it forces a reconfigure.

### Datasource delivery ("Individual program call instead of agent access")

Scales better than piggyback (no single-shell fetch bottleneck, no piggyback dependency). Source-verified (`cmk/gui/plugins/wato/datasource_programs.py`, `packages/cmk-check-engine/cmk/checkengine/sources/_builder.py`, `cmk/utils/tags.py`):

- Ruleset is **`datasource_programs`**; value is a plain command string with `value_raw = repr(command)`. We use `cat <dir>/$HOSTNAME$` (monitoring macros supported).
- `_builder._add_agent()` swaps in a `ProgramSource(program=datasource_programs[0])` **instead of** TCP — but only for an `is_tcp`/`is_all_agents_host`. A **`no-agent`** host never calls `_add_agent()`, so it *silently ignores* the rule → the SNMP devices in the same folder tree are unaffected (they're `no-agent`).
- `is_tcp` is purely the agent tag's `tcp` aux-tag (`cmk-agent`/`all-agents`), **independent of address family**. So datasource hosts are **`cmk-agent` + `no-ip` + `no-piggyback`**: the program runs, no IP/ping is needed, and `no-piggyback` skips the always-added empty PiggybackSource (`_builder` adds one to every host unless tagged `no-piggyback`).
- The **shell stays `all-agents`** so it gets BOTH its own data (datasource program on its own file) AND the BI special agent; no agent-port rule / no TCP in this mode.
- **One rule per site**: created on the estate ROOT folder with empty conditions — Checkmk rules apply to a folder *and its subfolders*, so a single `cat $HOSTNAME$` rule covers the whole tree. The rule `folder` field takes the `~`-ident (`~meridian_demo`), same notation as `folder_config`.
- **The files**: the shell (`DELIVERY_MODE=datasource`) writes each host's full agent output + its own minimal section, atomically (tmp+rename), **world-readable (0644)**, to `/var/tmp/cmk-demo-agent-output` — deliberately NOT under the site: `cat` runs as the site user and reads any world-readable path, so no sudo/site-user write is needed (SNMP is decoupled the same way now — netsim answers live over loopback instead of writing walks into the site). File name = the FQDN (= `$HOSTNAME$`). Docker runtime bind-mounts that host dir into the container. The shell writes once BEFORE opening its panel, so files exist before discovery runs; a writer thread refreshes every ~20 s. In datasource mode `build_delivery_output()` emits ONLY the shell's minimal section (no piggyback wrapping).

### Folder taxonomy (both modes)

Hosts are sorted into a role subfolder tree under the estate root (Applications, Databases, Storage, Infrastructure, Windows servers, Network → {Switches, Routers & WAN, UPS & power}) so it reads like real infra. Classified by short-name prefix (`_agent_role`/`_snmp_role`) so replicas land beside their originals. `folder_ident(*parts)` builds `~a~b` idents (`~` = root and separator); `ensure_folder_chain` creates nested chains parents-first (folder created with `name`=last segment + `parent`=parent ident). `prune_subtree` manages the whole subtree and is **guarded against a site-root (`~`) estate** so it can never mass-delete a shared site.

### Fingerprint fast-path (skip the slow re-run)

Setup stores a SHA of the *intended* config as host label `meridian_demo/fingerprint` on the shell. An unchanged `up` short-circuits in ~1 s (no heal/discovery/activation). Canon covers mode, root folder, shell host/ip/port, every host's `(fqdn, effective parent, role)`, the SNMP set, applicable BI tiers, and the datasource command — everything that changes Setup objects but NOT live metric values (they never change the service set). `SCHEMA_VERSION` is mixed in and prefixes the fingerprint (`v3-…`): **bump it whenever a change alters which services a host discovers** (a fake agent starts/stops emitting a section) or the shape of created objects — it invalidates every stored fingerprint and forces one full pass. Pure value changes need NO bump. `--force` overrides the fast-path.

### Per-host discovery skip

Even on a changed/forced run, `discover()` skips a host that already has **monitored** services (`GET /objects/service_discovery/<host>` → `check_table` entries with `service_phase == "monitored"`; that endpoint uses `DiscoveryAction.NONE` = cheap, reads stored state, no data-source fetch). So a roster that grew by one host only pays for that host. **The shell is never skipped** (`allow_skip=False`): its refresh re-delivers piggyback to any new child AND the final pass must re-scan for the BI aggregation service (which only appears once the estate is live). `--force` sets `allow_skip=False` everywhere.

### `replace` / `redeploy`

`estate.py replace` (alias `redeploy`) = `down` then `up` with the up flags. Teardown deletes the shell host and its fingerprint label, so the following `up` always runs the full discovery/activation.
