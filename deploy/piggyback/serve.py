#!/usr/bin/env python3
"""Meridian Retail demo: piggyback delivery host (the "shell").

An *optional* alternative to adding every estate host in Checkmk as its own
TCP host: run THIS one host, and the whole estate shows up as **piggyback
hosts** hanging off it. The delivery host itself carries only a minimal agent
section — it's just the shell that carries everyone else's data.

How it works
------------
Checkmk piggyback: any sections a host's agent wraps in `<<<<other-host>>>>`
... `<<<<>>>>` markers are attributed by the site to *other-host*, not to the
delivering host (the empty `<<<<>>>>` switches back). So this script:

  1. spawns each estate host's own, unmodified `serve.py` as a child process on
     an internal 127.0.0.1 port (reusing 100 % of the existing demos — including
     their break/heal toggles and restart persistence);
  2. on every agent poll, emits the delivery host's own minimal `<<<check_mk>>>`
     (+ controller status, so its Check_MK Agent service is OK and TLS-clean),
     then fetches each child's full agent output and re-frames it as
     `<<<<hostname>>>>` ... `<<<<>>>>` piggyback blocks.

In Checkmk you then add ONE TCP host (this delivery shell) plus the estate
hosts as **piggyback** hosts (no agent connection, no per-host port override).

It also serves a single combined `/admin` control panel that proxies the
break/heal toggles to the right child — one screen to drive the whole estate.

Plaintext TCP, stdlib only. Select a subset with ESTATE_HOSTS (comma list);
default = all. Scale UP with ESTATE_REPLICAS=N: every replicable host class
is stamped out N times (web-frontend-01, -02, ... -0N) — the original keeps
its incident toggles, the replicas run steady green as estate background.

Delivery mode (DELIVERY_MODE): `piggyback` (default, as above) or `datasource`
— the latter writes each host's agent output to a file (AGENT_OUTPUT_DIR) and
Checkmk reads it per host via a `cat $FILENAME$` datasource program instead of
piggyback. Better scaling (no single-shell fetch bottleneck), self-hosted only
(needs filesystem access). See deploy/cmk_setup.py for the matching site setup.

Config via env:
  DELIVERY_HOSTNAME  name of the shell host        (default: cmk-demo-gateway)
  AGENT_PORT         TCP port Checkmk polls         (default: 6556)
  HTTP_PORT          combined /admin control port   (default: 8080)
  DELIVERY_MODE      piggyback | datasource         (default: piggyback)
  AGENT_OUTPUT_DIR   datasource: where files go     (default: /var/tmp/...)
  AGENT_OUTPUT_INTERVAL  datasource: refresh seconds (default: 20)
  ESTATE_HOSTS       comma list of host names to carry (default: all)
  ESTATE_REPLICAS    replica multiplier for replicable classes (default: 1)
  ESTATE_FLEET       "1": also spawn fleet/serve.py (ONE process carrying the
                     ~140 steady-green bulk hosts of the 300-host estate) and
                     deliver its hosts alongside the classic roster
  FLEET_HTTP_PORT    fleet child's internal HTTP port (default: 8102)
  AGENT_VERSION      version in the delivery header (default: 2.5.0-...)
  CHILD_AGENT_BASE   internal child agent port base (default: 7600)
  CHILD_HTTP_BASE    internal child admin port base (default: auto after
                     the agent range, so big estates can't collide)
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import StreamRequestHandler, ThreadingTCPServer
from typing import Any

# Estate DNS domain — every host shows up in Checkmk as <short>.<ESTATE_DOMAIN>
# (FQDN). The short name stays the internal label (panel, ports, selection).
ESTATE_DOMAIN = os.environ.get("ESTATE_DOMAIN", "corp.meridian-retail.com")
DELIVERY_HOSTNAME = os.environ.get("DELIVERY_HOSTNAME", f"cmk-demo-gateway.{ESTATE_DOMAIN}")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
CHILD_AGENT_BASE = int(os.environ.get("CHILD_AGENT_BASE", "7600"))

# Delivery mode — how the estate hosts' agent data reaches Checkmk:
#   piggyback   (default) the shell's own agent embeds every child as a
#               <<<<host>>>> piggyback block; Checkmk polls only the shell.
#   datasource  (self-hosted) each child's agent output is written to a file
#               and Checkmk reads it per host via a "cat $FILENAME$" datasource
#               program ("Individual program call instead of agent access").
#               Scales better (no single-shell fetch bottleneck, no piggyback
#               dependency) but needs filesystem access, so it's self-hosted
#               only. The shell then emits ONLY its own minimal section.
DELIVERY_MODE = os.environ.get("DELIVERY_MODE", "piggyback")
# Where the per-host agent files are written in datasource mode. Must be
# readable by the site user running the "cat" program (a world-readable path
# like /var/tmp works without any sudo — the file need not live under the site).
AGENT_OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "/var/tmp/cmk-demo-agent-output")
AGENT_OUTPUT_INTERVAL = float(os.environ.get("AGENT_OUTPUT_INTERVAL", "20"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOSTS_DIR = os.path.join(REPO_ROOT, "hosts")
START = time.time()


# Estate roster: (hostname, directory under hosts/, toggle actions, extra
# child env, parent, replicable).
# `actions` drives the combined control panel; [] = steady-green background.
# `parent` is the short name of the upstream network device — exposed as an
# FQDN in the panel JSON and applied as the Checkmk "parents" attribute by
# deploy/cmk_setup.py. The network layer is the SNMP-simulated gear
# (snmp/netsim.py): these servers hang off the access switch sw-access-01
# (which in turn uplinks to the campus core sw-core-01), applied only when
# the SNMP devices are deployed too. Endpoints belong on an access switch,
# not the core — the core's ports are all switch/router uplink trunks.
# `replicable` marks classes that ESTATE_REPLICAS stamps out N times
# (web-frontend-02, -03, ...) — replicas run steady green; incident stories
# stay unique to the original (low noise, one root cause).
@dataclass(slots=True)
class HostSpec:
    name: str  # host + directory under hosts/ (identical by convention)
    directory: str
    actions: list[str]  # toggles for the /admin panel; [] = steady-green
    extra_env: dict[str, str]
    parent: str | None  # upstream SNMP device (Checkmk parents attr; see below)
    replicable: bool = False  # ESTATE_REPLICAS stamps these out N times


_A = ["degrade", "break", "heal"]  # the common incident toggle set
_HEALTHY = {"START_STATE": "healthy"}
_REGISTRY = [
    HostSpec("web-frontend-01", "web-frontend-01", [], _HEALTHY, "sw-access-01", replicable=True),
    HostSpec(
        "payment-api", "payment-api", ["break", "heal"], {"START_BROKEN": "0"}, "sw-access-01"
    ),
    HostSpec("app-worker-01", "app-worker-01", _A, _HEALTHY, "sw-access-01", replicable=True),
    HostSpec("app-redis-01", "app-redis-01", _A, _HEALTHY, "sw-access-01", replicable=True),
    HostSpec("db-postgres-01", "db-postgres-01", _A, _HEALTHY, "sw-access-01"),
    HostSpec("db-postgres-02", "db-postgres-02", _A, _HEALTHY, "sw-access-01", replicable=True),
    HostSpec("mail-relay-01", "mail-relay-01", _A, _HEALTHY, "sw-access-01", replicable=True),
    HostSpec("fileserver-01", "fileserver-01", _A, _HEALTHY, "sw-access-01", replicable=True),
    HostSpec("backup-01", "backup-01", [], _HEALTHY, "sw-access-01"),
    HostSpec("win-dc-01", "win-dc-01", _A, _HEALTHY, "sw-access-01", replicable=True),
]


def _replica_name(base: str, n: int) -> str:
    """web-frontend-01 -> web-frontend-02, ... (suffix numbering continues)."""
    stem = base[:-3] if base.endswith("-01") else base
    return f"{stem}-{n:02d}"


class Child:
    def __init__(
        self,
        idx: int,
        name: str,
        directory: str,
        actions: list[str],
        extra_env: dict[str, str],
        parent: str | None,
    ) -> None:
        self.name = name
        self.directory = directory
        self.actions = actions
        self.extra_env = extra_env
        self.parent = parent
        self.agent_port = CHILD_AGENT_BASE + idx
        self.http_port = CHILD_HTTP_BASE + idx
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def fqdn(self) -> str:
        # the name Checkmk sees (piggyback target + the child's own Hostname:)
        return f"{self.name}.{ESTATE_DOMAIN}"

    @property
    def script(self) -> str:
        return os.path.join(HOSTS_DIR, self.directory, "serve.py")

    def spawn(self) -> None:
        env = dict(os.environ)
        env.update(
            {
                "CMK_HOSTNAME": self.fqdn,
                "AGENT_PORT": str(self.agent_port),
                "HTTP_PORT": str(self.http_port),
                "STATE_FILE": f"/var/tmp/cmk-demo-pb-{self.name}.json",
            }
        )
        # only inject our defaults if the operator hasn't overridden them
        for k, v in self.extra_env.items():
            env.setdefault(k, v)
        if not os.path.exists(self.script):
            print(f"[pb] WARN: {self.script} missing — skipping {self.name}")
            return
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", self.script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(
            f"[pb] spawned {self.name:16} agent=127.0.0.1:{self.agent_port} "
            f"admin=127.0.0.1:{self.http_port}"
        )

    def wait_ready(self, timeout: float = 15.0) -> bool:
        if self.proc is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.agent_port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.25)
        print(f"[pb] WARN: {self.name} did not open its agent port in {timeout:g}s")
        return False

    def fetch_agent(self, timeout: float = 6.0) -> bytes:
        """Read the child's full agent output over TCP (raw)."""
        try:
            with socket.create_connection(("127.0.0.1", self.agent_port), timeout=timeout) as s:
                s.settimeout(timeout)
                chunks: list[bytes] = []
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except OSError as exc:
            print(f"[pb] WARN: fetch from {self.name} failed: {exc}")
            return b""

    def child_state(self) -> str | None:
        # most hosts return {"state": ...} on "/"; payment-api serves a health
        # page there and its JSON status on "/admin/status" — try both.
        for ep in ("/", "/admin/status"):
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{self.http_port}{ep}", timeout=2
                ) as r:
                    state = json.loads(r.read()).get("state")
                if state:
                    return state
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return None

    def fetch_meta(self) -> dict[str, Any] | None:
        """Read the child's state-change info (STATE_META) for the info tab."""
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{self.http_port}/admin/meta", timeout=2
            ) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def toggle(self, action: str) -> bool:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{self.http_port}/admin/{action}", timeout=3
            ) as r:
                r.read()
                return True
        except (urllib.error.URLError, OSError):
            return False

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# --------------------------------------------------------------------------- #
#  Fleet: ONE child process (fleet/serve.py) carries the steady-green bulk of
#  the 300-host estate. Each of its hosts is exposed here as a FleetHost that
#  duck-types Child (fetch_agent / child_state / ...), so delivery (piggyback
#  blocks, datasource files) and the panel JSON treat them like any child.
# --------------------------------------------------------------------------- #
ESTATE_FLEET = os.environ.get("ESTATE_FLEET", "0") == "1"
FLEET_HTTP_PORT = int(os.environ.get("FLEET_HTTP_PORT", "8102"))
FLEET_SCRIPT = os.path.join(REPO_ROOT, "fleet", "serve.py")


