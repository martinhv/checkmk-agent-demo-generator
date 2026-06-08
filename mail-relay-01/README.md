# Demo: `mail-relay-01` — downstream MX unreachable → deferred mail-queue backlog

The transactional-mail relay of the Meridian Retail estate (see `../FLEET.md`):
an Ubuntu 24.04 **Postfix** host (`postfix@-.service`) that injects and forwards
the shop's order confirmations and receipts to a downstream smarthost / MX. The
incident is a clean, single-root-cause story: the downstream MX becomes
unreachable, so outbound mail can no longer be delivered and Postfix keeps
re-queueing it — the **deferred** queue backs up while the **active** queue and
Postfix itself stay perfectly healthy (local injection still works). *"Explain
with AI"* fuses *deferred growing + active fine + Postfix status OK + CPU/mem/disk
green* into the root cause: **the relay host is healthy; the downstream MX is
unreachable — fix the relay target / DNS, don't touch this box.**

- **TCP 6556** — Check_MK agent output (plaintext; the fetcher sees `<<` →
  `TransportProtocol.PLAIN`, no TLS/registration). The controller-status section
  pretends the host is TLS-registered (no "TLS not activated" warning).
- **TCP 8080** — `/admin` control UI + curl toggle API. Nothing here is
  monitored.

## The incident it fakes

| Service | Healthy | Degraded | Broken | Role |
|---|---|---|---|---|
| **Postfix Queue default** | deferred ~1-3, active ~2-6 (levels 10/20) | **OK / climbing** — deferred rises 4 → ~18, still under the CRIT | **CRIT** — deferred > 20 and **growing live** across re-polls (default levels 10/20) | the headline; the backlog made visible |
| **Postfix status default** | OK — Postfix mail system running | OK | **OK** — still running | the tell: the daemon is fine, the box is healthy |
| Outbound network (eth0) | steady | sagging | dropping (nothing delivers) | corroboration |
| TCP conn stats | ESTABLISHED smtp steady | — | ESTABLISHED drops, SYN_SENT to dead MX rises | corroboration |
| everything else (CPU, load, memory, filesystems, disk SMART, time sync, APT, units, jobs) | OK | OK | OK | low-noise: one root cause |

A backed-up **deferred** queue does **not** burn CPU, fill RAM or fill the spool
volume (queued mail just sits as small files on a huge volume), so every
resource check stays green — exactly the picture that proves *the relay host is
not the problem*. The **active** queue stays small in every state because local
injection always works; that, plus the green Postfix-status service, is what
points the diagnosis downstream.

The `<<<postfix_mailq>>>` section emits, per instance (`[[[default]]]`):
`QUEUE_deferred <size_bytes> <count>` and `QUEUE_active <size_bytes> <count>`
(the agent's exact format). Default deferred levels are `(10, 20)`, active
`(200, 300)` — only the deferred count is engineered to cross.

**Three states, because the timeline matters:**

- `healthy` → all green. Deferred ~1-3, active ~2-6, mail flows.
- `degraded` → the downstream MX gets slow/flaky: a fraction of deliveries fail,
  so the deferred queue climbs from the healthy base toward ~18 over
  `DEFER_CLIMB_MIN` (~16 min) and plateaus there — **clearly rising, still under
  the CRIT (20)**. The breadcrumb. Postfix status still OK. **Trigger ~15-20 min
  before showtime.**
- `broken` → the MX is fully unreachable: nothing outbound delivers, the
  deferred queue **grows live** as a function of broken-time (~0.8 mail/s once
  the ramp is full) → crosses 20 within a couple of polls and keeps climbing →
  **Postfix Queue default CRIT**. The active queue stays small, Postfix status
  stays OK. `degraded` **auto-escalates to `broken` after `AUTO_BREAK_AFTER_MIN`**
  (default 18 min); the growth rate ramps over `BREAK_RAMP_MIN` (~3 min), no
  vertical cliff.

---

## 1. Run it

```bash
cd mail-relay-01
docker compose up --build -d
docker compose logs -f
```

Published on `127.0.0.1` as **6564** (agent) and **8094** (admin). Stdlib-only,
so without Docker:

```bash
AGENT_PORT=6564 HTTP_PORT=8094 START_STATE=healthy python3 serve.py
```

## 2. Set it up in Checkmk

1. *Setup → Hosts → Add host*. Name `mail-relay-01`, IP `127.0.0.1`, **Checkmk
   agent port → 6564**.
2. Service discovery (any state — no discovery-time baselines here). Activate.
   Everything green; the *Postfix Queue default* and *Postfix status default*
   services appear.

No extra rules are needed: the deferred-queue levels (10/20) are the default
parameters of the `postfix_mailq` check.

## 3. Demo choreography

| When | Action | What Checkmk shows |
|---|---|---|
| T−18 min | `curl localhost:8094/admin/degrade` | nothing red yet. The *Postfix Queue default* deferred graph starts climbing (4 → ~18), still OK/green — the breadcrumb |
| ~T | *nothing* — degraded **auto-escalates after 18 min** (or `/admin/break`) | deferred crosses 20 and keeps growing live across polls → **Postfix Queue default CRIT**; active stays small; Postfix status still OK |
| the page | open the Postfix Queue default CRIT | "deferred queue length 24…". Instinct: restart Postfix / flush the queue |
| ⭐ Explain with AI | one click | fuses deferred-growing + active-fine + Postfix-status-OK + CPU/mem/disk green → *"the relay host is healthy; the downstream MX is unreachable and mail is deferring — fix the relay target / DNS, restarting Postfix won't help"* |
| resolve | `curl localhost:8094/admin/heal` ("MX restored") | next polls: deferred drains back toward 1-3, all green |

**Control UI:** `http://localhost:8094/admin` — state badge, time-in-state,
"deferred queue N and growing live", per-state effect cards with toggle buttons,
5 s auto-refresh.

```bash
curl http://localhost:8094/admin/degrade   # MX flaky, deferred climbs (still OK)
curl http://localhost:8094/admin/break      # MX unreachable, deferred CRIT + growing
curl http://localhost:8094/admin/heal        # MX restored, all green
curl http://localhost:8094/                  # JSON: state, deferred_queue, active_queue, ...
```

## 4. Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CMK_HOSTNAME` | `mail-relay-01` | name in `<<<check_mk>>>` |
| `AGENT_PORT` | `6556` | agent TCP port (published 6564) |
| `HTTP_PORT` | `8080` | admin port (published 8094) |
| `START_STATE` | `healthy` | `healthy` \| `degraded` \| `broken` |
| `AUTO_BREAK_AFTER_MIN` | `18` | minutes in `degraded` before the MX goes fully unreachable (`0` = never) |
| `DEFER_CLIMB_MIN` | `16` | minutes for the deferred queue to climb to its degraded plateau (~18) |
| `BREAK_RAMP_MIN` | `3` | minutes over which the broken deferred-growth rate reaches full force |
| `STATE_FILE` | `/var/tmp/cmk-demo-mail-relay-state.json` | persists counters/uptime/incident across restarts (`""` = off) |
