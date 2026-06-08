# backup-01 — Meridian Retail demo host

Steady-green background host. Ubuntu 24.04 restic backup server that pulls
nightly backups of the DB and fileserver to an offsite repository. **No
incident, no break/heal toggle.** Purpose: fill out the Meridian Retail estate
with a realistic host and show healthy `Job restic-backup` / `Job restic-prune`
services in Checkmk.

## Services presented

All services are permanently OK.

| Checkmk service | What it shows |
|---|---|
| `Check_MK Agent` | TLS-registered, agent version 2.5.x |
| `CPU load` | Very low (0.2–0.3), well under default levels |
| `Memory` | ~6 GiB of 8 GiB used, no swap, Committed_AS green |
| `Filesystem /` | ~26 % used (40 GiB root), slow log creep + daily trim |
| `Filesystem /srv/backup` | ~70 % used (4 TiB volume), sawteeth from daily prune |
| `Disk IO sda` | Low read/write rates (system SSD) |
| `Disk IO sdb` | Moderate writes (backup storage SSD) |
| `Interface eth0` | Low traffic (backup transfers are nightly) |
| `SMART sda / sdb` | Both PASSED, temperature ~28–30 °C, raw attrs all zero |
| `Kernel Performance` | Context switches, page faults — all low |
| `NTP Time` | Synchronized, stratum 2, offset < 2 ms |
| `APT` | No updates pending |
| `Systemd Service Summary` | All ~30 units active; restic oneshots active/exited |
| `Job restic-backup` | exit code 0, ran ~6–8 h ago, real time ~48 min |
| `Job restic-prune` | exit code 0, ran ~7–8 h ago, real time ~8 min |

## Run it

```bash
docker compose up --build -d
docker compose logs -f
```

Status page: http://127.0.0.1:8096/admin

JSON status: http://127.0.0.1:8096/

## Checkmk setup

1. Add host `backup-01` with address `127.0.0.1`.
2. Under **Individual program call instead of agent** (or via the host's
   **Checkmk agent port** rule), set port to **6566**.
3. Discover services. All services should be OK immediately.

There is no state toggle and no incident. Discovering services at any time is
safe.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CMK_HOSTNAME` | `backup-01` | Hostname reported to Checkmk |
| `AGENT_PORT` | `6566` | TCP port the agent listens on (container-internal) |
| `HTTP_PORT` | `8080` | HTTP status port (container-internal; published as 8096) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | Agent version string |
| `STATE_FILE` | `/var/tmp/cmk-demo-backup-state.json` | Persistence file for counters and uptime (set empty to disable) |
