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

Config via env:
  DELIVERY_HOSTNAME  name of the shell host        (default: cmk-demo-gateway)
  AGENT_PORT         TCP port Checkmk polls         (default: 6556)
  HTTP_PORT          combined /admin control port   (default: 8080)
  ESTATE_HOSTS       comma list of host names to carry (default: all)
  ESTATE_REPLICAS    replica multiplier for replicable classes (default: 1)
  AGENT_VERSION      version in the delivery header (default: 2.5.0-...)
  CHILD_AGENT_BASE   internal child agent port base (default: 7600)
  CHILD_HTTP_BASE    internal child admin port base (default: auto after
                     the agent range, so big estates can't collide)
"""
from __future__ import annotations

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import StreamRequestHandler, ThreadingTCPServer

# Estate DNS domain — every host shows up in Checkmk as <short>.<ESTATE_DOMAIN>
# (FQDN). The short name stays the internal label (panel, ports, selection).
ESTATE_DOMAIN = os.environ.get("ESTATE_DOMAIN", "corp.meridian-retail.com")
DELIVERY_HOSTNAME = os.environ.get(
    "DELIVERY_HOSTNAME", f"cmk-demo-gateway.{ESTATE_DOMAIN}")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
CHILD_AGENT_BASE = int(os.environ.get("CHILD_AGENT_BASE", "7600"))

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOSTS_DIR = os.path.join(REPO_ROOT, "hosts")
START = time.time()

# Estate roster: (hostname, directory under hosts/, toggle actions, extra
# child env, parent, replicable).
# `actions` drives the combined control panel; [] = steady-green background.
# `parent` is the short name of the upstream network device — exposed as an
# FQDN in the panel JSON and applied as the Checkmk "parents" attribute by
# deploy/cmk_setup.py. The network layer is the SNMP-simulated gear
# (snmp/netsim.py): every server hangs off the campus core switch
# sw-core-01, which is only applied when the SNMP devices are deployed too.
# `replicable` marks classes that ESTATE_REPLICAS stamps out N times
# (web-frontend-02, -03, ...) — replicas run steady green; incident stories
# stay unique to the original (low noise, one root cause).
_REGISTRY = [
    ("web-frontend-01", "web-frontend-01", [], {"START_STATE": "healthy"},
     "sw-core-01", True),
    ("payment-api", "payment-api", ["break", "heal"],
     {"START_BROKEN": "0"}, "sw-core-01", False),
    ("app-worker-01", "app-worker-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
    ("app-redis-01", "app-redis-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
    ("db-postgres-01", "db-postgres-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", False),
    ("db-postgres-02", "db-postgres-02", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
    ("mail-relay-01", "mail-relay-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
    ("fileserver-01", "fileserver-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
    ("backup-01", "backup-01", [], {"START_STATE": "healthy"}, "sw-core-01",
     False),
    ("win-dc-01", "win-dc-01", ["degrade", "break", "heal"],
     {"START_STATE": "healthy"}, "sw-core-01", True),
]


def _replica_name(base: str, n: int) -> str:
    """web-frontend-01 -> web-frontend-02, ... (suffix numbering continues)."""
    stem = base[:-3] if base.endswith("-01") else base
    return f"{stem}-{n:02d}"


class Child:
    def __init__(self, idx: int, name: str, directory: str,
                 actions: list[str], extra_env: dict[str, str],
                 parent: str | None) -> None:
        self.name = name
        self.directory = directory
        self.actions = actions
        self.extra_env = extra_env
        self.parent = parent
        self.agent_port = CHILD_AGENT_BASE + idx
        self.http_port = CHILD_HTTP_BASE + idx
        self.proc: subprocess.Popen | None = None

    @property
    def fqdn(self) -> str:
        # the name Checkmk sees (piggyback target + the child's own Hostname:)
        return f"{self.name}.{ESTATE_DOMAIN}"

    @property
    def script(self) -> str:
        return os.path.join(HOSTS_DIR, self.directory, "serve.py")

    def spawn(self) -> None:
        env = dict(os.environ)
        env.update({
            "CMK_HOSTNAME": self.fqdn,
            "AGENT_PORT": str(self.agent_port),
            "HTTP_PORT": str(self.http_port),
            "STATE_FILE": f"/var/tmp/cmk-demo-pb-{self.name}.json",
        })
        # only inject our defaults if the operator hasn't overridden them
        for k, v in self.extra_env.items():
            env.setdefault(k, v)
        if not os.path.exists(self.script):
            print(f"[pb] WARN: {self.script} missing — skipping {self.name}")
            return
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", self.script], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[pb] spawned {self.name:16} agent=127.0.0.1:{self.agent_port} "
              f"admin=127.0.0.1:{self.http_port}")

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
            with socket.create_connection(("127.0.0.1", self.agent_port),
                                          timeout=timeout) as s:
                s.settimeout(timeout)
                chunks = []
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
                        f"http://127.0.0.1:{self.http_port}{ep}", timeout=2) as r:
                    state = json.loads(r.read()).get("state")
                if state:
                    return state
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return None

    def fetch_meta(self) -> dict | None:
        """Read the child's state-change info (STATE_META) for the info tab."""
        try:
            with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{self.http_port}/admin/meta", timeout=2) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def toggle(self, action: str) -> bool:
        try:
            with urllib.request.urlopen(  # noqa: S310
                    f"http://127.0.0.1:{self.http_port}/admin/{action}",
                    timeout=3) as r:
                r.read()
                return True
        except (urllib.error.URLError, OSError):
            return False

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


_selected = os.environ.get("ESTATE_HOSTS", "").strip()
_wanted = {h.strip() for h in _selected.split(",") if h.strip()} if _selected else None
_replicas = max(1, int(os.environ.get("ESTATE_REPLICAS", "1") or "1"))

# roster: selected classes, each replicable class stamped out _replicas times.
# Replicas force a healthy start and carry no toggle actions — incidents stay
# unique to the original (low noise, one root cause).
_roster: list[tuple[str, str, list, dict, str | None]] = []
for name, directory, actions, extra, parent, replicable in _REGISTRY:
    if _wanted is not None and name not in _wanted:
        continue
    _roster.append((name, directory, actions, extra, parent))
    if replicable:
        for n in range(2, _replicas + 1):
            green = {**extra, "START_STATE": "healthy", "START_BROKEN": "0"}
            _roster.append((_replica_name(name, n), directory, [], green, parent))

# keep the internal admin ports clear of the agent range however big the
# estate gets (agent ports occupy CHILD_AGENT_BASE .. +len(_roster))
CHILD_HTTP_BASE = int(os.environ.get(
    "CHILD_HTTP_BASE", str(CHILD_AGENT_BASE + max(100, len(_roster) + 10))))

CHILDREN: list[Child] = [
    Child(i, name, directory, actions, extra, parent)
    for i, (name, directory, actions, extra, parent) in enumerate(_roster)
]
_BY_NAME = {c.name: c for c in CHILDREN}


# --------------------------------------------------------------------------- #
#  Delivery agent output: minimal own section + piggyback blocks
# --------------------------------------------------------------------------- #
def _delivery_minimal() -> str:
    now = int(time.time())
    uptime = int(time.time() - START) + 3 * 86400
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 320 * 86400))
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
        json.dumps({
            "version": AGENT_VERSION, "agent_socket_operational": True,
            "ip_allowlist": [], "allow_legacy_pull": False,
            "connections": [{
                "site_id": "monitoring/prod", "receiver_port": 8000,
                "uuid": "0e5a2c11-9d44-4a7b-bf01-7c2e9a3d6e10",
                "local": {"connection_mode": "pull-agent", "cert_info": {
                    "issuer": "Site 'prod' local CA",
                    "from": "Tue, 03 Jun 2025 09:12:44 +0000", "to": cert_to}},
                "remote": "remote_query_disabled"}]}, separators=(",", ":")),
        # uptime so the shell has a couple of real services of its own
        "<<<uptime>>>",
        f"{uptime}.00 {int(uptime * 3.0)}.00",
    ]
    return "\n".join(lines) + "\n"


