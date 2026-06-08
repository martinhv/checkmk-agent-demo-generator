# Demo: `web-frontend-01` — nginx reverse proxy / TLS termination

The front door of the Meridian Retail estate (see `../FLEET.md`): an Ubuntu
24.04 box running nginx as a TLS-terminating reverse proxy for the payment
platform. **This is a steady-green background host.** It has no incident and
no break/heal toggle — it exists so the monitoring estate looks like a real
company, not a pile of test boxes.

The box is intentionally the busiest-looking host on the network side (~8 MB/s
rx / 6 MB/s tx, ~120 ESTABLISHED TCP connections), but all services remain
green at all times. All counters and gauges gently wobble to match the texture
of a live host.

- **TCP 6560** (published) / **6556** (container) — Check_MK agent output
  (plaintext; the Checkmk 2.5 fetcher sees `<<` → `TransportProtocol.PLAIN`,
  no TLS/registration required). The controller-status section pretends the
  host is TLS-registered, so no "TLS not activated" warning appears.
- **TCP 8090** (published) / **8080** (container) — `/admin` status page
  (state badge, uptime, service list, auto-refresh every 10 s) + `/` JSON
  status endpoint. No toggle buttons — there is nothing to toggle.

## Services presented to Checkmk

All services are green in all circumstances.

| Service | Value | Notes |
|---|---|---|
| **CPU load** | ~0.6 (4 cores) | well under per-core WARN of 5.0 |
| **Memory** | ~5.5 GiB used of 16 GiB | large page cache (TLS buffers); ~640 MiB Shmem (nginx shared zones) |
| **Disk** (sda, Samsung 870 EVO) | healthy SMART, calm I/O | latency ≈ 0.7 ms, utilization < 5 % |
| **Interface eth0** | ~8 MB/s rx / 6 MB/s tx | busiest host in the estate; well under 1 G |
| **TCP connections** | ~120 ESTABLISHED, ~80 TIME_WAIT | client churn at the edge; all counts green |
| **Filesystem /** | ~30 % used of 40 GiB | slow growth; daily log trim sawtooth |
| **Filesystem /var/log** | 20–35 % of 10 GiB | 24 h logrotate sawtooth |
| **nginx.service** | active/running | nginx master + 4 workers in ps |
| **Systemd Service Summary** | OK (~30 units) | certbot.service included |
| **Time sync** | stratum 2, offset ≈ 0 ms | dynamic timestamps — never stale |
| **APT** | no pending updates | exact sentinel text |
| **Job: certbot-renew** | exit 0, last run 6 h ago | nightly TLS cert renewal |
| **SMART (sda)** | PASSED, temp ~27 °C | well below 35/40 °C WARN/CRIT |

## 1. Run it

```bash
cd web-frontend-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6560** (agent) and **8090** (admin).
Stdlib-only Python — no dependencies. Without Docker:

```bash
AGENT_PORT=6560 HTTP_PORT=8090 python3 serve.py
```

Admin UI: `http://localhost:8090/admin`
JSON status: `http://localhost:8090/`

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `web-frontend-01`, IP `127.0.0.1`,
   **Checkmk agent port → 6560**.
2. Service discovery (any time — no discovery-time baselines here).
3. Activate changes. Every service will be green.

No extra monitoring rules are needed: all relevant checks use their default
thresholds and this host is well inside them.

## 3. Config (env vars)

| Var | Default (Docker) | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `web-frontend-01` | hostname in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port inside the container (published as **6560**) |
| `HTTP_PORT` | `8080` | admin HTTP port inside the container (published as **8090**) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version string in the agent header |
| `STATE_FILE` | `/var/tmp/cmk-demo-web-frontend-state.json` | counter/uptime persistence across restarts (`""` = disabled) |

## Notes

This host has no incident, no `START_STATE`, no break/heal toggle, and no
auto-escalation watchdog. It is there so that when an incident fires on
`db-postgres-01` or `app-worker-01`, the viewer sees a wall of green in the
rest of the estate — exactly one or two reds, one root cause.
