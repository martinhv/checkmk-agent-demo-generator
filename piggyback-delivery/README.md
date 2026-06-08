# Piggyback delivery — the whole estate behind one "shell" host

An **optional** alternative to adding every Meridian Retail host in Checkmk as
its own TCP host. Run *this* one host and the entire estate shows up as
**piggyback hosts** hanging off it. The delivery host itself carries only a
**minimal agent section** — it's just the shell that delivers everyone else's
data.

## How it works

Checkmk piggyback: any sections an agent wraps in `<<<<other-host>>>>` …
`<<<<>>>>` markers are attributed by the site to *other-host*, not to the
delivering host (the empty `<<<<>>>>` switches back). So `serve.py` here:

1. **spawns each estate host's own, unmodified `serve.py`** as a child process
   on an internal `127.0.0.1` port — reusing 100 % of the existing demos,
   including their break/heal toggles, auto-escalation and restart persistence;
2. on every agent poll, emits the delivery host's **minimal** `<<<check_mk>>>`
   (+ a controller-status section so its own *Check_MK Agent* service is OK and
   TLS-clean, + uptime), then fetches each child's full agent output and
   re-frames it as `<<<<hostname>>>>` … `<<<<>>>>` piggyback blocks.

```
<<<check_mk>>>                                       ← the delivery shell's own minimal data
Hostname: cmk-demo-gateway.corp.meridian-retail.com
...
<<<<web-frontend-01.corp.meridian-retail.com>>>>     ← everything below belongs to web-frontend-01
<<<check_mk>>>
... that host's full sections ...
<<<<>>>>                                             ← back to the delivery shell
<<<<app-worker-01.corp.meridian-retail.com>>>>
...
<<<<>>>>
```

Every host shows up in Checkmk as an **FQDN** (`<short>.corp.meridian-retail.com`,
set by `ESTATE_DOMAIN`). The short name (`web-frontend-01`, …) stays the internal
label used by the control panel, `ESTATE_HOSTS`, and the curl API below.

No host data is duplicated or re-implemented — the children are the single
source of truth.

## 1. Run it

```bash
cd piggyback-delivery
docker compose up --build -d      # one container runs the shell + all children
docker compose logs -f            # watch [pb] spawn lines
```

Published on `127.0.0.1` as **6559** (agent) and **8099** (combined control
panel). Carry a subset with `ESTATE_HOSTS` (comma list) in the compose file or
env; default is the whole estate.

No Docker? Stdlib-only — run it from the repo root checkout (it finds the host
dirs relative to itself):

```bash
AGENT_PORT=6559 HTTP_PORT=8099 python3 piggyback-delivery/serve.py
```

Sanity check (the delivery output should start with the shell's own section,
then `<<<<host>>>>` blocks):

```bash
nc 127.0.0.1 6559 | grep -E '^<<<<|^Hostname:'
```

## 2. Set it up in Checkmk

1. **Add the delivery shell as a normal TCP host.** Name
   `cmk-demo-gateway.corp.meridian-retail.com`, IP `127.0.0.1` (or the Docker
   host's IP), **Checkmk agent port → 6559**.
   Discover + activate — it gets a couple of plain services (Check_MK Agent,
   Uptime). Monitoring this host is what makes the site *fetch* the piggyback
   data for everyone else.
2. **Add each estate host as a piggyback host.** Use the **exact** FQDN names
   from the markers — `<short>.corp.meridian-retail.com` for each of
   `web-frontend-01`, `payment-api`, `app-worker-01`, `app-redis-01`,
   `db-postgres-01`, `db-postgres-02`, `mail-relay-01`, `fileserver-01`,
   `backup-01`, `win-dc-01` (e.g. `payment-api.corp.meridian-retail.com`). For
   each, set **"Checkmk agent /
   API integrations" → "Configured API integrations, no Checkmk agent"** so the
   site does *not* try to poll them over TCP — they receive their data purely
   via piggyback from the delivery host. (IP address can be left empty / "no
   IP".)
3. **Discover `db-postgres-01` while it is HEALTHY** — its SMART check baselines
   raw attribute values at discovery time (same caveat as the standalone
   `demo_dying_disk_db`). Discover the rest in any state.
4. Activate. The whole estate appears, mostly green.

> Tip: the standard *Dynamic host configuration* / *Piggyback* mechanics mean a
> piggyback host only has data while the delivery host is being polled and is
> emitting that host's block. If you carry a subset via `ESTATE_HOSTS`, only
> those piggyback hosts get data.

## 3. Combined control panel

`http://localhost:8099/admin` — one screen for the whole estate: each carried
host with its current state badge, `degrade` / `break` / `heal` buttons
(proxied to that child) and an **ⓘ info** link. Steady-green hosts show no
toggle buttons. Auto-refreshes every 5 s.

The **ⓘ info** link opens a per-host tab
(`/admin/<host>/info`) showing one card per state with exactly which Checkmk
services change when you switch to it — the same "what will happen" cards each
demo carries, rendered from the child's `/admin/meta` so the whole estate is
explained (and driven) from one place. Toggling from the info tab returns to it.

```bash
curl http://localhost:8099/admin/app-worker-01/break    # break one host
curl http://localhost:8099/admin/db-postgres-01/degrade
curl http://localhost:8099/admin/app-worker-01/info     # HTML: state-change cards
curl http://localhost:8099/                              # JSON: all hosts + states
```

Each child also still runs its own full control UI on an internal admin port
(`7700 + index`), not published by default.

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `ESTATE_DOMAIN` | `corp.meridian-retail.com` | DNS domain appended to every host → FQDN piggyback names |
| `DELIVERY_HOSTNAME` | `cmk-demo-gateway.${ESTATE_DOMAIN}` | name of the shell host in its `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port Checkmk polls (published 6559) |
| `HTTP_PORT` | `8080` | combined control panel (published 8099) |
| `ESTATE_HOSTS` | *(all)* | comma list of host names to carry (short names) |
| `CHILD_AGENT_BASE` | `7600` | internal child agent port base (`+ registry index`) |
| `CHILD_HTTP_BASE` | `7700` | internal child admin port base |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version in the delivery header |

Per-host incident timing (`AUTO_BREAK_AFTER_MIN`, `LEAK_FILL_MIN`,
`BREAK_RAMP_MIN`, …) is inherited by every child from the environment, so you
can set them once on the delivery container.
