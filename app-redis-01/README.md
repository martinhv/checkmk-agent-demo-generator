# Demo: `app-redis-01` — maxmemory eviction storm (TTL regression)

The session + cache store of the Meridian Retail estate (see `../FLEET.md`): a
Redis 7 server (`redis-server.service`) that payment-api and app-worker-01 read
and write. The incident is a **maxmemory eviction storm**. A bad deploy ships
cache keys with no / huge TTLs, so `used_memory` climbs to `maxmemory` (~6 GiB);
redis starts evicting under its `allkeys-lru` policy, the eviction rate spikes,
the keyspace hit-ratio collapses, clients block on the evictor, and command
latency rises. *"Explain with AI"* fuses redis `used_memory`-at-`maxmemory` +
`evicted_keys/s` + the hit-ratio drop + `blocked_clients` into the root cause: a
TTL regression flooded the cache; evictions are thrashing it — **fix the key
TTLs, don't just raise `maxmemory`**.

- **TCP 6556** — Check_MK agent output (plaintext; the fetcher sees `<<` →
  `TransportProtocol.PLAIN`, no TLS/registration). Controller-status section
  pretends the host is TLS-registered (no "TLS not activated" warning).
- **TCP 8080** — `/admin` control UI + curl toggle API. Nothing here is
  monitored — this story has no HTTP check.

The redis data comes from the `mk_redis` agent plugin's `<<<redis_info:sep(58)>>>`
section (a verbatim `redis-cli info` dump), parsed by
`cmk/plugins/redis/agent_based/redis_base.py` into the service item
`MERIDIAN_CACHE` → `Redis MERIDIAN_CACHE Server Info` / `Clients` /
`Persistence`.

## The incident it fakes