class FleetManager:
    """Owns the single fleet child process and its roster."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.base = f"http://127.0.0.1:{FLEET_HTTP_PORT}"

    def spawn(self) -> None:
        env = dict(os.environ)
        env.update(
            {"HTTP_PORT": str(FLEET_HTTP_PORT), "STATE_FILE": "/var/tmp/cmk-demo-fleet-state.json"}
        )
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", FLEET_SCRIPT],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[pb] spawned fleet manager http=127.0.0.1:{FLEET_HTTP_PORT}")

    def roster(self, timeout: float = 20.0) -> list[dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    self.base + "/", timeout=3
                ) as r:
                    return json.loads(r.read())["fleet"]
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        print("[pb] WARN: fleet manager did not answer — fleet hosts skipped")
        return []

    def fetch_agent(self, short: str, timeout: float = 6.0) -> bytes:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{self.base}/agent/{short}", timeout=timeout
            ) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as exc:
            print(f"[pb] WARN: fleet fetch {short} failed: {exc}")
            return b""

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class FleetHost:
    """Child-shaped adapter for one fleet host (always steady-green)."""

    actions: list[str] = []

    def __init__(self, mgr: FleetManager, info: dict[str, Any]) -> None:
        self.mgr = mgr
        self.name = info["name"]
        self.fqdn = info["fqdn"]
        self.parent = info.get("parent")
        self.role = info.get("role")
        self.os = info.get("os")
        self.descr = info.get("descr", "")

    def fetch_agent(self) -> bytes:
        return self.mgr.fetch_agent(self.name)

    def child_state(self) -> str:
        return "healthy"

    def fetch_meta(self) -> dict[str, Any] | None:
        return {
            "state": "healthy",
            "in_state_for_s": time.time() - START,
            "action_to_state": {},
            "states": {
                "healthy": {
                    "label": "HEALTHY",
                    "color": "#2e7d32",
                    "tagline": f"{self.descr} — steady-green fleet host, "
                    "no incident and no toggle.",
                    "effects": [
                        "all services green, values wobble naturally",
                        "part of the 300-host estate bulk (fleet/profiles.py)",
                    ],
                }
            },
        }

    def toggle(self, action: str) -> bool:  # noqa: ARG002
        return False

    def terminate(self) -> None:
        pass  # the FleetManager owns the process


FLEET_MGR = FleetManager() if ESTATE_FLEET else None

_selected = os.environ.get("ESTATE_HOSTS", "").strip()
_wanted = {h.strip() for h in _selected.split(",") if h.strip()} if _selected else None
_replicas = max(1, int(os.environ.get("ESTATE_REPLICAS", "1") or "1"))

# roster: selected classes, each replicable class stamped out _replicas times.
# Replicas force a healthy start and carry no toggle actions — incidents stay
# unique to the original (low noise, one root cause).
_roster: list[HostSpec] = []
for spec in _REGISTRY:
    if _wanted is not None and spec.name not in _wanted:
        continue
    _roster.append(spec)
    if spec.replicable:
        for n in range(2, _replicas + 1):
            green = {**spec.extra_env, "START_STATE": "healthy", "START_BROKEN": "0"}
            _roster.append(
                HostSpec(_replica_name(spec.name, n), spec.directory, [], green, spec.parent)
            )

# keep the internal admin ports clear of the agent range however big the
# estate gets (agent ports occupy CHILD_AGENT_BASE .. +len(_roster))
CHILD_HTTP_BASE = int(
    os.environ.get("CHILD_HTTP_BASE", str(CHILD_AGENT_BASE + max(100, len(_roster) + 10)))
)

CHILDREN: list[Child | FleetHost] = [
    Child(i, s.name, s.directory, s.actions, s.extra_env, s.parent) for i, s in enumerate(_roster)
]
_BY_NAME = {c.name: c for c in CHILDREN}


# --------------------------------------------------------------------------- #
#  Cross-host cascade: ONE trigger, a dependency-ordered chain of incidents
# --------------------------------------------------------------------------- #
# A single failure rarely stays on one host — it propagates *up the dependency
# stack*. This orchestrates exactly that from one button: a scripted timeline
# that flips each host's OWN break/degrade toggle at the right moment, so the
# estate lights up in a realistic causal order instead of all at once. Every
# host keeps its own incident logic (values, persistence, auto-escalation);
# the cascade only decides *when* to pull each host's existing lever.
#
# The story (Meridian's payments platform brownout) — ONE root cause, the
# dying SATA SSD in the primary DB, everything downstream a *symptom*:
#
#   T+0   db-postgres-01 degrade  read-retry/ECC storms begin — intermittent
#                                  latency spikes; SMART still PASSED. The quiet
#                                  breadcrumb (root cause, fires first).
#   T+2   db-postgres-02 degrade  the standby's replication lag grows as the
#                                  primary's I/O stalls (replays from a struggling primary).
#   T+3   app-worker-01  degrade  settlement commits to the primary slow down;
#                                  the job queue backs up, JVM heap starts filling.
#   T+5   db-postgres-01 break    primary disk fully fails-slow: iowait pegged,
#                                  query-latency cliff — the storage collapse.
#   T+6   payment-api    break    DB writes time out -> the payment API returns
#                                  errors: the customer-facing symptom (the page).
#   T+7   app-worker-01  break    buffered jobs overflow the heap -> OOM-killed,
#                                  order-worker.service flaps.
#
# The demo point: by showtime the AI faces a wall of red across five services
# on four hosts, and must trace it back to the ONE disk that started 7 min
# earlier — cross-signal fusion (disk latency + iowait + replication lag +
# heap/OOM + payment errors), not "whatever went red first".
#
# app-redis-01 and the web frontend stay GREEN on purpose (low noise, one
# root cause): the cache and edge tier corroborate by NOT failing.
#
# Timing: delays are seconds from the trigger, scaled by CASCADE_TIME_SCALE
# (default 1.0 = the ~7-min real timeline; e.g. 0.1 compresses it to ~45 s for
# testing / a short stage slot). Steps whose host isn't in this estate
# selection (ESTATE_HOSTS) or whose action the host doesn't expose are skipped.
CASCADE_TIME_SCALE = float(os.environ.get("CASCADE_TIME_SCALE", "1.0") or "1.0")
CASCADE_STATE_FILE = os.environ.get("CASCADE_STATE_FILE", "/var/tmp/cmk-demo-cascade-state.json")


@dataclass(slots=True)
class CascadeStep:
    delay_s: float  # seconds after trigger (before CASCADE_TIME_SCALE)
    host: str  # short name (must be a carried host)
    action: str  # the host's own toggle: degrade | break | heal
    why: str  # one-line storyline note (shown on the panel)


CASCADE_STEPS: list[CascadeStep] = [
    CascadeStep(
        0,
        "db-postgres-01",
        "degrade",
        "SATA SSD read-retry/ECC storms begin — "
        "intermittent latency spikes, SMART still PASSED (root cause, quiet)",
    ),
    CascadeStep(
        120,
        "db-postgres-02",
        "degrade",
        "standby replication lag grows as the primary's I/O stalls",
    ),
    CascadeStep(
        180,
        "app-worker-01",
        "degrade",
        "settlement commits to the primary slow down; job queue backs up, JVM heap starts filling",
    ),
    CascadeStep(
        300,
        "db-postgres-01",
        "break",
        "primary disk fully fails-slow: iowait pegged, query-latency cliff — the storage collapse",
    ),
    CascadeStep(
        360,
        "payment-api",
        "break",
        "DB writes time out -> payment API returns errors — the customer-facing symptom",
    ),
    CascadeStep(
        420,
        "app-worker-01",
        "break",
        "buffered jobs overflow the heap -> OOM-killed, order-worker.service flaps",
    ),
]


class Cascade:
    """Scheduler for the cross-host cascade.

    One always-running daemon loop watches the clock; `start()` arms it and each
    step fires (its host's toggle) once `elapsed >= delay_s * CASCADE_TIME_SCALE`.
    `heal()` stops the run and heals every participating host. State (armed +
    trigger time + which steps fired) is persisted so a shell restart mid-cascade
    resumes the timeline instead of resetting it — re-firing a toggle is
    idempotent (the child is already in / moves to the target state), and the
    children persist their own incident state independently.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.started_at: float | None = None
        self.fired: dict[str, float] = {}  # step key -> wall-clock fired_at
        self._complete_logged = False
        # steps that actually apply to this estate selection (host carried +
        # action exposed by that host); others are shown as "skipped".
        self.steps: list[CascadeStep] = []
        self.skipped: list[CascadeStep] = []
        for s in CASCADE_STEPS:
            child = _BY_NAME.get(s.host)
            if child is not None and (not child.actions or s.action in child.actions):
                self.steps.append(s)
            else:
                self.skipped.append(s)

    @staticmethod
    def _key(i: int, s: CascadeStep) -> str:
        return f"{i}:{s.host}:{s.action}"

    @property
    def participants(self) -> list[str]:
        """Distinct carried hosts the cascade touches (for heal / display)."""
        seen: dict[str, None] = {}
        for s in self.steps:
            seen.setdefault(s.host, None)
        return list(seen)

    def scaled_delay(self, s: CascadeStep) -> float:
        return s.delay_s * CASCADE_TIME_SCALE

    def start(self) -> None:
        with self._lock:
            self.active = True
            self.started_at = time.time()
            self.fired = {}
            self._complete_logged = False
        print(
            f"[cascade] armed — {len(self.steps)} steps over "
            f"{self.scaled_delay(self.steps[-1]) if self.steps else 0:.0f}s "
            f"(scale {CASCADE_TIME_SCALE:g})"
        )
        self._save()

    def heal(self) -> None:
        with self._lock:
            self.active = False
            self.started_at = None
            self.fired = {}
            self._complete_logged = False
        for host in self.participants:
            child = _BY_NAME.get(host)
            if child is not None:
                ok = child.toggle("heal")
                print(f"[cascade] heal {host} ({'ok' if ok else 'FAILED'})")
        self._save()

    def _fire(self, key: str, s: CascadeStep) -> None:
        child = _BY_NAME.get(s.host)
        ok = child.toggle(s.action) if child is not None else False
        with self._lock:
            self.fired[key] = time.time()
        print(f"[cascade] fire {s.host} -> {s.action} ({'ok' if ok else 'FAILED'})")
        self._save()

    def _tick(self) -> None:
        """Fire every due-but-unfired step. Called by the loop and safe to call
        repeatedly; on restart it catches up any steps whose time already passed."""
        with self._lock:
            if not self.active or self.started_at is None:
                return
            elapsed = time.time() - self.started_at
            due = [
                (self._key(i, s), s)
                for i, s in enumerate(self.steps)
                if self._key(i, s) not in self.fired and elapsed >= self.scaled_delay(s)
            ]
        for key, s in due:
            self._fire(key, s)
        with self._lock:
            done = self.active and self.steps and len(self.fired) >= len(self.steps)
            if done and not self._complete_logged:
                self._complete_logged = True
                print("[cascade] complete — all steps fired")

    def _loop(self) -> None:
        while True:
            time.sleep(3)
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                print(f"[cascade] tick error: {exc}")

    def start_scheduler(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    # --- status + persistence ------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self.active
            started_at = self.started_at
            fired = dict(self.fired)
        elapsed = (time.time() - started_at) if (active and started_at) else 0.0
        steps: list[dict[str, Any]] = []
        for i, s in enumerate(self.steps):
            key = self._key(i, s)
            at = self.scaled_delay(s)
            steps.append(
                {
                    "at_s": round(at),
                    "host": s.host,
                    "action": s.action,
                    "why": s.why,
                    "fired": key in fired,
                    "eta_s": None if key in fired else (round(at - elapsed) if active else None),
                }
            )
        for s in self.skipped:
            steps.append(
                {
                    "at_s": round(self.scaled_delay(s)),
                    "host": s.host,
                    "action": s.action,
                    "why": s.why,
                    "skipped": True,
                }
            )
        done = active and self.steps and len(fired) >= len(self.steps)
        return {
            "active": active,
            "complete": bool(done),
            "elapsed_s": round(elapsed, 1),
            "time_scale": CASCADE_TIME_SCALE,
            "total_s": round(self.scaled_delay(self.steps[-1])) if self.steps else 0,
            "participants": self.participants,
            "steps": steps,
        }

    def _save(self) -> None:
        if not CASCADE_STATE_FILE:
            return
        with self._lock:
            data = {"active": self.active, "started_at": self.started_at, "fired": self.fired}
        try:
            tmp = CASCADE_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, CASCADE_STATE_FILE)
        except OSError as exc:
            print(f"[cascade] save failed: {exc}")

    def load(self) -> None:
        if not CASCADE_STATE_FILE or not os.path.exists(CASCADE_STATE_FILE):
            return
        try:
            with open(CASCADE_STATE_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"[cascade] load failed ({exc}) — starting idle")
            return
        with self._lock:
            self.active = bool(data.get("active"))
            self.started_at = data.get("started_at")
            self.fired = dict(data.get("fired") or {})
        if self.active:
            print(
                f"[cascade] resumed — armed since t+{time.time() - (self.started_at or 0):.0f}s, "
                f"{len(self.fired)}/{len(self.steps)} steps already fired"
            )


CASCADE = Cascade()


# --------------------------------------------------------------------------- #
#  Delivery agent output: minimal own section + piggyback blocks
# --------------------------------------------------------------------------- #
def _delivery_minimal() -> str:
    now = int(time.time())
    uptime = int(time.time() - START) + 3 * 86400
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now + 320 * 86400))
    lines = [
        "<<<check_mk>>>",
        f"Version: {AGENT_VERSION}",
        "AgentOS: linux",
        f"Hostname: {DELIVERY_HOSTNAME}",
        "OSType: linux",
        "OSName: Ubuntu",
        "OSVersion: 24.04",
        "OSPlatform: ubuntu",
        "FailedPythonReason: ",
        "SSHClient: ",
        # minimal but TLS-clean so the delivery host's own Check_MK Agent is OK
        "<<<cmk_agent_ctl_status:sep(0)>>>",
        json.dumps(
            {
                "version": AGENT_VERSION,
                "agent_socket_operational": True,
                "ip_allowlist": [],
                "allow_legacy_pull": False,
                "connections": [
                    {
                        "site_id": "monitoring/prod",
                        "receiver_port": 8000,
                        "uuid": "0e5a2c11-9d44-4a7b-bf01-7c2e9a3d6e10",
                        "local": {
                            "connection_mode": "pull-agent",
                            "cert_info": {
                                "issuer": "Site 'prod' local CA",
                                "from": "Tue, 03 Jun 2025 09:12:44 +0000",
                                "to": cert_to,
                            },
                        },
                        "remote": "remote_query_disabled",
                    }
                ],
            },
            separators=(",", ":"),
        ),
        # uptime so the shell has a couple of real services of its own
        "<<<uptime>>>",
        f"{uptime}.00 {int(uptime * 3.0)}.00",
    ]
    return "\n".join(lines) + "\n"


