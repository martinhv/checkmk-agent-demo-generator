# Demo: the fake "black-box dependency" host

A throwaway container that impersonates the unmonitored dependency from the
keynote (`payment-api`) so you can **add it live** during the Theme 1 demo:

- **TCP 6556** — emits real Check_MK agent output (in plaintext) → the host gets
  a full set of services (Memory, CPU, Filesystems, Uptime, Interface, TCP
  connections, Systemd, timesyncd, APT, …) that matches a real Ubuntu 24.04
  agent dump section for section.
- **TCP 8080** — an HTTP endpoint for **`check_http` / `check_httpv2`**, plus
  the **`/admin` control UI** (state cards + toggle buttons).
- A **break/heal toggle** flips *both* at once, so the incident is coherent.

> **Why plaintext works with no TLS/registration:** the Checkmk 2.5 fetcher reads
> the first two bytes of the stream. Our output starts with `<<<check_mk>>>`, so
> those bytes are `<<` = `TransportProtocol.PLAIN`. With no encryption ruleset the
> server default is `ANY_AND_PLAIN`, so a plain, unregistered agent is accepted.
> (Source: `check_mk:packages/cmk-check-engine/cmk/fetchers/_tcp.py`,
> `parsed_encryption_handling` + `validate_agent_protocol`.)
>
> The agent additionally fakes a **registered controller** in
> `<<<cmk_agent_ctl_status>>>` (`allow_legacy_pull: false` + a pull connection
> with a cert ~330 days out), so the *Check_MK Agent* service reads as properly
> TLS-registered — no "TLS is not activated" WARN, nothing to explain away.

---

## 1. Run it

```bash
cd demo_broken_http_service
docker compose up --build -d
docker compose logs -f          # watch [boot] / [ctl] / [state] lines
```

It starts **broken on purpose** (that's the incident). Ports are published on
`127.0.0.1` as **`6557`** (agent) and **`8080`** (HTTP). Port 6557 is used because
the laptop's own Checkmk agent already owns the default 6556 — they run side by
side. Change the left-hand side in `docker-compose.yml` if needed.

No Docker? It's stdlib-only Python (here AGENT_PORT is the listen port directly):

```bash
AGENT_PORT=6557 HTTP_PORT=8080 START_BROKEN=1 python3 serve.py
```

Sanity-check the agent stream and the endpoint:

```bash
nc 127.0.0.1 6557 | head            # should print <<<check_mk>>> ...
curl -i http://127.0.0.1:8080/      # 503 + ~1.5 s while broken, 200 after heal
```

**Restart-proof:** counters, fake uptime and the incident state persist to a
JSON state file (a named volume in docker-compose). Restarting or redeploying
the container does NOT reset Checkmk's rate-based services (which would go
stale on backwards counters) and does not reset a running incident mid-demo.

---

## 2. Add it in Checkmk (the live part of the demo)

**Host:**
1. *Setup → Hosts → Add host*. Name `payment-api.corp.meridian-retail.com`, IP = the machine running the
   container (or the container IP if your site can reach it directly).
