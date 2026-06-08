# The demo estate — "Meridian Retail"

A fictional **mid-sized online retailer with an in-house payments platform**,
monitored by a single Checkmk site (`monitoring/prod`). Every host here is a
fake Checkmk agent (stdlib-only Python, plaintext TCP — see `CLAUDE.md`) so the
whole "company" can be staged on one laptop or VM. The estate is deliberately
**mostly green**: real infrastructure is mostly fine, and a wall of green with
one or two reds tells a single clean story (the storyline rule from
`CLAUDE.md`).

The two original hosts (`payment-api`, `db-postgres-01`) anchor the platform;
the rest fill in the tiers a real shop would run so the picture reads as a
genuine company, not a pile of unrelated test boxes.

## Roster

| Host | Tier | OS / role | Demo state | Incident story |
|---|---|---|---|---|
| `web-frontend-01` | edge | Ubuntu 24.04 · nginx reverse proxy / TLS termination | **steady green** | background — the estate's front door, always healthy |
| `payment-api` | app | Ubuntu 24.04 · gunicorn + nginx + redis client | incident | HTTP 503 symptom + failed `payment-worker.service` root cause *(existing: `demo_broken_http_service/`)* |
| `app-worker-01` | app | Ubuntu 24.04 · Java settlement/order worker | incident | **memory leak → swap thrash → OOM kill → service flap** (a *real* resource exhaustion, the mirror image of the dying-disk fake load) |
| `app-redis-01` | app | Ubuntu 24.04 · Redis 7 session + cache store | incident | **maxmemory eviction storm**: a bad TTL deploy floods memory, evictions spike, hit-ratio collapses, clients block |
| `db-postgres-01` | data | Ubuntu 24.04 · PostgreSQL 16 primary | incident | "Explain with AI": CPU-load page that is really a **fail-slow dying SSD** *(existing: `demo_dying_disk_db/`)* |
| `db-postgres-02` | data | Ubuntu 24.04 · PostgreSQL 16 read replica | incident | **connection-pool exhaustion**: a runaway BI/reporting job opens connections until `postgres_connections` approaches `max_connections` (the one postgres check that alerts by default: 80 %/90 %) |
| `mail-relay-01` | infra | Ubuntu 24.04 · Postfix transactional mail relay | incident | **mail queue backlog**: a downstream MX goes unreachable, the deferred queue piles up |
| `fileserver-01` | infra | Ubuntu 24.04 · Samba/NFS shared storage | incident | **filesystem filling**: a runaway log/upload spool grows until df magnitude + trend cross the levels (predictive) |
| `backup-01` | infra | Ubuntu 24.04 · restic backup host | **steady green** | background — last nightly backup job OK (`mk_job`) |
| `win-dc-01` | infra | Windows Server 2022 · Active Directory DC | incident | **C: drive filling + a stopped critical service** — the one Windows host, different agent format |

## Port map (publish on `127.0.0.1`)

Each host listens on a distinct pair so the whole estate can run at once.
(`6556` is usually the demo laptop's *own* agent — left free.)

| Host | agent TCP | admin/HTTP |
|---|---|---|
| `payment-api` *(existing)* | 6557 | 8080 |
| `db-postgres-01` *(existing)* | 6557¹ | 8081 |
| `web-frontend-01` | 6560 | 8090 |
| `app-redis-01` | 6561 | 8091 |
| `app-worker-01` | 6562 | 8092 |
| `fileserver-01` | 6563 | 8093 |
| `mail-relay-01` | 6564 | 8094 |
| `db-postgres-02` | 6565 | 8095 |
| `backup-01` | 6566 | 8096 |
| `win-dc-01` | 6567 | 8097 |

¹ the two original demos both publish 6557 — run one at a time, or re-map. The
new hosts use 6560–6567 so they never collide with each other or the originals.

## Two ways to run the estate

1. **Per-host TCP** (the default): bring up each host's own
   `docker compose`, add each in Checkmk as a TCP host with its agent-port
   override. Independent, copy-pasteable.
2. **One piggyback delivery host** (`piggyback-delivery/`): a single "shell"
   host carries the *whole* estate as piggyback. One container runs the shell
   plus every host's `serve.py` internally; Checkmk polls only the delivery
   shell (agent **6559**, control panel **8099**) and the estate hosts are added
   as **piggyback** hosts — no per-host agent port. The shell emits only a
   minimal agent section. See `piggyback-delivery/README.md`.

| Host | agent TCP | admin/HTTP |
|---|---|---|
| `cmk-demo-gateway.corp.meridian-retail.com` (piggyback delivery shell) | 6559 | 8099 |

## Conventions every host follows

- **FQDN host names.** Every host is monitored in Checkmk under its fully
  qualified name `<short>.corp.meridian-retail.com` (the AD/DNS domain implied by
  `win-dc-01`). The short label (`web-frontend-01`, …) used in the tables above is
  just the internal handle for ports, the control panel, and `ESTATE_HOSTS`
  selection. Override the domain with `ESTATE_DOMAIN` (piggyback) or `CMK_HOSTNAME`
  (per host). In piggyback mode the FQDN is the piggyback marker, so add each
  estate host in Checkmk under its FQDN.
- **Plaintext TCP agent** + the section-by-section parity rules in `CLAUDE.md`
  (full `<<<check_mk>>>` header, controller-status pretending TLS registration,
  deployed-plugin list, both `lnx_if` variants, `df_v2`, full `/proc/meminfo`,
  `systemd_units` with ~30 units, `timesyncd` with dynamic timestamps, `apt`
  sentinel, `mounts`).
- **Monotonic state-aware counters** (`Counter`), **autocorrelated gauges**
  (`gauge`/`_Wobble`), and **restart-persisted state** (`STATE_FILE`) — copied
  from `demo_dying_disk_db/serve.py`, the reference implementation.
- **Incident hosts** carry a `/admin` control UI (state badge, time-in-state,
  per-state effect cards, toggle buttons, 5 s auto-refresh) + a curl API, plus
  a `degraded`/`broken` escalation where the timeline matters.
- **Background hosts** are steady green: same realistic sections, no toggle.
- **Low noise, one root cause** per incident: only the symptom + root-cause
  services change; everything else stays green and *corroborates*.

See each host's own `README.md` for run instructions, the Checkmk setup, and
the demo choreography.