def build_delivery_output() -> bytes:
    out = bytearray(_delivery_minimal().encode("utf-8"))
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


def _host_info_page(child: "Child") -> str:
    """Per-host 'what happens on a state change' tab — mirrors the per-demo
    control screens, but rendered by the delivery shell from the child's
    /admin/meta so the estate is driven from one place."""
    meta = child.fetch_meta()
    if not meta:
        return ("<!doctype html><meta charset='utf-8'>"
                "<body style='background:#1a1d21;color:#d8dee4;"
                "font-family:system-ui,sans-serif;margin:2rem auto;max-width:40rem'>"
                f"<p>{child.name}: state info not available yet (child still starting). "
                "<a style='color:#6cf' href='/admin'>&larr; back to estate</a></p>")
    states = meta.get("states", {})
    a2s = meta.get("action_to_state", {})
    cur = meta.get("state")
    cur_meta = states.get(cur, {})
    # one card per reachable state (action -> target); steady-green hosts have
    # no actions, so just show their single state with no button.
    pairs = list(a2s.items()) if a2s else [(None, name) for name in states]
    cards = []
    for action, target in pairs:
        tmeta = states.get(target, {})
        color = tmeta.get("color", "#666")
        current = target == cur
        effects = "".join(f"<li>{e}</li>" for e in tmeta.get("effects", []))
        if current:
            btn = "<span class='btn current'>current state</span>"
        elif action:
            btn = (f"<a class='btn' href='/admin/{child.name}/{action}?back=info' "
                   f"style='background:{color}'>&rarr; {action}</a>")
        else:
            btn = ""
        cards.append(
            f"<div class='card{' active' if current else ''}' "
            f"style='border-color:{color}'>"
            f"<h2 style='color:{color}'>{tmeta.get('label', str(target).upper())}</h2>"
            f"<p class='tag'>{tmeta.get('tagline', '')}</p><ul>{effects}</ul>{btn}</div>")

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
 <div class="state">{cur_meta.get('label', str(cur).upper())}</div>
 <div class="since">in this state for <b>{since}</b> — {cur_meta.get('tagline', '')}</div>
 <div class="cards">{''.join(cards)}</div>
 <div class="foot">Each card is a target state and the Checkmk services that change when you
  switch to it. Buttons toggle this host and return here. Piggyback host
  carried by <b>{DELIVERY_HOSTNAME}</b>.</div>
