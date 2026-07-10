#!/usr/bin/env python3
"""estate.py — the Meridian Retail demo estate in ONE command.

Brings up the whole fake company (agent-based servers as piggyback hosts,
SNMP network gear as stored walks) AND configures Checkmk for it — hosts,
rules, parent topology, BI pack, discovery, activation. Tear it all down
again with `down`.

    ./estate.py up --site                  # full estate on the newest dev site
    ./estate.py up --site v300 --scale minimal
    ./estate.py up --site --scale standard --replicas 5   # ~50-host estate
    ./estate.py up --site-url ... --mode cloud            # Checkmk Cloud (SaaS)
    ./estate.py replace --site             # tear down + fresh deploy in one go
    ./estate.py status
    ./estate.py break sw-access-01         # or heal/degrade, any host/device
    ./estate.py down --site

Hosts are sorted into a role-based subfolder tree (Applications, Databases,
Storage, Infrastructure, Windows servers, Network/…) under the estate root so
the demo reads like a real infrastructure.

Deployment modes (--mode, default self-hosted):

  self-hosted  we have access to the Checkmk site's filesystem, so the full
               estate is possible — including the SNMP layer, whose stored
               walk files are written straight into the site. Behaves as it
               always has.
  cloud        Checkmk Cloud (SaaS): no access to the site filesystem. Data
               can only arrive through the agent controller / relay, so the
               SNMP layer (which needs walk files on disk) is skipped and only
               the agent-based (piggyback) hosts are deployed.

Scales (--scale):

  minimal    the two classic demos: payment-api + db-postgres-01.
             No network layer, no SNMP. Smallest possible footprint.
  standard   the full agent estate: 10 server hosts and the
             Payments-platform BI pack (no network layer).
  full       standard + the network layer: SNMP gear (Catalyst switches,
             WAN router, UPS) simulated as stored SNMP walks, with the
             campus core as parent of every server.   [default]
  company    the researched ~300-host company estate: full + the steady-green
             server fleet (fleet/profiles.py: ~170 Linux/Windows hosts on 12
             KVM hypervisors) + ~110 SNMP devices replayed from anonymized
             real walks (snmp/walklib: switches, firewalls, load balancers,
             printers, UPS/PDUs, sensors, NAS/SAN, iDRACs). Self-hosted only
             (the SNMP layer needs the site filesystem).

  --replicas N multiplies every replicable host class N times (web-frontend-02,
  app-worker-03, ... plus N SNMP access switches) — same stories, bigger
  estate. Replicas run steady green; each incident stays unique.

Moving parts (all stdlib, see the directories):

  deploy/piggyback/   ONE container (docker or podman; or --runtime native:
                      one process) runs every agent host's serve.py and
                      delivers them as piggyback — agent :6559, panel :8099
  snmp/netsim.py      renders SNMP walk files into the site's snmpwalks dir
                      every 30 s — panel :8101 (runs as the site user;
                      estate.py uses sudo when needed)
  deploy/cmk_setup.py the REST-API engine (folder, hosts, rules, BI,
                      discovery, activation, teardown) — also usable alone

Requirements: docker or podman compose (unless --runtime native), a running
Checkmk site, and for SNMP a LOCAL site (walk files are written into it).
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "deploy"))
import cmk_setup  # noqa: E402  (deploy/cmk_setup.py — the REST engine)

PANEL = "http://localhost:8099"       # piggyback shell control panel
SNMP_PANEL = "http://localhost:8101"  # netsim control panel
PIDFILE_SHELL = "/var/tmp/cmk-demo-estate-shell.pid"
PIDFILE_NETSIM = "/var/tmp/cmk-demo-estate-netsim.pid"
# self-hosted: per-host agent files the site's "cat" datasource program reads.
# World-readable, NOT under the site — no sudo needed; docker bind-mounts it.
AGENT_OUTPUT_DIR = "/var/tmp/cmk-demo-agent-output"


def delivery_for(mode: str) -> str:
    """How estate hosts' agent data reaches Checkmk. Self-hosted uses the
    file + datasource-program path (better scaling); cloud has no filesystem
    access, so it stays on piggyback via the agent controller/relay."""
    return "datasource" if mode == "self-hosted" else "piggyback"

SCALES = {
    "minimal": {"hosts": "payment-api,db-postgres-01", "snmp": False,
                "fleet": False},
    "standard": {"hosts": "", "snmp": False, "fleet": False},  # "" = whole roster
    "full": {"hosts": "", "snmp": True, "fleet": False},
    # the researched ~300-host company: full + the steady-green fleet
    # (fleet/profiles.py, ~170 servers) + the SNMP walk-replay devices
    # (snmp/walklib, ~110 network/power/printer/storage devices)
    "company": {"hosts": "", "snmp": True, "fleet": True},
}

# site-user-readable copies for netsim (the repo usually lives under a 0750
# home the site user cannot read — see netsim_up)
WALKLIB_COPY = "/var/tmp/cmk-demo-walklib"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False, **kw)  # noqa: S603


def get_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for(url: str, what: str, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = get_json(url)
        if data is not None:
            return data
        time.sleep(1)
    sys.exit(f"ERROR: {what} did not come up within {timeout:g}s ({url})")


def wait_for_children(timeout: float = 60.0):
    """The panel binds its HTTP port as soon as the shell starts, but each
    carried host is an internal TCP child that needs a moment to produce its
    first state. cmk_setup.setup() dies if any child still has no state, so
    block until every carried host reports one (or time out and let cmk_setup
    surface the precise 'not up yet' message). Matters most when the container
    was just (re)created — e.g. every podman `up`, which force-recreates."""
    deadline = time.time() + timeout
    info = {}
    while time.time() < deadline:
        info = get_json(PANEL + "/") or {}
        hosts = info.get("carried_hosts") or []
        if hosts and all(h.get("state") is not None for h in hosts):
            return info
        time.sleep(1)
    return info


# --------------------------------------------------------------------------- #
#  Piggyback shell (container via compose, or native)
# --------------------------------------------------------------------------- #
# Container runtimes drive the shell through a compose provider. docker and
# podman share the same `<engine> compose ...` surface and read the same
# docker-compose.yml, so they differ only by the CLI name. "native" runs the
# shell as a plain background process (no engine).
COMPOSE_ENGINES = ("docker", "podman")


def compose_engine(runtime: str) -> str | None:
    """The container CLI backing a compose-based runtime, or None for native."""
    return runtime if runtime in COMPOSE_ENGINES else None


def shell_env(args: argparse.Namespace) -> dict[str, str]:
    return {
        "ESTATE_HOSTS": SCALES[args.scale]["hosts"],
        "ESTATE_REPLICAS": str(args.replicas),
        "ESTATE_FLEET": "1" if SCALES[args.scale]["fleet"] else "0",
        "DELIVERY_MODE": delivery_for(args.mode),
    }


def _ensure_output_dir() -> None:
    """datasource mode: the host dir must exist and be world-readable BEFORE
    docker mounts it (else docker root-creates it) so the site user can cat."""
    os.makedirs(AGENT_OUTPUT_DIR, exist_ok=True)
    try:
        os.chmod(AGENT_OUTPUT_DIR, 0o755)
    except OSError:
        pass


def shell_up(args: argparse.Namespace) -> None:
    datasource = delivery_for(args.mode) == "datasource"
    if datasource:
        _ensure_output_dir()
    engine = compose_engine(args.runtime)
    if engine:
        if not shutil.which(engine):
            sys.exit(f"ERROR: {engine} not found — use --runtime native")
        env = {**os.environ, **shell_env(args)}
        if datasource:
            # compose bind-mounts this host path to the container's /agent-output.
            # Rootless podman runs the shell as the caller's uid, docker as root;
            # either way serve.py writes the files 0644 so the site user can cat.
            env["ESTATE_AGENT_OUTPUT_DIR"] = AGENT_OUTPUT_DIR
        cmd = [engine, "compose", "up", "--build", "-d"]
        if engine == "podman":
            # docker compose diffs the running container against the desired
            # image+env and recreates it when they differ. podman-compose does
            # NOT: on a name clash it errors and silently `podman start`s the
            # existing container, so a rebuilt image or changed env (e.g.
            # DELIVERY_MODE) is ignored — a stale container is reused. Force the
            # recreate so `up` always reflects the current build + env. Counters
            # persist to the state file, so the restart is invisible mid-demo.
            cmd.append("--force-recreate")
        r = sh(cmd, cwd=os.path.join(REPO, "deploy", "piggyback"), env=env)
        if r.returncode != 0:
            sys.exit(f"ERROR: {engine} compose up failed")
        return
    # native: one background process runs the shell + all children
    if _pid_alive(PIDFILE_SHELL):
        print("  shell already running (native)")
        return
    env = {**os.environ, **shell_env(args),
           "AGENT_PORT": "6559", "HTTP_PORT": "8099"}
    if datasource:
        env["AGENT_OUTPUT_DIR"] = AGENT_OUTPUT_DIR
    log = open("/var/tmp/cmk-demo-estate-shell.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", os.path.join(REPO, "deploy", "piggyback", "serve.py")],
        env=env, stdout=log, stderr=log, start_new_session=True)
    with open(PIDFILE_SHELL, "w") as f:
        f.write(str(proc.pid))
    print(f"  shell started natively (pid {proc.pid}, "
          "log /var/tmp/cmk-demo-estate-shell.log)")


def shell_down(args: argparse.Namespace) -> None:
    # Best-effort compose-down under whatever engine is installed (the project
    # is cwd-scoped, so this only touches our own compose file). This means a
    # default `down` cleans up regardless of which --runtime brought the shell
    # up — no leftover container blocking the ports on the next `up`.
    if args.runtime != "native":
        for engine in COMPOSE_ENGINES:
            if shutil.which(engine):
                sh([engine, "compose", "down"],
                   cwd=os.path.join(REPO, "deploy", "piggyback"))
    _pid_kill(PIDFILE_SHELL, "shell")


def _pid_alive(pidfile: str) -> bool:
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _pid_kill(pidfile: str, what: str) -> None:
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 15)
        print(f"  stopped {what} (pid {pid})")
    except (OSError, ValueError):
        pass
    try:
        os.remove(pidfile)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  SNMP walk renderer (must write into the LOCAL site as the site user)
# --------------------------------------------------------------------------- #
NETSIM_LOG = "/var/tmp/cmk-demo-estate-netsim.log"
NETSIM_COPY = "/var/tmp/cmk-demo-netsim.py"


def netsim_up(args: argparse.Namespace, site_name: str | None) -> None:
    fleet = SCALES[args.scale]["fleet"] if hasattr(args, "scale") else False
    netsim = os.path.join(REPO, "snmp", "netsim.py")
    running = get_json(SNMP_PANEL + "/")
    if running is not None:
        # A running netsim can be reused ONLY if it still reflects the current
        # config. It must be stopped and restarted when:
        #  - the scale changed (fleet on/off) — "sw-dc-tor-*" is fleet-only, so
        #    its presence reveals the running fleet state;
        #  - --force was passed;
        #  - netsim.py changed since launch — the SNMP topology + values (incl.
        #    REPLAY_ROSTER parents) live in it, and the sudo path runs a copy at
        #    NETSIM_COPY, so a diff means the running netsim is stale.
        # Reusing a stale netsim is why SNMP parent edits didn't apply: it kept
        # serving the OLD parents, so the fingerprint matched and cmk_setup
        # short-circuited. (Agent-host parents already update: the shell is
        # rebuilt every up.)
        running_fleet = any(s.startswith("sw-dc-tor")
                            for s in running.get("devices", {}))
        force = bool(getattr(args, "force", False))
        stale = (os.path.exists(NETSIM_COPY)
                 and not filecmp.cmp(netsim, NETSIM_COPY, shallow=False))
        if running_fleet == fleet and not force and not stale:
            print("  netsim already running")
            return
        why = ("--force" if force else "scale change"
               if running_fleet != fleet else "netsim.py changed")
        print(f"  restarting netsim ({why})")
        netsim_down()
        deadline = time.time() + 15
        while time.time() < deadline and get_json(SNMP_PANEL + "/") is not None:
            time.sleep(0.5)
        if get_json(SNMP_PANEL + "/") is not None:
            sys.exit(f"ERROR: could not stop the stale netsim — stop it by hand "
                     f"and re-run:\n       curl {SNMP_PANEL}/admin/shutdown")
    if args.walks_dir:
        target = ["--walks-dir", args.walks_dir]
        run_as = None
    elif site_name:
        target = ["--site", site_name]
        run_as = site_name
    else:
        sys.exit("ERROR: SNMP needs a local site (--site) or --walks-dir")
    target += ["--http-port", "8101", "--access-switches", str(args.replicas)]
    walklib_src = os.path.join(REPO, "snmp", "walklib")
    if fleet:
        target += ["--fleet"]

    if run_as:
        import pwd
        try:
            pwd.getpwnam(run_as)
        except KeyError:
            sys.exit(f"ERROR: no user {run_as!r} on this machine — is that "
                     "an OMD site here? (SNMP needs a LOCAL site)")
        if os.access(f"/omd/sites/{run_as}/var/check_mk", os.W_OK):
            run_as = None  # already permitted (running as the site user)

    log = open(NETSIM_LOG, "wb")  # noqa: SIM115  (fresh log per attempt)
    if run_as and shutil.which("docker") and \
            subprocess.run(["sudo", "-n", "true"],  # noqa: S603
                           capture_output=True).returncode != 0:
        # No cached sudo credential but docker is available: run netsim in a
        # container AS THE SITE'S UID with the site's snmpwalks dir mounted —
        # same effect as sudo (docker group membership already grants it),
        # but non-interactive. estate.py down stops it via /admin/shutdown
        # (--rm makes the container vanish when netsim exits).
        # This trick is docker-specific: it needs the rootful daemon to write
        # the walk files as an *arbitrary* uid (the site user's). Rootless
        # podman maps --user through a subuid namespace and can't become the
        # site user, so with --runtime podman (and no docker) netsim falls
        # through to the sudo path below.
        import pwd
        pw = pwd.getpwnam(run_as)
        walks = f"/omd/sites/{run_as}/var/check_mk/snmpwalks"
        state_dir = "/var/tmp/cmk-demo-netsim-docker"
        os.makedirs(state_dir, exist_ok=True)
        os.chmod(state_dir, 0o777)  # noqa: S103  (container writes as site uid)
        cmd = ["docker", "run", "-d", "--rm", "--name", "cmk-demo-netsim",
               "--user", f"{pw.pw_uid}:{pw.pw_gid}",
               "-p", "127.0.0.1:8101:8101",
               "-v", f"{os.path.join(REPO, 'snmp')}:/netsim:ro",
               "-v", f"{walks}:/walks",
               "-v", f"{state_dir}:/state",
               "-e", "STATE_FILE=/state/netsim-state.json",
               "python:3.12-slim", "python3", "-u", "/netsim/netsim.py",
               "--walks-dir", "/walks",
               *[a for a in target if a != "--fleet"],
               *(["--fleet", "--walklib", "/netsim/walklib"] if fleet else [])]
        # strip the host-side --site pair; the container writes to /walks
        cmd = [a for i, a in enumerate(cmd)
               if not (a == "--site" or (i > 0 and cmd[i - 1] == "--site"))]
        print("  starting netsim in docker (site uid, no sudo needed)")
        subprocess.run(["docker", "rm", "-f", "cmk-demo-netsim"],  # noqa: S603
                       capture_output=True)
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
        if r.returncode != 0:
            sys.exit(f"ERROR: docker netsim failed — see {NETSIM_LOG}")
    elif run_as:
        # Three traps: (1) the repo usually lives under a 0750 home dir the
        # site user cannot read -> run a world-readable copy; (2) so may the
        # caller's interpreter (pyenv!) -> use the site's own python;
        # (3) sudo's cached credential is bound to this terminal
        # (tty_tickets), so a detached `sudo -n` can't use it -> let sudo
        # authenticate on the tty itself and background the daemon with -b.
        shutil.copyfile(netsim, NETSIM_COPY)
        os.chmod(NETSIM_COPY, 0o644)
        if fleet:
            # the walk-replay models must be readable by the site user too
            shutil.copytree(walklib_src, WALKLIB_COPY, dirs_exist_ok=True)
            os.chmod(WALKLIB_COPY, 0o755)
            for name in os.listdir(WALKLIB_COPY):
                os.chmod(os.path.join(WALKLIB_COPY, name), 0o644)
            target += ["--walklib", WALKLIB_COPY]
        site_python = f"/omd/sites/{run_as}/bin/python3"
        python = site_python if os.path.exists(site_python) else "/usr/bin/python3"
        print("  starting netsim as the site user (sudo may prompt)")
        r = subprocess.run(  # noqa: S603
            ["sudo", "-u", run_as, "-b", "--",
             python, "-u", NETSIM_COPY, *target],
            stdout=log, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            sys.exit("ERROR: sudo failed — run netsim yourself:\n"
                     f"       sudo -u {run_as} python3 {NETSIM_COPY} "
                     f"{' '.join(target)}")
    else:
        if fleet:
            target += ["--walklib", walklib_src]
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", netsim, *target],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        with open(PIDFILE_NETSIM, "w") as f:
            f.write(str(proc.pid))

    deadline = time.time() + 90
    while time.time() < deadline:
        if get_json(SNMP_PANEL + "/") is not None:
            print(f"  netsim started (panel {SNMP_PANEL}/admin)")
            return
        time.sleep(1)
    try:
        with open(NETSIM_LOG) as f:
            tail = "".join(f.readlines()[-8:])
    except OSError:
        tail = "(no log)"
    sys.exit(f"ERROR: netsim did not come up — last log lines "
             f"({NETSIM_LOG}):\n{tail}")


def netsim_down() -> None:
    # netsim runs as the site user, so we can't signal it — but its control
    # panel has a shutdown endpoint (localhost demo tool, by design)
    if get_json(SNMP_PANEL + "/") is not None:
        try:
            urllib.request.urlopen(  # noqa: S310
                SNMP_PANEL + "/admin/shutdown", timeout=5).read()
            print("  netsim stopped")
        except (urllib.error.URLError, OSError):
            print(f"  WARN: netsim still up but shutdown failed — "
                  f"stop it yourself (panel {SNMP_PANEL}/admin)")
    _pid_kill(PIDFILE_NETSIM, "netsim")


# --------------------------------------------------------------------------- #
#  Checkmk side (delegates to deploy/cmk_setup.py)
# --------------------------------------------------------------------------- #
def cmk_args(args: argparse.Namespace, extra: list[str]) -> list[str]:
    out = list(extra)
    if args.site_url:
        out += ["--site-url", args.site_url]
        if args.user:
            out += ["--user", args.user]
        if args.secret:
            out += ["--secret", args.secret]
    else:
        out += ["--site"] + ([args.site] if args.site not in (None, "auto") else [])
    if args.force_foreign:
        out += ["--force-foreign"]
    if getattr(args, "force", False):
        out += ["--force"]
    if getattr(args, "mode", None):
        out += ["--mode", args.mode]
    return out


def resolve_site_name(args: argparse.Namespace) -> str | None:
    """Local site name for netsim's walk target (None = remote/unknown)."""
    if args.site and args.site != "auto":
        return args.site
    if args.site == "auto":
        return cmk_setup.detect_dev_site()
    return None  # --site-url: possibly a remote site


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
def cmd_up(args: argparse.Namespace) -> None:
    snmp = SCALES[args.scale]["snmp"] and not args.no_snmp
    if args.mode == "cloud" and snmp:
        # cloud has no site filesystem to write stored walks into — the SNMP
        # layer simply isn't possible there, so drop it (agent hosts stay)
        print("  cloud mode: skipping the SNMP layer (no site-filesystem "
              "access for stored walks)")
        snmp = False
    print(f"== estate up: mode={args.mode} scale={args.scale} "
          f"replicas={args.replicas} snmp={'on' if snmp else 'off'} "
          f"runtime={args.runtime}")

    delivery = delivery_for(args.mode)
    print(f"* starting the delivery shell ({delivery})")
    shell_up(args)
    wait_for(PANEL + "/", "the delivery shell")
    info = wait_for_children()  # block until children report state (fresh container)
    print(f"  shell {info['delivery_host']} carrying "
          f"{len(info['carried_hosts'])} hosts")
    if delivery == "datasource":
        print(f"  agent files under {AGENT_OUTPUT_DIR} "
              "(read per host via a 'cat $HOSTNAME$' datasource rule)")

    site_name = resolve_site_name(args) if (snmp or args.site or args.site_url) else None

    if snmp:
        print("* starting the SNMP walk renderer")
        netsim_up(args, site_name)

    if args.no_checkmk:
        print("* --no-checkmk: skipping site setup")
        print(f"\nEstate running. Panels: {PANEL}/admin"
              + (f" and {SNMP_PANEL}/admin" if snmp else ""))
        return

    print("* configuring Checkmk (deploy/cmk_setup.py)")
    cmk_setup.main(cmk_args(args, [
        "--snmp", "on" if snmp else "off",
        "--agent-output-dir", AGENT_OUTPUT_DIR]))