def build_delivery_output() -> bytes:
    out = bytearray(_delivery_minimal().encode("utf-8"))
    # in datasource mode the children are delivered as files (see the writer
    # below), so the shell carries only its own minimal section
    if DELIVERY_MODE == "datasource":
        return bytes(out)
    for child in CHILDREN:
        payload = child.fetch_agent()
        if not payload:
            continue  # child not up yet; better to omit than emit garbage
        out += f"<<<<{child.fqdn}>>>>\n".encode()
        out += payload
        if not payload.endswith(b"\n"):
            out += b"\n"
        out += b"<<<<>>>>\n"
    return bytes(out)


# --------------------------------------------------------------------------- #
#  Datasource mode: write each host's agent output to a file
# --------------------------------------------------------------------------- #
def _write_file(name: str, payload: bytes) -> bool:
    """Atomically write one host's agent output to <AGENT_OUTPUT_DIR>/<name>
    (tmp + rename so a half-written file is never cat'd), world-readable so the
    site user's datasource program can read it. Empty payload -> keep the last
    good file rather than truncate it to nothing."""
    if not payload:
        return False
    path = os.path.join(AGENT_OUTPUT_DIR, name)
    tmp = os.path.join(AGENT_OUTPUT_DIR, f".{name}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(f"[files] WARN: writing {path} failed: {exc}")
        return False


def write_agent_files() -> int:
    """One pass: the shell's own minimal section plus every child's full agent
    output, each to its own file named by the FQDN Checkmk uses ($HOSTNAME$)."""
    os.makedirs(AGENT_OUTPUT_DIR, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(AGENT_OUTPUT_DIR, 0o755)
    wrote = _write_file(DELIVERY_HOSTNAME, _delivery_minimal().encode("utf-8"))
    for child in CHILDREN:
        wrote += _write_file(child.fqdn, child.fetch_agent())
    return int(wrote)


def agent_file_writer() -> None:
    while True:
        time.sleep(AGENT_OUTPUT_INTERVAL)
        n = write_agent_files()
        print(f"[files] refreshed {n}/{len(CHILDREN) + 1} agent files in {AGENT_OUTPUT_DIR}")


# --------------------------------------------------------------------------- #
#  Servers
# --------------------------------------------------------------------------- #
class AgentHandler(StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.wfile.write(build_delivery_output())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class AgentServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _host_info_page(child: Child | FleetHost) -> str:
    """Per-host 'what happens on a state change' tab — mirrors the per-demo
    control screens, but rendered by the delivery shell from the child's
    /admin/meta so the estate is driven from one place."""
    meta = child.fetch_meta()
    if not meta:
        return (
            "<!doctype html><meta charset='utf-8'>"
            "<body style='background:#1a1d21;color:#d8dee4;"
            "font-family:system-ui,sans-serif;margin:2rem auto;max-width:40rem'>"
            f"<p>{child.name}: state info not available yet (child still starting). "
            "<a style='color:#6cf' href='/admin'>&larr; back to estate</a></p>"
        )
    states = meta.get("states", {})
    a2s = meta.get("action_to_state", {})
    cur = meta.get("state")
    cur_meta = states.get(cur, {})
    # one card per reachable state (action -> target); steady-green hosts have
    # no actions, so just show their single state with no button.
    pairs = list(a2s.items()) if a2s else [(None, name) for name in states]
    cards: list[str] = []
    for action, target in pairs:
        tmeta = states.get(target, {})
        color = tmeta.get("color", "#666")
        current = target == cur
        effects = "".join(f"<li>{e}</li>" for e in tmeta.get("effects", []))
        if current:
            btn = "<span class='btn current'>current state</span>"
        elif action:
            btn = (
                f"<a class='btn' href='/admin/{child.name}/{action}?back=info' "
                f"style='background:{color}'>&rarr; {action}</a>"
            )
        else:
            btn = ""
        cards.append(
            f"<div class='card{' active' if current else ''}' "
            f"style='border-color:{color}'>"
            f"<h2 style='color:{color}'>{tmeta.get('label', str(target).upper())}</h2>"
            f"<p class='tag'>{tmeta.get('tagline', '')}</p><ul>{effects}</ul>{btn}</div>"
        )

    badge_color = cur_meta.get("color", "#666")
    since = _fmt_duration(meta.get("in_state_for_s"))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>{child.name} — state info ({DELIVERY_HOSTNAME})</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:72rem; padding:0 1rem; }}
 a.back {{ color:#6cf; text-decoration:none; font-size:.9rem; }}
 h1 {{ font-weight:600; font-size:1.3rem; color:#9aa4af; margin:.4rem 0; }}
 h1 b {{ color:#d8dee4; }}
 .state {{ display:inline-block; padding:.4rem 1.1rem; border-radius:.4rem;
          color:#fff; font-weight:700; font-size:1.5rem; letter-spacing:.05em;
          background:{badge_color}; }}
 .since {{ color:#9aa4af; margin:.6rem 0 0; }}
 .cards {{ display:flex; gap:1rem; margin-top:1.6rem; flex-wrap:wrap; }}
 .card {{ flex:1 1 20rem; border:2px solid #333; border-radius:.6rem;
         padding:1rem 1.2rem; background:#22262b; opacity:.85; }}
 .card.active {{ opacity:1; background:#262b31;
                box-shadow:0 0 14px rgba(255,255,255,.06); }}
 .card h2 {{ margin:.1rem 0 .4rem; font-size:1.1rem; }}
 .card .tag {{ color:#9aa4af; min-height:2.6rem; margin:.2rem 0 .4rem; }}
 .card ul {{ padding-left:1.2rem; margin:.4rem 0 1rem; }}
 .card li {{ margin:.25rem 0; font-size:.92rem; }}
 .btn {{ display:inline-block; padding:.45rem 1.1rem; border-radius:.4rem;
        color:#fff; text-decoration:none; font-weight:600; }}
 .btn.current {{ background:#444; color:#aaa; cursor:default; }}
 .foot {{ margin-top:2rem; color:#666; font-size:.85rem; }}
</style></head><body>
 <a class="back" href="/admin">&larr; back to estate overview</a>
 <h1>state info — <b>{child.name}</b>
  <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{cur_meta.get("label", str(cur).upper())}</div>
 <div class="since">in this state for <b>{since}</b> — {cur_meta.get("tagline", "")}</div>
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">Each card is a target state and the Checkmk services that change when you
  switch to it. Buttons toggle this host and return here. Piggyback host
  carried by <b>{DELIVERY_HOSTNAME}</b>.</div>
</body></html>"""


def _cascade_section() -> str:
    """The one-button cross-host cascade: status badge + the causal timeline +
    trigger / heal-all controls. Rendered at the top of the estate overview so
    the whole story can be driven from a single control."""
    st = CASCADE.status()
    if st["complete"]:
        badge_c, badge_t = "#c62828", "COMPLETE"
    elif st["active"]:
        badge_c, badge_t = "#f9a825", f"RUNNING · t+{_fmt_duration(st['elapsed_s'])}"
    else:
        badge_c, badge_t = "#3a4350", "IDLE"

    items: list[str] = []
    for s in st["steps"]:
        at = _fmt_duration(s["at_s"])
        if s.get("skipped"):
            mark, cls, note = "&#8856;", "skip", " — not in this estate selection"
        elif s.get("fired"):
            mark, cls, note = "&#10003;", "done", ""
        elif st["active"]:
            eta = s.get("eta_s")
            mark, cls = "&#9203;", "pend"
            note = f" — in {_fmt_duration(max(0, eta))}" if eta is not None else ""
        else:
            mark, cls, note = "&#9675;", "idle", ""
        items.append(
            f"<li class='cs {cls}'><span class='m'>{mark}</span>"
            f"<span class='at'>T+{at}</span> "
            f"<b>{s['host']}</b> <span class='act'>{s['action']}</span>"
            f"<span class='why'>{s['why']}{note}</span></li>"
        )

    if st["active"]:
        controls = (
            "<a class='cbtn heal' href='/admin/cascade/heal'>&#9632; stop &amp; heal all</a>"
            "<a class='cbtn again' href='/admin/cascade/start'>&#8635; restart</a>"
        )
    else:
        controls = "<a class='cbtn go' href='/admin/cascade/start'>&#9654; trigger cascade</a>"

    scale = st["time_scale"]
    scale_note = "" if scale == 1.0 else f" · time&times;{scale:g}"
    return f"""
 <div class="casc">
  <div class="chead">
   <div>
    <h2>cross-host cascade <span class="cbadge" style="background:{badge_c}">{badge_t}</span></h2>
    <p class="csub">payments-platform brownout — one root cause (dying disk on
     <b>db-postgres-01</b>) propagating up the stack over
     ~{_fmt_duration(st["total_s"])}{scale_note}. The frontend &amp; cache stay
     green (low noise, one root cause).</p>
   </div>
   <div class="cctl">{controls}</div>
  </div>
  <ul class="clist">{"".join(items)}</ul>
 </div>
 <style>
  .casc {{ border:1px solid #33404d; border-radius:.6rem; background:#20262d;
          padding:1rem 1.2rem; margin:1rem 0 1.6rem; }}
  .chead {{ display:flex; justify-content:space-between; align-items:flex-start;
           gap:1rem; flex-wrap:wrap; }}
  .casc h2 {{ margin:.1rem 0 .3rem; font-size:1.1rem; color:#d8dee4; }}
  .cbadge {{ font-size:.72rem; font-weight:700; letter-spacing:.05em; color:#fff;
            padding:.12rem .55rem; border-radius:.3rem; vertical-align:.12em; }}
  .csub {{ color:#9aa4af; margin:.2rem 0 0; max-width:44rem; font-size:.9rem; }}
  .cctl {{ display:flex; gap:.4rem; flex-wrap:wrap; }}
  .cbtn {{ display:inline-block; padding:.5rem 1rem; border-radius:.4rem; color:#fff;
          text-decoration:none; font-weight:600; font-size:.9rem; white-space:nowrap; }}
  .cbtn.go {{ background:#c62828; }} .cbtn.heal {{ background:#2e7d32; }}
  .cbtn.again, .cbtn.stop {{ background:#3a4350; }}
  .clist {{ list-style:none; margin:1rem 0 0; padding:0; }}
  .cs {{ display:flex; align-items:baseline; gap:.5rem; padding:.28rem 0;
        border-top:1px solid #2a2e34; font-size:.9rem; }}
  .cs .m {{ width:1.2rem; text-align:center; }}
  .cs .at {{ color:#7f8b97; font-variant-numeric:tabular-nums; min-width:3.6rem; }}
  .cs .act {{ color:#9aa4af; font-size:.82rem; padding:.02rem .4rem; border:1px solid #3a4350;
             border-radius:.25rem; margin-left:.1rem; }}
  .cs .why {{ color:#7f8b97; flex:1 1 100%; margin-left:1.7rem; font-size:.82rem; }}
  .cs.done {{ color:#e07a7a; }} .cs.done .m {{ color:#c62828; }}
  .cs.pend {{ color:#d8dee4; }} .cs.pend .m {{ color:#f9a825; }}
  .cs.idle {{ color:#9aa4af; }} .cs.skip {{ opacity:.45; }}
 </style>"""


def _overview_page() -> str:
    rows: list[str] = []
    colors = {"healthy": "#2e7d32", "degraded": "#f9a825", "broken": "#c62828", None: "#666"}
    fleet = [c for c in CHILDREN if isinstance(c, FleetHost)]
    for c in CHILDREN:
        if isinstance(c, FleetHost):
            continue  # rendered compactly below the classic roster
        state = c.child_state()
        badge = (
            f"<span class='b' style='background:{colors.get(state, '#666')}'>"
            f"{(state or 'n/a').upper()}</span>"
        )
        if c.actions:
            btns = " ".join(f"<a class='t' href='/admin/{c.name}/{a}'>{a}</a>" for a in c.actions)
        else:
            btns = "<span class='green'>steady-green</span>"
        info = f"<a class='t info' href='/admin/{c.name}/info'>&#9432; info</a>"
        rows.append(
            f"<tr><td class='n'>{c.name}</td><td>{badge}</td><td>{btns}</td><td>{info}</td></tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>{DELIVERY_HOSTNAME} — piggyback estate control</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:54rem; padding:0 1rem; }}
 h1 {{ font-weight:600; font-size:1.25rem; color:#9aa4af; }} h1 b {{ color:#d8dee4; }}
 p.sub {{ color:#9aa4af; }}
 table {{ width:100%; border-collapse:collapse; margin-top:1rem; }}
 td {{ padding:.5rem .4rem; border-bottom:1px solid #2a2e34; }}
 td.n {{ font-weight:600; }}
 .b {{ display:inline-block; padding:.15rem .6rem; border-radius:.3rem; color:#fff;
       font-weight:700; font-size:.8rem; letter-spacing:.04em; }}
 .t {{ display:inline-block; padding:.25rem .7rem; margin-right:.3rem; border-radius:.3rem;
       background:#333; color:#d8dee4; text-decoration:none; font-size:.85rem; }}
 .t:hover {{ background:#3a4350; }}
 .t.info {{ background:#243240; }} .t.info:hover {{ background:#2c3e50; }}
 .green {{ color:#2e7d32; font-size:.85rem; }}
 .foot {{ margin-top:1.5rem; color:#666; font-size:.83rem; }}
</style></head><body>
 <h1>piggyback estate — delivery shell <b>{DELIVERY_HOSTNAME}</b>
  <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <p class="sub">{len(CHILDREN)} hosts carried. This shell emits only a
  minimal agent section; everyone below arrives as piggyback blocks or
  per-host datasource files.</p>
 {_cascade_section() if CASCADE.steps else ""}
 <table>{"".join(rows)}</table>
 {_fleet_section(fleet)}
 <div class="foot">curl: /admin/&lt;host&gt;/&lt;degrade|break|heal&gt; · / (JSON status).
  Click <b>&#9432; info</b> on any host to see exactly which Checkmk services
  change in each state.</div>
</body></html>"""


def _fleet_section(fleet: list[FleetHost]) -> str:
    if not fleet:
        return ""
    by_role: dict[str, list[FleetHost]] = {}
    for h in fleet:
        by_role.setdefault(h.role or "other", []).append(h)
    blocks: list[str] = []
    for role in sorted(by_role):
        names = " ".join(
            f"<span class='fh' title='{h.descr}'>{h.name}</span>"
            for h in sorted(by_role[role], key=lambda x: x.name)
        )
        blocks.append(f"<div class='frole'><b>{role}</b> ({len(by_role[role])})<br>{names}</div>")
    return (
        f"<h2 style='color:#9aa4af;font-size:1.05rem;margin-top:1.6rem'>"
        f"steady-green fleet — {len(fleet)} hosts "
        f"<span style='color:#2e7d32;font-size:.85rem'>all healthy, "
        "no toggles</span></h2>"
        "<style>.fh{display:inline-block;background:#22313f;color:#9fc59f;"
        "border-radius:.25rem;padding:.06rem .4rem;margin:.12rem;"
        "font-size:.78rem}.frole{margin:.5rem 0;color:#9aa4af}</style>" + "".join(blocks)
    )


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "pb-delivery-ctl/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[http] {self.address_string()} {format % args}")

    def _send(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str) -> None:
        raw = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        if path == "/admin":
            return self._send_html(_overview_page())
        if path == "/admin/cascade" or path.startswith("/admin/cascade/"):
            sub = path[len("/admin/cascade") :].lstrip("/")
            if sub == "status":
                return self._send(200, CASCADE.status())
            if sub in ("", "start", "trigger"):
                CASCADE.start()
                print("[ctl] cascade -> START")
            elif sub in ("heal", "stop"):
                CASCADE.heal()
                print("[ctl] cascade -> HEAL")
            else:
                return self._send(404, {"error": f"unknown cascade action {sub!r}"})
            self.send_response(303)
            self.send_header("Location", "/admin")
            self.end_headers()
            return None
        if path.startswith("/admin/"):
            parts = path[len("/admin/") :].split("/")
            if len(parts) == 2 and parts[0] in _BY_NAME:
                child = _BY_NAME[parts[0]]
                if parts[1] == "info":
                    return self._send_html(_host_info_page(child))
                ok = child.toggle(parts[1])
                # stay on the host's info tab when toggled from there
                loc = f"/admin/{child.name}/info" if "back=info" in query else "/admin"
                self.send_response(303)
                self.send_header("Location", loc)
                self.end_headers()
                print(f"[ctl] {child.name} -> {parts[1]} ({'ok' if ok else 'FAILED'})")
                return None
        return self._send(
            200,
            {
                "delivery_host": DELIVERY_HOSTNAME,
                "domain": ESTATE_DOMAIN,
                "carried_hosts": [
                    {
                        "name": c.name,
                        "fqdn": c.fqdn,
                        "state": c.child_state(),
                        "actions": c.actions,
                        "parent": f"{c.parent}.{ESTATE_DOMAIN}" if c.parent else None,
                        **({"role": c.role, "os": c.os} if isinstance(c, FleetHost) else {}),
                    }
                    for c in CHILDREN
                ],
                "cascade": CASCADE.status(),
                "ui": "/admin",
            },
        )


def main() -> None:
    print(
        f"[boot] delivery shell={DELIVERY_HOSTNAME!r}  mode={DELIVERY_MODE}  "
        f"agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  hosts={len(CHILDREN)}"
    )

    # native (non-docker) runs: SIGTERM must reap the children too, not just ^C
    # — install BEFORE spawning so a kill during startup can't leak them
    def _sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        # only real Child processes are spawned here; fleet hosts (below) are
        # served by the FLEET_MGR process, not spawned per host
        for child in CHILDREN:
            if isinstance(child, Child):
                child.spawn()
        if FLEET_MGR:
            FLEET_MGR.spawn()
        # give children a moment to bind so the first poll already has everyone
        for child in CHILDREN:
            if isinstance(child, Child):
                child.wait_ready()
        if FLEET_MGR:
            fleet_hosts = [FleetHost(FLEET_MGR, info) for info in FLEET_MGR.roster()]
            CHILDREN.extend(fleet_hosts)
            _BY_NAME.update({h.name: h for h in fleet_hosts})
            print(
                f"[boot] fleet: carrying {len(fleet_hosts)} steady-green "
                f"bulk hosts (total {len(CHILDREN)})"
            )

        # arm the cross-host cascade scheduler (resumes a mid-run cascade if the
        # shell was restarted while one was armed)
        CASCADE.load()
        CASCADE.start_scheduler()
        if CASCADE.skipped:
            print(
                f"[cascade] {len(CASCADE.steps)} steps active, "
                f"{len(CASCADE.skipped)} skipped (host not in this selection)"
            )

        agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
        http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
        threading.Thread(target=agent.serve_forever, daemon=True).start()

        if DELIVERY_MODE == "datasource":
            # write the files ONCE before opening the panel, so that by the time
            # estate.py sees the panel and starts discovery the datasource
            # programs (cat) already have something to read
            n = write_agent_files()
            print(
                f"[boot] wrote {n}/{len(CHILDREN) + 1} agent files to "
                f"{AGENT_OUTPUT_DIR} (refresh every {AGENT_OUTPUT_INTERVAL:g}s)"
            )
            threading.Thread(target=agent_file_writer, daemon=True).start()
            print("[boot] In Checkmk: add each estate host as a Checkmk-agent host and")
            print(f"[boot] one 'Individual program call' rule: cat {AGENT_OUTPUT_DIR}/$HOSTNAME$")
        else:
            print("[boot] In Checkmk: add ONE TCP host for the delivery shell, then add")
            print("[boot] the estate hosts as *piggyback* hosts (no agent connection).")
        print(f"[boot] control panel: http://localhost:{HTTP_PORT}/admin")
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down — terminating children")
    finally:
        for child in CHILDREN:
            child.terminate()
        if FLEET_MGR:
            FLEET_MGR.terminate()


if __name__ == "__main__":
    main()
