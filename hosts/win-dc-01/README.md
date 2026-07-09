# Demo: `win-dc-01` — Windows AD domain controller, C: drive fills up

The one **Windows** host in the Meridian Retail estate (see `../FLEET.md`): a
Windows Server 2022 Active Directory domain controller. It uses the same
plaintext-TCP trick as the Linux demos, but the agent payload is the **Windows
agent format** — verified against a real 2.3 Windows agent dump
(`check_mk/tests/gui_e2e/data/windows-2.3.0p10`): `<<<df:sep(9)>>>` (tab-sep,
NTFS), `<<<wmi_cpuload:sep(124)>>>`, the Windows `<<<mem>>>` key set
(`MemTotal`/`PageTotal`/`VirtualTotal`…), `<<<services>>>`, `<<<ps:sep(9)>>>`,
`<<<systemtime>>>`, `<<<fileinfo:sep(124)>>>`, plus the controller-status
section pretending TLS registration.

- **TCP 6556** — Windows-format Check_MK agent output (plaintext).
- **TCP 8080** — `/admin` control UI + curl toggle API.

## The incident it fakes

A single clean root cause: the in-house **Meridian Backup Agent** service —
which nightly trims `C:\` (old NTDS logs, the Windows Update download cache) —
**crashes**. With nothing clearing it, the **system drive C: fills up**.

| Service | Healthy | Degraded | Broken | Role |
|---|---|---|---|---|
| **Filesystem C:/** | ~53 % used | climbs → ~84 % → **WARN** | **> 90 % → CRIT**, still growing live | the headline (default magnitude levels 80/90) |
| **Service MeridianBackupAgent** | running | **stopped** | stopped | the root cause (see note on the Windows Services rule below) |
| **File `…\SoftwareDistribution\Download\cache.cab`** | static | growing live | large | corroboration the AI can quote (fileinfo) |
| CPU load (WMI queue), Memory + page file, D:/ data, AD services (NTDS/DNS/Netlogon/DFSR/W32Time), Uptime, System Time | OK | OK | OK | low noise — one root cause |

> **Note on the stopped service:** individual Windows services alert only when
> you add a **"Windows Services" monitoring rule** for them (there is no default
> — `cmk/plugins/windows/agent_based/services.py`). The **C: fill is the
> guaranteed default red**; add the rule for `MeridianBackupAgent` to also get
> a service-level CRIT (recommended for the demo — see step 3).

**Three states, because the timeline matters:**

- `healthy` → all green. C: ~53 %, every service running.
- `degraded` → MeridianBackupAgent **stopped** (root cause appears first); C:
  climbs from 53 % toward ~84 % over `LEAK_FILL_MIN` (~18 min) → **Filesystem
  C:/ WARN**; the cache file grows live. **Trigger ~20 min before showtime.**
- `broken` → C: crosses **90 % → CRIT** and keeps growing. `degraded`
  **auto-escalates after `AUTO_BREAK_AFTER_MIN`** (default 20 min); the final
  climb ramps over `BREAK_RAMP_MIN` (~4 min), no vertical cliff.

---

## 1. Run it

```bash
cd win-dc-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6567** (agent) and **8097** (admin). No Windows
host is required — the container just writes the Windows section text.
Stdlib-only, so without Docker:

```bash
AGENT_PORT=6567 HTTP_PORT=8097 START_STATE=healthy python3 serve.py
```

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `win-dc-01.corp.meridian-retail.com`, IP `127.0.0.1`, **Checkmk
   agent port → 6567**. Checkmk detects it as a Windows host from the agent
   sections.
2. Service discovery (any state — no discovery-time baselines). Activate.
3. **Recommended:** add a *"Windows Services"* monitoring rule matching
   service `MeridianBackupAgent` (expected state: running) so the stopped
   service produces its own CRIT alongside the C: fill. Without it, only the
   Filesystem C:/ alert fires (which is enough for the headline).

## 3. Demo choreography

| When | Action | What Checkmk shows |
|---|---|---|
| T−20 min | `curl localhost:8097/admin/degrade` | `MeridianBackupAgent` → stopped; Filesystem C:/ starts climbing (WARN as it nears 84 %); the cache file grows |
| ~T | *nothing* — auto-escalates after 20 min (or `/admin/break`) | Filesystem C:/ ramps past 90 % → **CRIT**, still growing live |
| the page | open the Filesystem C:/ CRIT | "C: nearly full" — instinct: just delete some files |
| ⭐ Explain with AI | one click | fuses the stopped MeridianBackupAgent + the steep C: slope + the growing SoftwareDistribution cache file → *"the cleanup service died; C: is filling at N GB/h and full in ~M h — restart the agent and clear the WU cache"* |
| resolve | `curl localhost:8097/admin/heal` ("service restarted, cache cleared") | next poll: C: drops back, service running, all green |

**Control UI:** `http://localhost:8097/admin` — state badge, C:-used %, how long
the agent has been stopped, the live cache-file size, per-state effect cards,
toggle buttons, 5 s auto-refresh.

```bash
curl http://localhost:8097/admin/degrade   # backup service crashes, C: fills (WARN)
curl http://localhost:8097/admin/break       # C: > 90% (CRIT)
curl http://localhost:8097/admin/heal         # restarted + cache cleared, all green
curl http://localhost:8097/                   # JSON: state, c_drive_used_pct, backup_agent, ...
```

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `win-dc-01.corp.meridian-retail.com` | name in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published 6567) |
| `HTTP_PORT` | `8080` | admin port (published 8097) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AUTO_BREAK_AFTER_MIN` | `20` | minutes in `degraded` before C: auto-crosses CRIT (`0` = never) |
| `LEAK_FILL_MIN` | `18` | minutes for C: to climb to the WARN band while degraded |
| `BREAK_RAMP_MIN` | `4` | minutes for the final climb past 90 % |
| `STATE_FILE` | `/var/tmp/cmk-demo-win-dc-state.json` | persists state/uptime across restarts (`""` = off) |