def cmd_down(args: argparse.Namespace) -> None:
    print("== estate down")
    if not args.no_checkmk and (args.site or args.site_url):
        print("* removing the Checkmk objects")
        try:
            cmk_setup.main(cmk_args(args, ["--remove"]))
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(f"  (Checkmk teardown incomplete: {exc.code}) — "
                      "continuing with process shutdown")
    print("* stopping netsim")
    netsim_down()
    print("* stopping the piggyback shell")
    shell_down(args)
    print("Done.")


def cmd_replace(args: argparse.Namespace) -> None:
    """Full teardown + fresh deploy in one go (down, then up). Removing the
    estate deletes the shell host and its stored fingerprint, so the following
    up always runs the complete discovery/activation — no fast-path skip."""
    print("== estate replace (down + up)")
    cmd_down(args)
    print()
    cmd_up(args)


def _status_lines(items: list[tuple[str, str]]) -> None:
    """Per-host lines for small rosters; big (company-scale) rosters print
    the unhealthy hosts only, plus a summary count."""
    unhealthy = [(n, s) for n, s in items if s not in ("healthy", None, "n/a")]
    if len(items) <= 20:
        for name, state in items:
            mark = "" if state == "healthy" else "   <== not green"
            print(f"  {name:22} {state or 'n/a'}{mark}")
        return
    print(f"  {len(items) - len(unhealthy)} healthy"
          + (f", {len(unhealthy)} NOT green:" if unhealthy else ", all green"))
    for name, state in unhealthy:
        print(f"  {name:22} {state}   <== not green")


