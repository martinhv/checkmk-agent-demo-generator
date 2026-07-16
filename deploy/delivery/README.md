# Delivery shell — the whole estate behind one "shell" host

An **optional** alternative to adding every Meridian Retail host in Checkmk as
its own TCP host. Run *this* one shell and it delivers the entire estate's agent
data to Checkmk. The shell itself carries only a **minimal agent section** —
it's just the carrier for everyone else's data.

The shell delivers in one of two modes (`DELIVERY_MODE`):

- **datasource** (the default `estate.py` uses on a self-hosted site) — each
  host's agent output is written to a file that Checkmk reads per host via a
  `cat $HOSTNAME$` datasource program. Scales best, needs filesystem access.
- **piggyback** (for Checkmk Cloud/SaaS, no site filesystem) — the estate shows
  up as **piggyback hosts** hanging off the shell. This README walks through the
  piggyback path in detail; the datasource wiring is covered in
  `../../CLAUDE.md` and handled automatically by `../cmk_setup.py`.

## Quick start

On a Checkmk dev box, from zero to the fully monitored estate:

```bash
cmk-dev-install-site              # install today's build + create the v* site
../../estate.py up --site         # or by hand:
cd deploy/delivery
docker compose up --build -d      # start the estate container
../cmk_setup.py --site            # set up the site (newest running v* dev site)
```

Then open the control panel on <http://localhost:8099/admin> to break/heal
hosts. For any other site pass `--site-url`/`--user`/`--secret` instead of
`--site`; details and the manual setup in the sections below.

## How it works

Either way, the shell first **spawns each estate host's own, unmodified
`serve.py`** as a child process on an internal `127.0.0.1` port — reusing 100 %
of the existing demos, including their break/heal toggles, auto-escalation and
restart persistence. What differs is only how each child's agent output then
reaches Checkmk.

### Datasource mode (the default)

`DELIVERY_MODE=datasource` — the shell writes each child's full agent output to
its own file under `AGENT_OUTPUT_DIR` (default `/var/tmp/cmk-demo-agent-output`),
named by the host's FQDN, atomically (tmp + rename) and **world-readable
(0644)**. Checkmk reads each host with a single *"Individual program call
instead of agent access"* rule — `cat <dir>/$HOSTNAME$` — so the site user's
`cat` reads the file directly, no piggyback, no site-filesystem write, no sudo.
The Docker runtime bind-mounts the host dir into the container as
`/agent-output`. The shell writes every file once **before** it opens its panel
(so discovery has something to read) and a thread refreshes them every ~20 s.
In this mode the shell emits only its own minimal section over TCP.

This is what `estate.py`/`cmk_setup.py` use on a self-hosted site: it scales
best (no single-shell fetch bottleneck, no piggyback dependency).

### Piggyback mode (cloud / SaaS)

`DELIVERY_MODE=piggyback` — for a site with no filesystem access to write files
for. Checkmk piggyback: any sections an agent wraps in `<<<<other-host>>>>` …
`<<<<>>>>` markers are attributed by the site to *other-host*, not to the
delivering host (the empty `<<<<>>>>` switches back). So on every agent poll the
shell emits its own **minimal** `<<<check_mk>>>` (+ a controller-status section
so its own *Check_MK Agent* service is OK and TLS-clean, + uptime), then fetches
each child's full agent output and re-frames it as `<<<<hostname>>>>` …
`<<<<>>>>` piggyback blocks:

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

Either way, every host shows up in Checkmk as an **FQDN**
(`<short>.corp.meridian-retail.com`, set by `ESTATE_DOMAIN`). The short name
(`web-frontend-01`, …) stays the internal label used by the control panel,
`ESTATE_HOSTS`, and the curl API below. No host data is duplicated or
re-implemented — the children are the single source of truth.

## 1. Run it

```bash
cd deploy/delivery
docker compose up --build -d      # one container runs the shell + all children
docker compose logs -f            # watch [pb] spawn lines
```

Published on `127.0.0.1` as **6559** (agent) and **8099** (combined control
panel). Carry a subset with `ESTATE_HOSTS` (comma list) in the compose file or
env; default is the whole estate.

A bare `docker compose up` runs in **piggyback** mode (the compose default). For
datasource mode, set `DELIVERY_MODE=datasource` — `estate.py up` does this
automatically on a self-hosted site and bind-mounts the output dir; by hand,
`DELIVERY_MODE=datasource docker compose up --build -d`.

No Docker? Stdlib-only — run it from the repo root checkout (it finds the host
dirs relative to itself):

