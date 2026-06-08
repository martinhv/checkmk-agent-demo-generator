# Demo: `fileserver-01` — filesystem filling → df trend → magnitude CRIT

The shared-storage host of the Meridian Retail estate (see `../FLEET.md`): an
Ubuntu 24.04 box running **Samba** (`smbd`/`nmbd` for the Windows clients) and
**NFS** (`nfs-server`) — home directories, shared drives, and an upload/ingest
spool. The incident is a classic operational footgun: a bad config push
disabled log rotation on the ingest pipeline, so a **runaway batch importer**
keeps appending to one log file on the data volume `/srv/shares`. The file
grows without bound, the volume fills, and the **Filesystem** service crosses
its levels. *"Explain with AI"* fuses the steep fill **slope** + the projected
**time-to-full** + the single offending **growing file** (fileinfo) into the
root cause: a log/spool that stopped rotating — rotate/clear it, don't just
delete random files or grow the volume.

- **TCP 6563** — Check_MK agent output (plaintext; the fetcher sees `<<` →
  `TransportProtocol.PLAIN`, no TLS/registration). The controller-status
  section pretends the host is TLS-registered (no "TLS not activated" warning).
- **TCP 8093** — `/admin` control UI + curl toggle API. Nothing here is
  monitored.

## The incident it fakes

| Service | Healthy | Degraded | Broken | Role |
|---|---|---|---|---|
| **Filesystem `/srv/shares`** | ~55 % used, growth flat (cleanup keeps up) | usage climbs ~55 % → ~78 %; the **growth trend turns steep** → trend WARN / projected fill *(needs a trend rule — see setup)*; magnitude still green/edging | **CRIT** — > 90 % used (default magnitude levels **80/90**) and **still growing live** | the headline |
| **File `…/import-batch.log`** (fileinfo) | small, rotates normally (~6 MB) | grows fast — the single file to blame | large | the named root cause |
| **Disk IO `sdb`** (the data array) | calm | write IOPS climb (the append) | elevated | corroboration |
| **Filesystem `/`, `/home`** | OK | OK | OK | proves only the data volume fills |
| everything else (memory, CPU, network, time sync, APT, SMART, Samba/NFS units, jobs) | OK | OK | OK | low-noise: one root cause |