def cmd_status(_args: argparse.Namespace) -> None:
    shell = get_json(PANEL + "/")
    if shell:
        print(f"shell   UP  {shell['delivery_host']} "
              f"({len(shell['carried_hosts'])} hosts, panel {PANEL}/admin)")
        _status_lines([(h["name"], h.get("state")) for h in shell["carried_hosts"]])
    else:
        print(f"shell   DOWN ({PANEL})")
    net = get_json(SNMP_PANEL + "/")
    if net:
        print(f"netsim  UP  ({len(net['devices'])} devices, "
              f"panel {SNMP_PANEL}/admin, walks {net['walks_dir']})")
        _status_lines([(s, d.get("state")) for s, d in net["devices"].items()])
    else:
        print(f"netsim  DOWN ({SNMP_PANEL})")


def cmd_toggle(args: argparse.Namespace) -> None:
    action, host = args.action, args.host
    shell = get_json(PANEL + "/") or {"carried_hosts": []}
    net = get_json(SNMP_PANEL + "/") or {"devices": {}}
    if any(h["name"] == host for h in shell["carried_hosts"]):
        url = f"{PANEL}/admin/{host}/{action}"
    elif host in net["devices"]:
        url = f"{SNMP_PANEL}/admin/{host}/{action}"
    else:
        known = ([h["name"] for h in shell["carried_hosts"]]
                 + list(net["devices"]))
        sys.exit(f"ERROR: unknown host {host!r} — known: {', '.join(known)}")
    try:
        urllib.request.urlopen(url, timeout=10).read()  # noqa: S310
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"ERROR: toggle failed: {exc}")
    print(f"{host} -> {action}")