```bash
AGENT_PORT=6559 HTTP_PORT=8099 python3 deploy/delivery/serve.py                 # piggyback
DELIVERY_MODE=datasource AGENT_OUTPUT_DIR=/var/tmp/cmk-demo-agent-output \
  AGENT_PORT=6559 HTTP_PORT=8099 python3 deploy/delivery/serve.py               # datasource
```

Sanity check:

```bash
# piggyback: the delivery output starts with the shell's own section, then <<<<host>>>> blocks
nc 127.0.0.1 6559 | grep -E '^<<<<|^Hostname:'
# datasource: one full agent file per host appears in the output dir
ls /var/tmp/cmk-demo-agent-output/ && head -1 /var/tmp/cmk-demo-agent-output/*payment-api*
```

## 2. Set it up in Checkmk — one command

`../cmk_setup.py` does the whole site side through the REST API
(stdlib-only, idempotent — safe to re-run, e.g. after changing
`ESTATE_HOSTS`); the full from-scratch flow is the quick start at the top. It
defaults to `--mode self-hosted` (**datasource** delivery); pass `--mode cloud`
for the **piggyback** path. Match the container's `DELIVERY_MODE` to it.

`--site` knows the cmk-dev-site conventions — URL `http://localhost/<site>`,
credentials `cmkadmin`/`cmk` — so no other options are needed; without a NAME
it picks the newest running local `v*` site, `--site NAME` targets a specific
one. For any other site, pass the URL and credentials explicitly:

```bash
../cmk_setup.py --site-url http://localhost/mysite --user automation
# secret via --secret, $CMK_AUTOMATION_SECRET, or interactive prompt
```

It needs a site user with Setup write access (e.g. the site's `automation`
user) and does, in order:

1. asks the running container (control panel `:8099`) which hosts it
   *actually* carries — an `ESTATE_HOSTS` subset just works;
2. **heals** every non-healthy host first, because services must be
   discovered in the healthy state (`db-postgres-01`'s SMART check baselines
   raw attribute values at discovery — same caveat as the standalone demo);
3. creates a **Meridian Retail demo** folder (`--folder`, `/` = root), the
   delivery shell, and every carried host — **with `parents` set from the
   estate topology** (servers → the SNMP campus core `sw-core-01`, declared in
   the registry here and re-applied on re-runs). The host tags depend on the
   mode:
   - **datasource** (default): one *"Individual program call"* rule
     (`datasource_programs`: `cat <dir>/$HOSTNAME$`) on the root folder, and
     every estate host tagged `cmk-agent` + `no-ip` + `no-piggyback` so it runs
     that program and needs no TCP/IP/ping. No agent-port rule.
   - **cloud** (piggyback): the shell is a TCP host with an `agent_ports` rule
     (6559), and every estate host is a pure piggyback host ("no agent" +
     "always use and expect piggyback data" + "no IP");
4. creates the **"Payments platform" BI pack** — tier rules (network path →
   customer entry → payment API → processing/cache → data layer → storage)
   under one worst-of aggregation — plus a `special_agents:bi` rule on the
   shell, so a **BI Aggregation service pages** when any tier goes red (the
   shell is created as "all-agents" so the BI special agent runs *in addition
   to* its own Checkmk-agent source — the `cat` program in datasource mode, or
   the TCP fetch in piggyback mode);
