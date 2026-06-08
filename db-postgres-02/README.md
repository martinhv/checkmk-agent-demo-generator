# Demo: the PostgreSQL read replica with a connection leak (`db-postgres-02`)

A throwaway container that impersonates the **read replica** of the Meridian
Retail payments database (`db-postgres-01`'s hot-standby sibling). It streams
WAL from the primary and serves the **analytics / reporting** read traffic. Its
hardware is **healthy** — this is a *different failure class* from
db-postgres-01's fail-slow dying disk. The incident is **connection-pool
exhaustion**: a runaway BI/reporting client (a connection leak in the analytics
service) opens PostgreSQL connections and never closes them, so the connection
count climbs toward `max_connections` until new clients can't connect.

This is the demo for **the one PostgreSQL check that alerts by default**:
`postgres_connections` warns at **80 %** and crits at **90 %** of
`max_connections`. Everything else stays green and corroborates.

- **TCP 6556** (published as **6565**) — emits real Check_MK agent output
  (plaintext, same trick as the other demos: the fetcher sees `<<` →
  `TransportProtocol.PLAIN`, no TLS, no registration). Includes the full
  **mk_postgres plugin sections**, so the host carries real PostgreSQL
  services. The controller-status section **pretends the host is
  TLS-registered** (`allow_legacy_pull: false` + a registered pull connection
  with a cert expiring in ~324 days), so the Check_MK Agent service shows **no
  "TLS is not activated" warning**.
- **TCP 8080** (published as **8095**) — the **control UI** at `/admin` (state
  badge, time-in-state, one card per state with its effects, toggle buttons;
  auto-refreshes every 5 s) plus the same toggles as a curl API.

## The incident it fakes

`max_connections` is **200**. The leak drives the count up; the connections are
mostly **idle / idle-in-transaction** (a leak holds slots, it doesn't run
queries), so the `idle` connection percentage is the one that trips the levels.

| Service | Healthy | Broken | Role in the story |
|---|---|---|---|
| **PostgreSQL Connections MAIN/analytics** | ~30/200 (15 %) | **CRIT** — idle connections **> 90 %** of max_connections (> 180/200), count **grows live** toward max | the **headline** alarm — the only thing red |
| **PostgreSQL Daemon Sessions MAIN** | ~30 (a few running) | **~190 total**, the great majority *idle / idle-in-transaction* (the leaked BI sessions) | the pile-up that explains *why* |
| **PostgreSQL Connection Time MAIN** | ~0.01 s | **~1.5 s** — every new client waits for a free slot | "even connecting is slow now" |
| **PostgreSQL DB MAIN/analytics Statistics** | numbackends ~30, ~85 read-only commits/s | numbackends **~190** (matches sessions + ps + connections), throughput roughly flat | the machine is fine — slots, not load |
| **PostgreSQL Query Duration MAIN/analytics** | a few short analytics scans | one **idle-in-transaction** backend held open since the runaway (age grows live) | the leaking client, caught in the act |
| **CPU load / CPU utilization** | load ~1.5, mostly idle | load nudges up but **stays GREEN** (15-min well under the 20 WARN) | not a compute problem |
| **Memory** | OK | **still OK** (a few hundred MB of backend RAM, nowhere near a level) | not a memory problem |
| **Disk IO / Filesystems / SMART** | OK, calm SSDs | **still OK** | healthy hardware — the opposite of db-postgres-01 |
| everything else (postgres unit/instance/version, locks, vacuum/analyze, bloat, systemd ~31 units, time sync, APT, network, jobs) | OK | **still OK** | low noise, one root cause |

The AI fuses **connections % near max + the idle-in-transaction backend
pile-up + the slow connect time** into the diagnosis: *a client is leaking
connections toward `max_connections`; new clients can't get in — kill the
leaking BI session / add connection pooling*. (`pgbouncer.service` is present
and running in the agent output — the obvious "add pooling" lever.)

The memory section is a full `/proc/meminfo` (58 keys, identical key set to a
real Ubuntu 24.04 host), so the Memory service yields the complete metric set.
The section layout is diffed against a real 2.5 Linux agent dump — incl. agent
controller status, deployed-plugin list (`mk_postgres`), both `lnx_if` variants,
mounts, systemd-timesyncd (dynamic sync timestamps) and APT.

A **read-replica detail**: the agent reports the standby's recovery activity in
`ps` — a `startup recovering …` process and a `walreceiver streaming …`
backend — so the host reads as a genuine hot standby. There is **no stock
PostgreSQL replication-lag agent section**, so none is invented; the story
stays entirely within the verified postgres checks.

### Three states, because the timeline is part of the story

- `healthy` → all green. ~30/200 connections, the replica streaming and
  serving reads.
- `degraded` → the BI client starts leaking: the connection count climbs
  **90 → ~150/200** — approaching, but staying **under** the 80 % WARN (160) —
  while the idle backends and connect time creep up. The breadcrumb. **Trigger
  ~15–20 min before showtime.** `degraded` **auto-escalates to `broken` after
  18 min** (`AUTO_BREAK_AFTER_MIN`, 0 disables); the /admin screen shows the
  countdown.
- `broken` → the leak runs away: connections cross the **90 % CRIT** (> 180)
  and keep **growing live** poll-by-poll, capped a few slots under max (the
  leak literally can't open the last connections). The impact ramps in over
  `BREAK_RAMP_MIN` (default 4 min), no vertical cliff.

## 1. Run it

```bash
docker compose up --build -d
docker compose logs -f          # watch [boot]/[ctl]/[state]
```

The agent is on `127.0.0.1:6565`, the control UI on
<http://localhost:8095/admin>.

Sanity-check the agent output and the connection count by hand:

```bash
nc 127.0.0.1 6565 | grep -A4 '<<<postgres_connections'
# analytics;200;<idle>;<active>   ->  idle/200 crosses 80 %/90 %
curl -s localhost:8095/ | python3 -m json.tool   # JSON status incl. connections_pct
```

Toggle states:

```bash
curl localhost:8095/admin/degrade   # start the leak (climb to ~150/200, still OK)
curl localhost:8095/admin/break     # runaway -> Connections CRIT, grows live
curl localhost:8095/admin/heal      # back to ~30/200, all green
```

## 2. Set it up in Checkmk (before the talk)

1. **Add the host** `db-postgres-02` with IP address `127.0.0.1`.
2. Override the agent port: rule *"Checkmk agent port"* (or the host's
   *Connection* attributes) → **6565**.
3. The host speaks plaintext TCP — no TLS/registration needed. (The
   controller-status section makes the *Check_MK Agent* service read as
   TLS-registered, so you won't see a "TLS is not activated" warning.)
4. Run **service discovery** and add all services. You can discover in any
   state here (unlike db-postgres-01, there is no SMART-baseline trap), but
   discovering while **healthy** gives the cleanest "all green" starting board.
5. You should see the **PostgreSQL Connections MAIN/analytics** and
   **MAIN/postgres** services, **Daemon Sessions MAIN**, **Connection Time
   MAIN**, **Instance/Version MAIN**, the per-DB **Statistics**, plus the
   standard Linux services — all OK.

No extra ruleset is required: the connection levels (80 %/90 %) are the
check's defaults.

## 3. Demo choreography

1. **~18–20 min before** the segment: `curl localhost:8095/admin/degrade`.
   The connection graph for *MAIN/analytics* starts climbing from ~30 toward
   ~150; idle sessions and connect time creep. Nothing is red yet — this is the
   breadcrumb you can point back to ("the leak began ~20 minutes ago").
   (Or rely on the auto-escalation and just leave it in `degraded`.)
2. **At showtime**, either let the auto-escalation fire or
   `curl localhost:8095/admin/break`. Within the ramp, **PostgreSQL
   Connections MAIN/analytics** crosses 80 % → WARN, then 90 % → **CRIT**, and
   the connection count keeps growing live across refreshes (toward, but never
   reaching, 200).
3. Open the service. Note the corroborating, *green* signals: **Daemon
   Sessions** shows ~190 connections almost all idle, **Connection Time** has
   crept to ~1.5 s, **DB Statistics** numbackends matches — while **CPU,
   Memory, Disk, SMART** are all fine.
4. Run **Explain with AI**. The expected diagnosis: a client is **leaking
   connections** toward `max_connections`; the database and host are healthy —
   the fix is to **kill the leaking BI/reporting session and put the analytics
   workload behind a connection pooler** (pgbouncer is already installed),
   *not* to raise `max_connections` or add hardware.
5. `curl localhost:8095/admin/heal` to reset between runs.

## 4. Why the states produce exactly those service results

- **The lever is `postgres_connections`** (`cmk/plugins/postgres/agent_based/
  postgres_connections.py`): default parameters
  `levels_perc_active = (80.0, 90.0)` and `levels_perc_idle = (80.0, 90.0)`
  (lines 157–160). The check reads `mc` (max_connections) from the agent line
  and computes `used_perc = current / mc * 100` **separately** for the active
  and idle connection counts (lines 117–142). The leaked connections are
  **idle**, so `idle / 200` is what crosses 80 %/90 %. The service item is
  `PostgreSQL Connections MAIN/analytics` (the instance marker `[[[main]]]` is
  uppercased and joined with the db name by `parse_dbs`).
- **The agent line format** is `datname;mc;idle;active` under
  `<<<postgres_connections:sep(59)>>>`, inside `[databases_start]` …
  `[databases_end]` followed by the header row. `parse_dbs`
  (`cmk/plugins/postgres/lib.py`) drops the leading `datname` header/column and
  zips the rest, so `analytics;200;185;4` → `{mc: 200, idle: 185, active: 4}`.
- **Everything agrees by construction.** A single function (`connection_counts`)
  produces `(idle, active)`; the **Daemon Sessions** `t`/`f` lines, the
  **pg_stat_database** `numbackends` for `analytics`, the `ps_lnx` client
  backends, and the `tcp_conn_stats` established count are all derived from
  those same two numbers — so they can never tell different stories (the
  CLAUDE.md cross-check). `postgres_sessions` reports `t` = idle, `f` = running;
  `postgres_conn_time` is the bare connect-time float per instance.
- **Why nothing else moves.** A connection leak is not a CPU, memory, or disk
  problem. CPU load only nudges (stays green), Memory adds a few hundred MB of
  backend RSS (nowhere near a level), disks/SMART are untouched. Low noise, one
  root cause.
- **Grows live, capped under max.** In `broken`, the count integrates a runaway
  ramp plus a slow creep that asymptotes to `MAX_CONNECTIONS − 6`, so re-polls
  on stage show the incident actively worsening without the impossible-looking
  "exactly 200/200".
- **Counters never go backwards** across restarts/redeploys: all rate counters
  (CPU, kernel, diskstat, network, pg_stat_database) and the incident state are
  persisted to `STATE_FILE` (atomic tmp+rename) so Checkmk's rate-based checks
  don't go stale (see CLAUDE.md).

## 5. Config (env vars)

| Env var | Default | Effect |
|---|---|---|
| `CMK_HOSTNAME` | `db-postgres-02` | hostname baked into the agent output |
| `AGENT_PORT` | `6556` | TCP port for the agent (container side; published as 6565) |
| `HTTP_PORT` | `8080` | TCP port for the admin/toggle UI (published as 8095) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` at boot |
| `MAX_CONNECTIONS` | `200` | the connection-levels denominator (`mc`) |
| `AUTO_BREAK_AFTER_MIN` | `18` | minutes in `degraded` before the leak auto-runs-away to `broken` (0 disables) |
| `LEAK_FILL_MIN` | `16` | minutes for the leak to climb ~90 → ~150 connections while degraded |
| `BREAK_RAMP_MIN` | `4` | minutes for the broken impact (CRIT connections / connect-time creep) to reach full force (0 = instant) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the `<<<check_mk>>>` header |
| `STATE_FILE` | `/var/tmp/cmk-demo-db-postgres-02-state.json` | persistence for counters/uptime/incident state (`""` = off) |
