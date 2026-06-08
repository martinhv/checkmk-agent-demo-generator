# checkmk-agent-demo-generator

Fake Checkmk hosts for live demos: stdlib-only Python servers that emit
realistic Check_MK agent output (plaintext TCP — accepted by Checkmk 2.5
without TLS/registration) plus a break/heal control plane, so you can stage
believable incidents on a laptop or a throwaway VM.

Born as demo plumbing for a Checkmk product keynote, now a standalone toolbox —
grown into a whole fictional **mid-sized company estate** ("Meridian Retail", an
online retailer with an in-house payments platform). See **`FLEET.md`** for the
company story, the host roster, and the port map.

## The demo hosts

Each is a self-contained directory (`serve.py` + `Dockerfile` +
`docker-compose.yml` + `README.md`). Most carry a toggleable incident
(`healthy → degraded → broken`, with auto-escalation and a `/admin` control
UI); a couple stay steady-green as believable estate background. The estate is
deliberately **mostly green** — a wall of green with one or two reds tells one
clean story.

| Dir | Host | Tier | State | Story |
|---|---|---|---|---|
| `web-frontend-01/` | nginx reverse proxy / TLS | edge | green | the estate's front door — steady-green background |
| `demo_broken_http_service/` | `payment-api` (gunicorn+nginx+redis) | app | incident | HTTP 503 symptom + failed `payment-worker.service` root cause |
| `app-worker-01/` | Java order/settlement worker | app | incident | **memory leak → swap thrash → OOM kill → service flap** (Memory CRIT + failed unit) |
| `app-redis-01/` | Redis 7 session + cache | app | incident | **maxmemory eviction storm** (bgsave fails, evictions storm, hit-ratio collapses) |
| `demo_dying_disk_db/` | `db-postgres-01` (PostgreSQL 16 primary) | data | incident | "Explain with AI": CPU-load page that is really a **fail-slow dying SSD** |
| `db-postgres-02/` | PostgreSQL 16 read replica | data | incident | **connection-pool exhaustion** toward `max_connections` |
| `mail-relay-01/` | Postfix transactional relay | infra | incident | **mail queue backlog** — downstream MX unreachable, deferred queue grows |
| `fileserver-01/` | Samba/NFS shared storage | infra | incident | **filesystem filling** — runaway spool, df magnitude + trend |
| `backup-01/` | restic backup host | infra | green | nightly backup OK — steady-green background |
| `win-dc-01/` | Windows Server 2022 AD DC | infra | incident | **C: drive fills up** after the cleanup service crashes (the one Windows host) |

Each dir's README has run instructions, the Checkmk setup, and the demo
choreography. `demo_dying_disk_db/install-native.sh` deploys that demo behind a
*real* TLS-registered agent on a Linux box (relay trick — see CLAUDE.md).

### Optional: run the whole estate as piggyback hosts

`piggyback-delivery/` is a single **delivery "shell" host** that carries the
*entire* estate as **piggyback**. One container runs the shell plus every
host's `serve.py` internally; Checkmk polls only the delivery shell (agent
`6559`) and you add the estate hosts as piggyback hosts — no per-host agent
port. The shell itself emits only a minimal agent section. It also gives you a
single combined control panel (`:8099/admin`) to drive every host's break/heal.
Reuses each host's `serve.py` unmodified. See `piggyback-delivery/README.md`.

## Port map

The new hosts use distinct ports (6560–6567 / 8090–8097) so the whole estate
can run at once. The two original demos both publish 6557 — run one at a time
or re-map. Full table in `FLEET.md`.

## Engineering knowledge

**`CLAUDE.md`** is the distilled knowledge base for building fake hosts that
survive scrutiny by Checkmk itself *and* by Linux/PostgreSQL/Windows
specialists: transport tricks, monotonic state-aware counters, restart
persistence, autocorrelated gauges, incident design rules, physics
cross-checks, and section-by-section parity with a real agent.

For calibration, diff a fake host against a real 2.5 Linux agent dump from your
own environment (e.g. `cmk-agent-ctl dump` / `check_mk_agent` on an Ubuntu 24.04
box). The Windows host is calibrated against the real 2.3 Windows agent dump
shipped in the Checkmk source tree (`tests/gui_e2e/data/windows-2.3.0p10`).

## Quick start

```bash
cd app-worker-01               # or any host directory
docker compose up --build -d
nc 127.0.0.1 6562 | head        # agent stream (port per host — see its README)
open http://localhost:8092/admin  # control UI (incident hosts)
```