2. Set the **Checkmk agent port** override to **`6557`** (the demo's published port;
   the default 6556 is your laptop's own agent).
3. Save → the **DNS/ping validation** and **agent connection test** run right there
   (the ⭐ host-setup beat). Run **service discovery** → the agent services appear.
   *(No registration needed — and the Check_MK Agent service reads as
   TLS-registered, see above.)*

**HTTP check (the ⭐ "found in one search → HTTP rule" beat):**
- Global-search for **HTTP** → *Check HTTP web service* (`check_httpv2`) rule.
- Endpoint URL `http://<host>:8080/`, assign to `payment-api`.
- Activate → the service goes **CRIT (503, ~1.5 s)** — the smoking gun the app's
  RED metrics were pointing at.

---

## 2b. What services you get

**Design choice — low noise, one root cause.** The host stays *healthy in both
states*: memory ~40 %, load < 1 per core, disks idle, filesystems ~38 %/65 %
(and visibly growing/being cleaned — no static fakes). The break flips exactly
two reds — the **symptom** (HTTP 503) and the **root cause**
(`payment-worker.service` failed) — plus *graph-visible corroboration that
never alerts*:

- 3 of 4 gunicorn workers gone; the **survivor leaks ~6 MB/min** — its RSS and
  the host's AnonPages grow live, poll by poll
- **TIME_WAIT creeps up** (clients retrying the fast-failing endpoint),
  ESTABLISHED dips
- **tx throughput collapses** 320 → ~55 kB/s over ~3 min (503 bodies are tiny),
  rx ticks up slightly (retries)
- load *dips* slightly — the box is doing less work; "the host looks fine" IS
  the story

| Section | Service(s) | Auto-discovered? | Broken state |
|---|---|---|---|
| `mem` / `cpu` / `df_v2` / `uptime` / `lnx_if` / `tcp_conn_stats` / `kernel` / `diskstat` / `timesyncd` / `apt` / `mounts` | Memory, CPU load, CPU utilization, Filesystems, Uptime, Interface, TCP Connections, Kernel, Systemd Timesyncd Time, APT Updates… | ✅ yes | **all OK** (corroborating drifts visible in graphs only) |
| `systemd_units` | **Systemd Service Summary** (Total: ~32 units) | ✅ yes | **CRIT** — `payment-worker.service` failed ← **root cause** |
| `systemd_units` | `Systemd Service payment-worker` (individual) | ⚙️ needs the *Systemd single services* discovery rule | **CRIT** while broken |
| `job` | **Job settlement-batch**, **Job log-archive** | ✅ yes | both **OK** |
| `ps` | count of gunicorn workers | ⚙️ needs a *State and count of processes* rule (match `gunicorn`) | 4 workers → **1**, survivor's memory growing |
| **`check_http`** (active check, not the agent) | **HTTP payment-api** | the rule you add | **CRIT** — 503, slow ← **symptom** |

The two ⚙️ rows are good *live* beats: "let me also discover the systemd service /
the worker processes" → add the rule → services appear. The rest show up from
plain discovery.

## 3. Demo choreography (matches Theme 1)

| Beat | Action | What the room sees |
|---|---|---|
| Setup | container is up & **broken** | nothing in Checkmk yet — it's the blind spot |
| Add host ⭐ | add `payment-api.corp.meridian-retail.com`, discovery | DNS/ping OK, agent test OK, services found |
| HTTP check | add the `check_httpv2` rule | **CRIT** — service returns 503, slow |
| Correlate | open the host | wall of green + **Systemd Service Summary CRIT**: `payment-worker.service` failed — two reds, one story |
| Activate ⭐ | activate changes (slide-out) | everything goes live without leaving the page |
| Resolve | heal via `/admin` (or curl) | next poll: HTTP → **OK**, Summary → **OK**, workers back to 4 |

**Control UI:** open `http://<host>:8080/admin` — current state, time-in-state,
live extras (survivor RSS, TIME_WAIT creep), and per-state cards listing exactly
which Checkmk services change, with toggle buttons. Better on stage than
remembering curl commands. The curl API still works:

```bash
curl http://<host>:8080/admin/break    # back to the incident
curl http://<host>:8080/admin/heal     # recover
curl http://<host>:8080/admin/status   # JSON: state, durations, leak size
```

> Tip: Checkmk's normal check interval is 1 min. For a snappy demo, lower the
> check interval on these services, or hit *Reschedule* (the "play" icon) right
> after you heal so the state flips on stage instead of a minute later.

---

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `payment-api.corp.meridian-retail.com` | name baked into `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port |
| `HTTP_PORT` | `8080` | HTTP endpoint port |
| `START_BROKEN` | `1` | start in the incident state (ignored once a state file exists) |
| `BROKEN_DELAY_MS` | `1500` | extra latency while broken, wobbled ±30 % (sells the slow HTTP check) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the agent header |
| `STATE_FILE` | `/var/tmp/cmk-demo-payment-api-state.json` | persistence file (`""` disables) |

## 5. Realism notes (why it survives a close look)

Everything from `CLAUDE.md` ("Faking a Checkmk agent") is applied:

- **Counters integrate state-dependent rates** (`Counter` class) — strictly
  monotonic across toggles AND restarts (accumulators persisted), so no
  `IgnoreResults` staleness cascades. Rates wobble via three incommensurate
  long-period harmonics + an AR(1) walk — no clockwork sine, no Nyquist
  aliasing, no white noise.
- **Gauges wander, never sit still**: loadavg (per-timescale smoothing),
  MemFree/Available, Dirty, TCP states, HTTP response time.
- **Filesystems grow and get cleaned**: log/spool/export sawteeth on slow
  secular growth, plus the `df_inodes` block.
- **Physics a specialist will sum**: `/proc/stat` ticks ≈ 400/s on 4 CPUs,
  SSD-class virtio latencies (~0.4–0.7 ms, single-digit util), meminfo LRU
  arithmetic exact (anon LRU = AnonPages+Shmem etc.), CommitLimit =
  Swap + RAM/2, KernelStack ≈ threads × 16 KiB, both df volumes exist in
  `diskstat`, **every running systemd unit has its process in `ps`** (~32
  units incl. active/exited oneshots, ~28 processes), worker death frees the
  right amount of anon memory before the leak grows it back.
- **Real-dump section parity** (diffed against a real 2.5 Linux agent dump):
  full `check_mk` header, controller status, plugin provenance (`mk_apt`),
  both `lnx_if` variants, dynamic `timesyncd` timestamps, the exact APT
  sentinel, `mounts`, and the empty marker sections (`labels`, `nfsmounts_v2`,
  `cifsmounts`, `md`, `vbox_guest`, `local`). Ubuntu 24.04 meminfo key set
  matches exactly.