The data volume usage is a **continuous live-growing function of wall-clock +
persisted START** (like the dying-disk host's df/meminfo curves), so it climbs
smoothly across re-polls and survives restarts. Root `/` and `/home` keep their
own slow-growth-plus-cleanup sawteeth and stay well under the levels.

**Three states, because the timeline matters:**

- `healthy` → all green. `/srv/shares` ~55 % used, normal growth + a 30-min
  retention-prune sawtooth, Samba/NFS serving happily.
- `degraded` → the runaway starts. Usage climbs steadily from ~55 % toward
  ~78 % over `FILL_RAMP_MIN` (~16 min). The **growth trend becomes steep** —
  with a Filesystem trend rule this is a **trend WARN** with a projected
  time-to-full — while the magnitude levels are still green/edging. The
  `import-batch.log` file grows fast in fileinfo. **Trigger ~18 min before
  showtime.**
- `broken` → usage crosses **90 % → Filesystem CRIT** and keeps **growing live**
  across re-polls. `degraded` **auto-escalates to `broken` after
  `AUTO_BREAK_AFTER_MIN`** (default 18 min); the final push past 90 % ramps over
  `BREAK_RAMP_MIN` (~4 min), no vertical cliff.

---

## 1. Run it

```bash
cd fileserver-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6563** (agent) and **8093** (admin). Stdlib-only,
so without Docker:

```bash
AGENT_PORT=6563 HTTP_PORT=8093 START_STATE=healthy python3 serve.py
```

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `fileserver-01`, IP `127.0.0.1`, **Checkmk
   agent port → 6563**.
2. Service discovery (any state — no discovery-time baselines here). Activate.
   Everything green.

**Magnitude (80/90 %) is default** — the `broken`-state CRIT on
`Filesystem /srv/shares` needs **no rule**. The fileinfo `File …` services are
discovery-based and don't alert by default.

**The trend WARN needs a rule.** The filesystem `trend_range` default is 24 h
and `trend_perfdata` is on, so Checkmk always *graphs* the growth and the
projected time-to-full — but the trend levels (`trend_perc`, `trend_timeleft`,
`trend_bytes`) are **unset by default**, so the trend never WARNs on its own.
To make the `degraded` breadcrumb fire a real WARN:

- *Setup → Services → Filesystems → "Filesystem (used space and growth)"*, scope
  it to `fileserver-01` / item `/srv/shares`, and set either
  **"Levels for the percentual growth"** (`trend_perc`, e.g. WARN at +5 %/24 h)
  or **"Levels on time left until filesystem full"** (`trend_timeleft`, e.g.
  WARN below 48 h). With the demo's steep fill the projected time-to-full drops
  to a few hours, so either trips immediately in `degraded`.

(Source: `cmk/plugins/lib/df.py` `FILESYSTEM_DEFAULT_PARAMS` →
`"levels": (80.0, 90.0)` and `TREND_DEFAULT_PARAMS` →
`{"trend_range": 24, "trend_perfdata": True}`; `cmk/plugins/lib/size_trend.py`
applies `trend_perc`/`trend_timeleft`/`trend_bytes` only when present.)

## 3. Demo choreography

| When | Action | What Checkmk shows |
|---|---|---|
| T−18 min | `curl localhost:8093/admin/degrade` | nothing red yet. `Filesystem /srv/shares` usage starts climbing; the growth graph turns steep; with the trend rule the service goes **WARN** with a shrinking "time left until full". `import-batch.log` grows in fileinfo |
| ~T | *nothing* — degraded **auto-escalates after 18 min** (or `/admin/break`) | usage ramps past 90 % → **Filesystem /srv/shares CRIT**, and keeps growing on every poll |
| the page | open the Filesystem CRIT | "/srv/shares 9x % full". Instinct: delete files / grow the volume |
| ⭐ Explain with AI | one click | fuses the steep fill slope + projected time-to-full + the one fast-growing file (`import-batch.log`) + flat root// home + calm CPU/memory → *"the ingest log stopped rotating and is filling /srv/shares at N GB/h, full in ~M h — rotate/clear it; growing the volume only delays it"* |
| resolve | `curl localhost:8093/admin/heal` ("rotation re-enabled, log cleared") | next poll: usage drops back to ~55 %, trend flattens, all green |

**Control UI:** `http://localhost:8093/admin` — state badge, time-in-state,
live `/srv/shares` used %, fill rate (GB/h), projected time-to-full, per-state
effect cards with toggle buttons, 5 s auto-refresh.

```bash
curl http://localhost:8093/admin/degrade   # runaway starts (usage climbs, trend WARN)
curl http://localhost:8093/admin/break      # crosses 90 % (Filesystem CRIT)
curl http://localhost:8093/admin/heal        # rotation fixed, all green
curl http://localhost:8093/                  # JSON: state, srv_shares_used_pct, fill_rate_gb_per_h, hours_until_full, ...
```

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `fileserver-01` | name in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published 6563) |
| `HTTP_PORT` | `8080` | admin port (published 8093) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AUTO_BREAK_AFTER_MIN` | `18` | minutes in `degraded` before usage crosses 90 % (auto-CRIT); `0` = never |
| `FILL_RAMP_MIN` | `16` | minutes for the runaway to drive `/srv/shares` ~55 % → ~97 % |
| `BREAK_RAMP_MIN` | `4` | minutes for the broken push past 90 % to reach full force |
| `STATE_FILE` | `/var/tmp/cmk-demo-fileserver-state.json` | persists counters/uptime/incident across restarts (`""` = off) |