| Service / metric | Healthy | Degraded | Broken | Role |
|---|---|---|---|---|
| **Redis … Persistence** | OK — last RDB save ok | OK — saves still ok | **WARN** — `rdb_last_bgsave_status:err` (the bgsave fork can't allocate under memory pressure) | the **default-alerting** redis lever (default state WARN) |
| Redis memory (`used_memory` vs `maxmemory`) | ~40 % (2.4 / 6 GiB) | climbs toward the 6 GiB cap | **pinned at 6 GiB** | the headline; graph-only (no default level) |
| Eviction rate (`evicted_keys/s`) | 0 | a trickle (begins as it nears the cap) | **storms (thousands/s, live-growing)** | graph-only; the arrow at the cause |
| Keyspace hit ratio (`hits/(hits+misses)`) | ~99 % | slips (~85 %) | **collapses (~60 %)** | graph-only corroboration |
| Redis … Clients (`blocked_clients`) | 0 | 0 | **> 0** | graph-only (no default level) |
| **Linux Memory / Swap** | OK (~52 % used) | OK | **OK** — redis evicts, it does NOT OOM the box | proves the box is fine; the story is the *app* |
| CPU load | ~0.5 | a hair higher | **GREEN** (15-min ≪ 20 WARN) | proves CPU is not the cause |
| everything else (filesystems, network, time sync, APT, disk SMART, jobs, all ~30 systemd units) | OK | OK | OK | low-noise: one root cause |

**Why the host's own Linux Memory stays GREEN (deliberate).** Redis enforces
`maxmemory` and *evicts* rather than growing without bound, so `/proc/meminfo`
never goes red: as redis' anon working set grows the kernel reclaims page cache
to make room (`Cached` shrinks from ~6.5 → ~2.4 GiB), leaving ~2.7 GiB free even
at the cap, and swap is never touched. The whole story lives in the redis
*application* metrics, not the Linux Memory check — exactly the cross-signal an
AI is good at and a "just add RAM" reflex gets wrong.

**Which redis checks alert by default (verified against the 2.5/2.6 source).**
Only three redis check plugins exist — `redis_info` (Server Info),
`redis_info_clients`, `redis_info_persistence` — and **only persistence alerts
by default**: `rdb_last_bgsave_state` defaults to **WARN** when the last RDB save
was faulty (`cmk/plugins/redis/agent_based/redis_info_persistence.py:122-133`,
ruleset `…/rulesets/redis_info_persistence.py:23-28`). The clients check's
levels (`connected/output/input/blocked`) all default to `("no_levels", None)`
(`…/agent_based/redis_info_clients.py:79-84`), and there is **no used_memory /
maxmemory / evicted_keys / hit-ratio check plugin** in the free tree — those are
**graph-only** here. So, like the dying-disk demo documents for "Disk IO": to
turn the eviction storm itself into a hard alert, add a rule (see step 3 below).
Out of the box the **Persistence WARN is the redis service that goes red**, and
the graphs supply the AI everything it needs.

**Three states, because the timeline matters:**

- `healthy` → all green. `used_memory` ~40 % of the 6 GiB `maxmemory`, hit ratio
  ~99 %, 0 evictions, no blocked clients.
- `degraded` → the bad deploy lands: `used_memory` climbs toward `maxmemory`
  over `LEAK_FILL_MIN` (~15 min), `evicted_keys/s` rises off zero, the hit ratio
  *slips* — the breadcrumb (graph-visible). RDB saves still OK, no blocked
  clients. **Trigger ~18 min before showtime.**
- `broken` → `used_memory` pinned at `maxmemory`: heavy `evicted_keys/s`
  (counter climbs live across re-polls), `blocked_clients > 0`, hit ratio
  collapsed, and the background RDB save fails → **Redis … Persistence WARN**.
  `degraded` **auto-escalates to `broken` after `AUTO_BREAK_AFTER_MIN`** (default
  18 min); the broken impact ramps over `BREAK_RAMP_MIN` (~3 min), no cliff.

---

## 1. Run it

```bash
cd app-redis-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6561** (agent) and **8091** (admin). Stdlib-only,
so without Docker:

```bash
AGENT_PORT=6561 HTTP_PORT=8091 START_STATE=healthy python3 serve.py
```

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `app-redis-01.corp.meridian-retail.com`, IP `127.0.0.1`, **Checkmk
   agent port → 6561**.
2. Service discovery (any state — no discovery-time baselines here). Activate.
   Everything green. You'll see `Redis MERIDIAN_CACHE Server Info`, `… Clients`
   and `… Persistence`.

## 3. (Optional) make the eviction storm a hard alert

Out of the box the redis red is the **Persistence WARN**. To page on the storm
directly (turning the graph-only signals into alerts), add either:

- *Setup → Service monitoring rules → **Redis clients*** → set **Upper levels on
  the total number of clients pending on a blocking call** (e.g. WARN 1 / CRIT
  10) — `blocked_clients` then alerts in `broken`.
- *Setup → Service monitoring rules → **Redis persistence*** → set **State when
  last RDB save operation was faulty** to **CRIT** — the `broken` Persistence
  service then goes CRIT instead of WARN.

(The `used_memory`/`evicted_keys`/hit-ratio metrics have no free check plugin,
so they remain graph-only — perfect AI corroboration, like Disk-IO latency on
the dying-disk demo.)

## 4. Demo choreography

| When | Action | What Checkmk shows |
|---|---|---|
| T−18 min | `curl localhost:8091/admin/degrade` | nothing red yet. The redis **Memory graph** climbs toward `maxmemory`; `evicted_keys/s` lifts off zero and the **hit-ratio graph** starts slipping |
| ~T | *nothing* — degraded **auto-escalates after 18 min** (or `/admin/break`) | `used_memory` pinned at 6 GiB; eviction storm (thousands/s); hit ratio collapses; `blocked_clients > 0`; **Redis … Persistence → WARN** (bgsave failing) |
| the page | open the Redis Persistence WARN (or the blocked-clients alert if you set the rule) | "last RDB save faulty" / "clients blocking". Instinct: just `redis-cli BGSAVE` again, or bump `maxmemory` |
| ⭐ Explain with AI | one click | fuses `used_memory` == `maxmemory` + `evicted_keys/s` storming + hit ratio ~60 % + blocked clients + **flat green Linux Memory/CPU** → *"a TTL regression flooded the cache; allkeys-lru is thrashing it — fix the key TTLs; raising maxmemory only delays it"* |
| resolve | `curl localhost:8091/admin/heal` ("TTLs fixed / bad keys flushed") | next poll: `used_memory` drops to ~40 %, evictions back to 0, hit ratio recovers, bgsave ok |

**Control UI:** `http://localhost:8091/admin` — state badge, time-in-state,
live `used_memory` / hit-ratio / eviction-rate / blocked-clients, per-state
effect cards with toggle buttons, 5 s auto-refresh.

```bash
curl http://localhost:8091/admin/degrade   # bad deploy lands (memory climbs, evictions begin)
curl http://localhost:8091/admin/break      # eviction storm (Persistence WARN, hit ratio collapses)
curl http://localhost:8091/admin/heal        # TTLs fixed, all green
curl http://localhost:8091/                  # JSON: state, used_memory_pct_of_maxmemory, hit_ratio_pct, evicted_keys_per_s, blocked_clients, ...
```

## 5. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `app-redis-01.corp.meridian-retail.com` | name in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published 6561) |
| `HTTP_PORT` | `8080` | admin port (published 8091) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AUTO_BREAK_AFTER_MIN` | `18` | minutes in `degraded` before the eviction storm auto-fires (`0` = never) |
| `LEAK_FILL_MIN` | `15` | minutes for `used_memory` to fill to `maxmemory` while degraded |
| `BREAK_RAMP_MIN` | `3` | minutes for the broken impact (eviction peg, hit-ratio collapse, bgsave failure) to reach full force |
| `STATE_FILE` | `/var/tmp/cmk-demo-app-redis-state.json` | persists counters/uptime/incident across restarts (`""` = off) |
