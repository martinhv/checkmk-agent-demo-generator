# Demo: `app-worker-01` — memory leak → OOM kill → service flap

The order/settlement worker of the Meridian Retail estate (see `../FLEET.md`):
a Java service (`order-worker.service`) that drains the payments job queue. The
incident is the **mirror image of the dying-disk db host** — there the
CPU-load page was a red herring; here the resource exhaustion is **real**. A
heap leak fills RAM, spills into swap (major-fault thrash), and the OOM killer
finally reaps the JVM, so the worker flaps. *"Explain with AI"* fuses Memory +
Swap + major page faults + the failed unit into the root cause: a heap leak in
the worker — fix the app, don't add RAM.

- **TCP 6556** — Check_MK agent output (plaintext; the fetcher sees `<<` →
  `TransportProtocol.PLAIN`, no TLS/registration). Controller-status section
  pretends the host is TLS-registered (no "TLS not activated" warning).
- **TCP 8080** — `/admin` control UI + curl toggle API. Nothing here is
  monitored — this story has no HTTP check.

## The incident it fakes

| Service | Healthy | Degraded | Broken | Role |
|---|---|---|---|---|
| **Memory** | virtual (RAM+swap) ~32 %, committed ~57 % | **WARN** — `Committed_AS` crosses the commit limit (12.19 GiB) as the JVM commits heap; virtual climbs toward 80 % | **CRIT** — virtual (RAM+swap) > 90 % (default levels 80/90) | the headline; the leak made visible |
| **Systemd Service Summary** | OK | OK — worker still up | **CRIT** — `order-worker.service` FAILED (OOM-killed) | the symptom that pages |
| **Kernel / Page faults** | ~1 major fault/s | hundreds/s (swap-in thrash) | pinned (heavy thrash) | the arrow at memory, not CPU |
| **Swap** (Memory graph) | 0 % | climbing from 0 | ~94 % used | corroboration |
| **CPU load** | ~0.7 | elevated (GC + swap-in) | elevated but **GREEN** (15-min < 20 WARN) | proves CPU is *not* the cause |
| everything else (filesystems, network, time sync, APT, disk SMART, jobs) | OK | OK | OK | low-noise: one root cause |

The Memory check derives `MemUsed = MemTotal − MemFree − Caches`; under the
leak the page cache is reclaimed (low `Cached`) and free memory collapses, so
`MemUsed` climbs and — once swap engages — virtual usage crosses the levels.
The full `/proc/meminfo` (58 keys, Ubuntu 24.04 key set) keeps the LRU
arithmetic self-consistent (`Active(anon)+Inactive(anon) = AnonPages+Shmem`,
etc.), so a kernel-literate viewer can sum it.

**Three states, because the timeline matters:**

- `healthy` → all green. ~6.5 GiB of 16 used, swap empty, worker draining the queue.
- `degraded` → the heap leak grows over `LEAK_FILL_MIN` (~18 min): RAM climbs,
  swap starts filling, major page faults spike. The Memory service crosses
  **WARN on Committed_AS** while the swap graph and virtual usage climb — the
  breadcrumb. The worker is still up. **Trigger ~20 min before showtime.**
- `broken` → memory is full: virtual usage > 90 % → **Memory CRIT**, and the
  OOM killer reaps the JVM → `order-worker.service` **failed** → **Systemd
  Service Summary CRIT**. The worker flaps (OOM-kill/restart count climbs live
  on `/admin`). `degraded` **auto-escalates to `broken` after
  `AUTO_BREAK_AFTER_MIN`** (default 20 min — the OOM kill firing); the broken
  impact (swap peg → virtual CRIT) ramps over `BREAK_RAMP_MIN` (~4 min), no
  vertical cliff.

---

## 1. Run it

```bash
cd app-worker-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6562** (agent) and **8092** (admin). Stdlib-only,
so without Docker:

```bash
AGENT_PORT=6562 HTTP_PORT=8092 START_STATE=healthy python3 serve.py
```

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `app-worker-01.corp.meridian-retail.com`, IP `127.0.0.1`, **Checkmk
   agent port → 6562**.
2. Service discovery (any state — no discovery-time baselines here, unlike the
   SMART check on the db host). Activate. Everything green.

No extra rules are needed: the Memory virtual-usage levels (80/90) and the
Systemd Service Summary are defaults.

## 3. Demo choreography

| When | Action | What Checkmk shows |
|---|---|---|
| T−20 min | `curl localhost:8092/admin/degrade` | nothing red yet. The Memory service edges to **WARN** (Committed_AS over the limit); the swap graph and major-page-faults graph start climbing |
| ~T | *nothing* — degraded **auto-escalates (OOM) after 20 min** (or `/admin/break`) | virtual memory ramps past 90 % → **Memory CRIT**; `order-worker.service` → **failed** → **Systemd Service Summary CRIT** |
| the page | open the Systemd Service Summary CRIT | "order-worker failed". Instinct: just restart it / give it more heap |
| ⭐ Explain with AI | one click | fuses Memory CRIT + swap full + major-fault thrash + the OOM-killed unit + flat-but-elevated CPU → *"the worker has a heap leak; it filled RAM, thrashed swap and was OOM-killed — fix the leak, more RAM only delays it"* |
| resolve | `curl localhost:8092/admin/heal` ("patched worker deployed") | next poll: memory drains, swap frees, unit back to running |

**Control UI:** `http://localhost:8092/admin` — state badge, time-in-state,
"heap leaking for…", live OOM-kill count, per-state effect cards with toggle
buttons, 5 s auto-refresh.

```bash
curl http://localhost:8092/admin/degrade   # leak grows (Memory WARN)
curl http://localhost:8092/admin/break      # OOM kill (Memory CRIT + unit failed)
curl http://localhost:8092/admin/heal        # patched, all green
curl http://localhost:8092/                  # JSON: state, memory_pressure_pct, oom_kills, ...
```

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `app-worker-01.corp.meridian-retail.com` | name in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published 6562) |
| `HTTP_PORT` | `8080` | admin port (published 8092) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AUTO_BREAK_AFTER_MIN` | `20` | minutes in `degraded` before the OOM kill auto-fires (`0` = never) |
| `LEAK_FILL_MIN` | `18` | minutes for the leak to fill memory while degraded |
| `BREAK_RAMP_MIN` | `4` | minutes for the broken impact (virtual CRIT) to reach full force |
| `STATE_FILE` | `/var/tmp/cmk-demo-app-worker-state.json` | persists counters/uptime/incident across restarts (`""` = off) |