</body></html>"""


def _overview_page() -> str:
    rows = []
    colors = {"healthy": "#2e7d32", "degraded": "#f9a825", "broken": "#c62828",
              None: "#666"}
    for c in CHILDREN:
        state = c.child_state()
        badge = (f"<span class='b' style='background:{colors.get(state, '#666')}'>"
                 f"{(state or 'n/a').upper()}</span>")
        if c.actions:
            btns = " ".join(
                f"<a class='t' href='/admin/{c.name}/{a}'>{a}</a>" for a in c.actions)
        else:
            btns = "<span class='green'>steady-green</span>"
        info = f"<a class='t info' href='/admin/{c.name}/info'>&#9432; info</a>"
        rows.append(f"<tr><td class='n'>{c.name}</td><td>{badge}</td>"
                    f"<td>{btns}</td><td>{info}</td></tr>")
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
 <p class="sub">{len(CHILDREN)} hosts carried as piggyback. This shell emits only a
  minimal agent section; everyone below arrives wrapped in <code>&lt;&lt;&lt;&lt;host&gt;&gt;&gt;&gt;</code>.</p>
 <table>{''.join(rows)}</table>
 <div class="foot">curl: /admin/&lt;host&gt;/&lt;degrade|break|heal&gt; · / (JSON status).
  Click <b>&#9432; info</b> on any host to see exactly which Checkmk services change in each state.</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "pb-delivery-ctl/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: dict) -> None:
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
        if path.startswith("/admin/"):
            parts = path[len("/admin/"):].split("/")
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
        return self._send(200, {
            "delivery_host": DELIVERY_HOSTNAME,
            "domain": ESTATE_DOMAIN,
            "carried_hosts": [
                {"name": c.name, "fqdn": c.fqdn, "state": c.child_state(),
                 "actions": c.actions,
                 "parent": f"{c.parent}.{ESTATE_DOMAIN}" if c.parent else None}
                for c in CHILDREN],
            "ui": "/admin",
        })


def main() -> None:
    print(f"[boot] piggyback delivery shell={DELIVERY_HOSTNAME!r}  "
          f"agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  hosts={len(CHILDREN)}")

    # native (non-docker) runs: SIGTERM must reap the children too, not just ^C
    # — install BEFORE spawning so a kill during startup can't leak them
    def _sigterm(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    try:
        for child in CHILDREN:
            child.spawn()
        # give children a moment to bind so the first poll already has everyone
        for child in CHILDREN:
            child.wait_ready()

        agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
        http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
        threading.Thread(target=agent.serve_forever, daemon=True).start()
        print(f"[boot] control panel: http://localhost:{HTTP_PORT}/admin")
        print("[boot] In Checkmk: add ONE TCP host for the delivery shell, then add the")
        print("[boot] estate hosts as *piggyback* hosts (no agent connection needed).")
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down — terminating children")
    finally:
        for child in CHILDREN:
            child.terminate()


if __name__ == "__main__":
    main()
