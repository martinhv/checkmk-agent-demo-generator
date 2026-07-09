#!/usr/bin/env python3
"""estate.py — the Meridian Retail demo estate in ONE command.

Brings up the whole fake company (agent-based servers as piggyback hosts,
SNMP network gear as stored walks) AND configures Checkmk for it — hosts,
rules, parent topology, BI pack, discovery, activation. Tear it all down
again with `down`.

    ./estate.py up --site                  # full estate on the newest dev site
    ./estate.py up --site v300 --scale minimal
    ./estate.py up --site --scale standard --replicas 5   # ~50-host estate
    ./estate.py status
    ./estate.py break sw-access-01         # or heal/degrade, any host/device
    ./estate.py down --site

Scales (--scale):

  minimal    the two classic demos: payment-api + db-postgres-01.
             No network layer, no SNMP. Smallest possible footprint.
  standard   the full agent estate: 12 hosts incl. the Linux network
             devices, parent topology and the Payments-platform BI pack.
  full       standard + the SNMP network gear (Catalyst switches, WAN
             router, UPS) simulated as stored SNMP walks.   [default]

  --replicas N multiplies every replicable host class N times (web-frontend-02,
  app-worker-03, ... plus N SNMP access switches) — same stories, bigger
  estate. Replicas run steady green; each incident stays unique.

Moving parts (all stdlib, see the directories):

  deploy/piggyback/   ONE container (or --runtime native: one process) runs
                      every agent host's serve.py and delivers them as
                      piggyback — agent :6559, panel :8099
  snmp/netsim.py      renders SNMP walk files into the site's snmpwalks dir
                      every 30 s — panel :8101 (runs as the site user;
                      estate.py uses sudo when needed)
  deploy/cmk_setup.py the REST-API engine (folder, hosts, rules, BI,
                      discovery, activation, teardown) — also usable alone

Requirements: docker compose (unless --runtime native), a running Checkmk
site, and for SNMP a LOCAL site (walk files are written into it).
"""
from __future__ import annotations

import argparse
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

SCALES = {
    "minimal": {"hosts": "payment-api,db-postgres-01", "snmp": False},
    "standard": {"hosts": "", "snmp": False},   # "" = the whole roster
    "full": {"hosts": "", "snmp": True},
}


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


# --------------------------------------------------------------------------- #
#  Piggyback shell (docker or native)
# --------------------------------------------------------------------------- #
def shell_env(args: argparse.Namespace) -> dict[str, str]:
    return {
        "ESTATE_HOSTS": SCALES[args.scale]["hosts"],
        "ESTATE_REPLICAS": str(args.replicas),
    }


def shell_up(args: argparse.Namespace) -> None:
    if args.runtime == "docker":
        if not shutil.which("docker"):
            sys.exit("ERROR: docker not found — use --runtime native")
        env = {**os.environ, **shell_env(args)}
        r = sh(["docker", "compose", "up", "--build", "-d"],
               cwd=os.path.join(REPO, "deploy", "piggyback"), env=env)
        if r.returncode != 0:
            sys.exit("ERROR: docker compose up failed")
        return
    # native: one background process runs the shell + all children
    if _pid_alive(PIDFILE_SHELL):
        print("  shell already running (native)")
        return
    env = {**os.environ, **shell_env(args),
           "AGENT_PORT": "6559", "HTTP_PORT": "8099"}
    log = open("/var/tmp/cmk-demo-estate-shell.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", os.path.join(REPO, "deploy", "piggyback", "serve.py")],
        env=env, stdout=log, stderr=log, start_new_session=True)
    with open(PIDFILE_SHELL, "w") as f:
        f.write(str(proc.pid))
    print(f"  shell started natively (pid {proc.pid}, "
          "log /var/tmp/cmk-demo-estate-shell.log)")


def shell_down(args: argparse.Namespace) -> None:
    if args.runtime == "docker" and shutil.which("docker"):
        sh(["docker", "compose", "down"],
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
    if get_json(SNMP_PANEL + "/") is not None:
        print("  netsim already running")
        return
    netsim = os.path.join(REPO, "snmp", "netsim.py")
    if args.walks_dir:
        target = ["--walks-dir", args.walks_dir]
        run_as = None
    elif site_name:
        target = ["--site", site_name]
        run_as = site_name
    else:
        sys.exit("ERROR: SNMP needs a local site (--site) or --walks-dir")
    target += ["--http-port", "8101", "--access-switches", str(args.replicas)]

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
    if run_as:
        # Three traps: (1) the repo usually lives under a 0750 home dir the
        # site user cannot read -> run a world-readable copy; (2) so may the
        # caller's interpreter (pyenv!) -> use the site's own python;
        # (3) sudo's cached credential is bound to this terminal
        # (tty_tickets), so a detached `sudo -n` can't use it -> let sudo
        # authenticate on the tty itself and background the daemon with -b.
        shutil.copyfile(netsim, NETSIM_COPY)
        os.chmod(NETSIM_COPY, 0o644)
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
    print(f"== estate up: scale={args.scale} replicas={args.replicas} "
          f"snmp={'on' if snmp else 'off'} runtime={args.runtime}")

    print("* starting the piggyback shell")
    shell_up(args)
    info = wait_for(PANEL + "/", "the delivery shell")
    print(f"  shell {info['delivery_host']} carrying "
          f"{len(info['carried_hosts'])} hosts")

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
    cmk_setup.main(cmk_args(args, ["--snmp", "on" if snmp else "off"]))


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


def cmd_status(_args: argparse.Namespace) -> None:
    shell = get_json(PANEL + "/")
    if shell:
        print(f"shell   UP  {shell['delivery_host']} "
              f"({len(shell['carried_hosts'])} hosts, panel {PANEL}/admin)")
        for h in shell["carried_hosts"]:
            state = h.get("state") or "n/a"
            mark = "" if state == "healthy" else "   <== not green"
            print(f"  {h['name']:22} {state}{mark}")
    else:
        print(f"shell   DOWN ({PANEL})")
    net = get_json(SNMP_PANEL + "/")
    if net:
        print(f"netsim  UP  ({len(net['devices'])} devices, "
              f"panel {SNMP_PANEL}/admin, walks {net['walks_dir']})")
        for short, d in net["devices"].items():
            state = d.get("state") or "n/a"
            mark = "" if state == "healthy" else "   <== not green"
            print(f"  {short:22} {state}{mark}")
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

    up = sub.add_parser("up", help="start simulators + configure Checkmk")
    up.add_argument("--scale", choices=sorted(SCALES), default="full")
    up.add_argument("--replicas", type=int, default=1, metavar="N",
                    help="stamp out every replicable host class N times")
    up.add_argument("--runtime", choices=("docker", "native"), default="docker",
                    help="how to run the piggyback shell")
    up.add_argument("--no-snmp", action="store_true",
                    help="skip the SNMP layer even at --scale full")
    up.add_argument("--walks-dir",
                    help="write SNMP walks here instead of into the site "
                         "(no sudo needed; for inspection only)")
    add_site_args(up)
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="teardown Checkmk objects + stop everything")
    down.add_argument("--runtime", choices=("docker", "native"), default="docker")
    add_site_args(down)
    down.set_defaults(func=cmd_down)

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