# --------------------------------------------------------------------------- #
def add_site_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--site", nargs="?", const="auto", metavar="NAME",
                   help="local dev site (no NAME = newest running v* site)")
    p.add_argument("--site-url", help="site base URL (non-dev sites)")
    p.add_argument("--user", help="site user (with --site-url)")
    p.add_argument("--secret", default=os.environ.get("CMK_AUTOMATION_SECRET"),
                   help="user secret (with --site-url)")
    p.add_argument("--force-foreign", action="store_true",
                   help="activate even with other users' pending changes")
    p.add_argument("--no-checkmk", action="store_true",
                   help="only start/stop the simulators, skip the site setup")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.split("\n")[3:]))
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_up_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--mode", choices=("self-hosted", "cloud"),
                            default="self-hosted",
                            help="self-hosted = full access to the site "
                                 "filesystem (SNMP layer + datasource files); "
                                 "cloud = Checkmk Cloud/SaaS, piggyback agent "
                                 "data only, SNMP layer skipped")
        parser.add_argument("--scale", choices=sorted(SCALES), default="full")
        parser.add_argument("--replicas", type=int, default=1, metavar="N",
                            help="stamp out every replicable host class N times")
        parser.add_argument("--runtime", choices=("docker", "podman", "native"),
                            default="docker",
                            help="how to run the delivery shell: docker/podman "
                                 "(container via `<engine> compose`) or native "
                                 "(a plain background process, no engine)")
        parser.add_argument("--no-snmp", action="store_true",
                            help="skip the SNMP layer even at --scale full")
        parser.add_argument("--force", action="store_true",
                            help="reconfigure Checkmk even if nothing changed "
                                 "(re-run discovery + activation); by default an "
                                 "unchanged re-run short-circuits in ~1s")
        parser.add_argument("--walks-dir",
                            help="write SNMP walks here instead of into the site "
                                 "(no sudo needed; for inspection only)")
        add_site_args(parser)

    up = sub.add_parser("up", help="start simulators + configure Checkmk")
    add_up_args(up)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="teardown Checkmk objects + stop everything")
    down.add_argument("--runtime", choices=("docker", "podman", "native"),
                      default="docker")
    add_site_args(down)
    down.set_defaults(func=cmd_down)

    replace = sub.add_parser(
        "replace", aliases=["redeploy"],
        help="full teardown + fresh deploy (down then up)")
    add_up_args(replace)
    replace.set_defaults(func=cmd_replace)

    st = sub.add_parser("status", help="what is running, which host is in which state")
    st.set_defaults(func=cmd_status)

    for action in ("break", "degrade", "heal"):
        t = sub.add_parser(action, help=f"{action} a host or SNMP device")
        t.add_argument("host")
        t.set_defaults(func=cmd_toggle, action=action)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