5. runs service discovery — the **shell first** (in piggyback mode that refresh
   fetch is also what stores everyone else's piggyback payloads on the site);
6. activates, then re-discovers the shell once the estate is live — the BI
   special agent only reports the aggregation after the first activation.

Site and container on different machines? Run the script wherever it can
reach both, and pass `--agent-ip <container host IP as the site sees it>`
plus `--panel http://<container host>:8099`.

`--remove` (with the same `--site`/`--site-url`/`--folder`) tears everything
down again — hosts, rule, folder — and leaves anything else on the site
untouched.

### Manual setup (what the script does, if you'd rather click)

The estate host names are the **exact** FQDNs from the shell —
`<short>.corp.meridian-retail.com` for each of `web-frontend-01`, `payment-api`,
`app-worker-01`, `app-redis-01`, `db-postgres-01`, `db-postgres-02`,
`mail-relay-01`, `fileserver-01`, `backup-01`, `win-dc-01` (e.g.
`payment-api.corp.meridian-retail.com`).

**Datasource mode** (the shell running `DELIVERY_MODE=datasource`):

1. **Add one *"Individual program call instead of agent access"* rule**
   (ruleset *Datasource programs*) with command `cat
   /var/tmp/cmk-demo-agent-output/$HOSTNAME$` on the estate folder — one rule
   covers every host below it.
2. **Add each estate host** (the shell included) with **"Checkmk agent / API
   integrations" → "Checkmk agent"**, **"Piggyback" → "No piggyback"** and
   **"IP address family" → "No IP"**: the program runs per host, no TCP/IP/ping.
   The file the shell wrote for that FQDN must be world-readable to the site
   user (it is — 0644).

**Piggyback mode** (`DELIVERY_MODE=piggyback`, e.g. cloud):

1. **Add the delivery shell as a normal TCP host.** Name
   `cmk-demo-gateway.corp.meridian-retail.com`, IP `127.0.0.1` (or the Docker
   host's IP), **Checkmk agent port → 6559**.
   Discover + activate — it gets a couple of plain services (Check_MK Agent,
   Uptime). Monitoring this host is what makes the site *fetch* the piggyback
   data for everyone else.
2. **Add each estate host as a piggyback host.** For each, set **"Checkmk agent
   / API integrations" → "No API integrations, no Checkmk agent"**, **"Piggyback"
   → "Always use and expect piggyback data"** and **"IP address family" → "No
   IP"** — the site must not poll them over TCP; their data arrives purely via
   piggyback from the delivery host.

Then, in **either** mode:

3. **Discover `db-postgres-01` while it is HEALTHY** — its SMART check baselines
   raw attribute values at discovery time (same caveat as the standalone
   `hosts/db-postgres-01`). Discover the rest in any state.
4. Activate. The whole estate appears, mostly green.

> Tip: in piggyback mode a host only has data while the delivery shell is being
> polled and is emitting that host's block; in datasource mode the file must
> exist (the shell writes it). If you carry a subset via `ESTATE_HOSTS`, only
> those hosts get data.

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

At the top of the panel is the **cross-host cascade** — one button that fires a
dependency-ordered chain of incidents telling a single root-cause story (the
dying disk on `db-postgres-01` propagating up the payments stack: replica lag →
settlement worker → payment API → worker OOM, over ~7 min, with the frontend and
cache staying green). The card shows a live timeline (fired / pending / ETA) and
**trigger** / **stop & heal all** controls. `CASCADE_TIME_SCALE` compresses the
timeline for short slots (e.g. `0.1` → ~45 s).

```bash
curl http://localhost:8099/admin/app-worker-01/break    # break one host
curl http://localhost:8099/admin/db-postgres-01/degrade
curl http://localhost:8099/admin/app-worker-01/info     # HTML: state-change cards
curl http://localhost:8099/admin/cascade/start          # arm the cross-host cascade
curl http://localhost:8099/admin/cascade/heal           # stop it and heal every host
curl http://localhost:8099/admin/cascade/status         # JSON: timeline + what has fired
curl http://localhost:8099/                              # JSON: all hosts + states + cascade
```

Each child also still runs its own full control UI on an internal admin port
(`7700 + index`), not published by default.

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `DELIVERY_MODE` | `piggyback` | `datasource` (write per-host files) or `piggyback` (embed as blocks). `estate.py` sets `datasource` for self-hosted |
| `AGENT_OUTPUT_DIR` | `/var/tmp/cmk-demo-agent-output` | datasource mode: where the per-host agent files are written (world-readable) |
| `ESTATE_DOMAIN` | `corp.meridian-retail.com` | DNS domain appended to every host → FQDN names |
| `DELIVERY_HOSTNAME` | `cmk-demo-gateway.${ESTATE_DOMAIN}` | name of the shell host in its `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port Checkmk polls (published 6559) |
| `HTTP_PORT` | `8080` | combined control panel (published 8099) |
| `ESTATE_HOSTS` | *(all)* | comma list of host names to carry (short names) |
| `ESTATE_REPLICAS` | `1` | stamp out every replicable host class N times (web-frontend-02, ... — steady-green copies; incidents stay unique to the originals) |
| `CHILD_AGENT_BASE` | `7600` | internal child agent port base (`+ registry index`) |
| `CHILD_HTTP_BASE` | *(auto)* | internal child admin port base (placed after the agent range) |
| `AGENT_VERSION` | `2.5.0-2026.04.03` | version in the delivery header |
| `CASCADE_TIME_SCALE` | `1.0` | scale the cross-host cascade timeline (`0.1` → ~45 s for a short slot) |

Per-host incident timing (`AUTO_BREAK_AFTER_MIN`, `LEAK_FILL_MIN`,
`BREAK_RAMP_MIN`, …) is inherited by every child from the environment, so you
can set them once on the delivery container.
