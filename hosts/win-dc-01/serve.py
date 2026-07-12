#!/usr/bin/env python3
r"""Meridian Retail demo host: win-dc-01 — Windows Server 2022 AD domain controller.

The one Windows box in the estate (see ../FLEET.md). Same plaintext-TCP trick
as the Linux demos (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration), but the agent payload is the
**Windows** format (verified against a real 2.3 Windows agent dump:
check_mk/tests/gui_e2e/data/windows-2.3.0p10): `<<<df:sep(9)>>>`,
`<<<wmi_cpuload:sep(124)>>>`, the Windows `<<<mem>>>` keys, `<<<services>>>`,
`<<<ps:sep(9)>>>`, `<<<systemtime>>>`.

Incident (ONE root cause, low noise): the in-house **Meridian Backup Agent**
service crashes, so its job that trims `C:\` (old NTDS logs, the Windows Update
download cache) stops running -> the **system drive C: fills up**. Symptom:
Filesystem C:/ crosses the default magnitude levels (80 % WARN / 90 % CRIT).
The AI fuses the stopped service + the steep C: fill slope + the growing cache
file into "the backup/cleanup service died; C: is filling — restart it and
clear SoftwareDistribution".

Three states:
  healthy   C: ~53 % used, every service running. all green.
  degraded  MeridianBackupAgent stopped (root cause); C: climbs 53 -> ~84 %
            (Filesystem C:/ WARN). The stopped service shows in <<<services>>>
            (CRIT only if you add the "Windows Services" monitoring rule for
            it — documented). Trigger ~20 min before showtime.
  broken    C: > 90 % and still growing live -> Filesystem C:/ CRIT.

Windows checks have no rate-based agent sections here (we omit winperf
counters), so unlike the Linux demos there is no monotonic-counter machinery —
df usage and the CPU queue are gauges. Stdlib only.

Config via env: CMK_HOSTNAME, AGENT_PORT, HTTP_PORT, START_STATE,
  AGENT_VERSION, AUTO_BREAK_AFTER_MIN (default 20), LEAK_FILL_MIN (default 18,
  the C:-fill window while degraded), BREAK_RAMP_MIN (default 4), STATE_FILE.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import StreamRequestHandler, ThreadingTCPServer

HOSTNAME = os.environ.get("CMK_HOSTNAME", "win-dc-01.corp.meridian-retail.com")
# Win32_ComputerSystem.Name is the NetBIOS short name (uppercase), not the FQDN.
COMPUTERNAME = HOSTNAME.split(".")[0].upper()
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "20"))
LEAK_FILL_MIN = float(os.environ.get("LEAK_FILL_MIN", "18"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "4"))

START = time.time()
UPTIME_OFFSET = 41 * 86400  # a DC stays up a long time (~41 days)

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
_degraded_since: float | None = None if _state == "healthy" else START
_broken_since: float | None = None if _state != "broken" else START
_state_since: float = START


def get_state() -> str:
    with _state_lock:
        return _state


def set_state(value: str) -> None:
    global _state, _degraded_since, _broken_since, _state_since
    with _state_lock:
        if value != _state:
            _state_since = time.time()
        _state = value
        if value == "healthy":
            _degraded_since = None
        elif _degraded_since is None:
            _degraded_since = time.time()
        if value == "broken":
            if _broken_since is None:
                _broken_since = time.time()
        else:
            _broken_since = None
    save_state()


def state_since_seconds() -> float:
    with _state_lock:
        return time.time() - _state_since


def degraded_seconds() -> float:
    with _state_lock:
        return 0.0 if _degraded_since is None else time.time() - _degraded_since


def broken_seconds() -> float:
    with _state_lock:
        return 0.0 if _broken_since is None else time.time() - _broken_since


def break_ramp(frac: float = 1.0) -> float:
    bs = broken_seconds()
    if bs <= 0:
        return 0.0
    if BREAK_RAMP_MIN <= 0:
        return 1.0
    return min(1.0, bs / (BREAK_RAMP_MIN * 60.0 * frac))


def _lerp(healthy: float, broken: float, r: float) -> float:
    return healthy + (broken - healthy) * r


def pressure() -> float:
    """0 (healthy) .. 1 (C: full). Fills to 0.78 while degraded (C: WARN),
    broken pushes 0.78 -> 1.0 over the ramp (C: CRIT)."""
    ds = degraded_seconds()
    if ds <= 0:
        deg = 0.0
    elif LEAK_FILL_MIN <= 0:
        deg = 1.0
    else:
        deg = min(1.0, ds / (LEAK_FILL_MIN * 60.0))
    p = 0.78 * deg
    if broken_seconds() > 0:
        p = max(p, 0.78 + 0.22 * break_ramp(1.0))
    return max(0.0, min(1.0, p))


def disk_dying() -> bool:
    """The backup/cleanup service is down once we leave healthy."""
    return get_state() in ("degraded", "broken")


# --------------------------------------------------------------------------- #
#  Autocorrelated gauge (no counters needed — Windows here has no rate checks)
# --------------------------------------------------------------------------- #
class _Wobble:
    def __init__(self, phase: float = 0.0, period: float = 1200.0) -> None:
        self.phase = phase
        self.omega = 2.0 * math.pi / period
        self.noise = 0.0

    def step(self, now: float) -> float:
        harm = (
            0.60 * math.sin(self.omega * now + self.phase)
            + 0.28 * math.sin(self.omega * 2.7 * now + self.phase * 1.7)
            + 0.18 * math.sin(self.omega * 0.41 * now + self.phase * 0.5)
        )
        self.noise = max(-1.5, min(1.5, self.noise * 0.9 + random.gauss(0.0, 0.25)))
        return max(-1.0, min(1.0, (harm + 0.45 * self.noise) / 1.8))


_GAUGES: dict[str, _Wobble] = {}
_GAUGE_LOCK = threading.Lock()


def gauge(
    name: str,
    base: float,
    *,
    amp_abs: float | None = None,
    amp_frac: float | None = None,
    phase: float = 0.0,
    period: float = 1200.0,
) -> float:
    with _GAUGE_LOCK:
        w = _GAUGES.get(name)
        if w is None:
            w = _GAUGES[name] = _Wobble(phase, period)
        d = w.step(time.time())
    if amp_abs is not None:
        return base + amp_abs * d
    return base * (1.0 + (amp_frac or 0.0) * d)


def c_drive_used_kb(now: float) -> int:
    """Used kB on C: (size ~120 GiB). Healthy ~53 %; once the cleanup service
    dies the WU cache + NTDS logs pile up, filling toward >90 %. Pure function
    of pressure + a slow secular term, with a small wander — continuous across
    re-polls and restarts."""
    p = pressure()
    base = _lerp(66_700_000, 116_800_000, p)  # 53 % -> ~92.8 %
    secular = min(1_500_000, (now - START + UPTIME_OFFSET) * 0.02)
    return int(base + secular + gauge("c.used", 0, amp_abs=90_000, period=1500))


def cache_file_bytes() -> int:
    """The offending SoftwareDistribution cache file — grows live while the
    cleanup service is down."""
    if not disk_dying():
        return 1_180_000_000
    return 1_180_000_000 + int(degraded_seconds() * 7_400_000)  # ~7 MB/s


# --------------------------------------------------------------------------- #
#  Agent output (Windows format)
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    pressure()

    lines: list[str] = []
    a = lines.append
    TAB = "\t"

    # ---- check_mk header (Windows; mirrors the real 2.x Windows agent) ----- #
    a("<<<check_mk>>>")
    a(f"Version: {AGENT_VERSION}")
    a("BuildDate: Apr  3 2026")
    a("AgentOS: windows")
    a(f"Hostname: {HOSTNAME}")
    a("Architecture: 64bit")
    a("OSName: Microsoft Windows Server 2022 Datacenter")
    a("OSVersion: 10.0.20348")
    a("OSType: windows")
    a(time.strftime("Time: %Y-%m-%dT%H:%M:%S+0000", time.gmtime(now)))
    a("WorkingDirectory: C:\\Windows\\system32")
    a("ConfigFile: C:\\Program Files (x86)\\checkmk\\service\\check_mk.yml")
    a("LocalConfigFile: C:\\ProgramData\\checkmk\\agent\\check_mk.user.yml")
    a("AgentDirectory: C:\\Program Files (x86)\\checkmk\\service")
    a("PluginsDirectory: C:\\ProgramData\\checkmk\\agent\\plugins")
    a("StateDirectory: C:\\ProgramData\\checkmk\\agent\\state")
    a("ConfigDirectory: C:\\ProgramData\\checkmk\\agent\\config")
    a("TempDirectory: C:\\ProgramData\\checkmk\\agent\\tmp")
    a("LogDirectory: C:\\ProgramData\\checkmk\\agent\\log")
    a("SpoolDirectory: C:\\ProgramData\\checkmk\\agent\\spool")
    a("LocalDirectory: C:\\ProgramData\\checkmk\\agent\\local")
    a("OnlyFrom:")

    # ---- controller status: pretend TLS-registered (allow_legacy_pull=false +
    #      a registered pull connection with a cert ~325 days out) so the
    #      Check_MK Agent service shows no "TLS is not activated" warning. ---- #
    a("<<<cmk_agent_ctl_status:sep(0)>>>")
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now + 325 * 86400))
    a(
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
                        "uuid": "c47b1d92-7a30-4e1f-9c8a-1b6d4f2e8a55",
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
        )
    )

    # ---- CPU load via WMI processor-queue (green; a DC idles) -------------- #
    a("<<<wmi_cpuload:sep(124)>>>")
    qlen = max(0, round(gauge("cpu.qlen", 1.0, amp_abs=1.4, phase=0.3, period=420)))
    a("[system_perf]")
    a("Name|ProcessorQueueLength|Timestamp_PerfTime|Frequency_PerfTime|WMIStatus")
    a(f"|{qlen}|{int(uptime * 10_000_000)}|10000000|OK")
    a("[computer_system]")
    a("Name|NumberOfLogicalProcessors|NumberOfProcessors|WMIStatus")
    a(f"{COMPUTERNAME}|4|1|OK")

    # ---- uptime (seconds) -------------------------------------------------- #
    a("<<<uptime>>>")
    a(str(uptime))

    # ---- memory: Windows keys. RAM + pagefile both green (not a mem story).  #
    a("<<<mem>>>")
    mem_total = 16_768_000
    mem_free = int(gauge("mem.free", 9_100_000, amp_frac=0.03, phase=0.6, period=1500))
    page_total = 24_117_248
    page_free = int(gauge("page.free", 18_900_000, amp_frac=0.02, phase=1.4, period=1700))
    a(f"MemTotal:      {mem_total} kB")
    a(f"MemFree:       {mem_free} kB")
    a("SwapTotal:     7340032 kB")
    a("SwapFree:      6815744 kB")
    a(f"PageTotal:     {page_total} kB")
    a(f"PageFree:      {page_free} kB")
    a("VirtualTotal:  137438953344 kB")
    a("VirtualFree:   137431814144 kB")

    # ---- fileinfo: the offending WU cache file (grows live; needs a File-info
    #      rule to alert — corroboration the AI can quote). sep(124). -------- #
    a("<<<fileinfo:sep(124)>>>")
    a(str(now))
    a(f"C:\\Windows\\SoftwareDistribution\\Download\\cache.cab|{cache_file_bytes()}|{now - 90}")
    a(f"C:\\Windows\\NTDS\\ntds.dit|{4_731_273_216}|{now - 1800}")

    # ---- df: the incident. sep(9) -> TAB-separated; NTFS reformatting in the
    #      parser. C: fills (default magnitude 80/90), D: data stays green. --- #
    a("<<<df:sep(9)>>>")
    c_size = 125_827_068
    c_used = c_drive_used_kb(time.time())
    c_avail = c_size - c_used
    c_pct = round(c_used / c_size * 100)
    a(TAB.join(["C:\\", "NTFS", str(c_size), str(c_used), str(c_avail), f"{c_pct}%", "C:\\"]))
    d_size = 419_430_400
    d_used = int(gauge("d.used", 171_000_000, amp_abs=400_000, period=1800))
    a(
        TAB.join(
            [
                "D:\\",
                "NTFS",
                str(d_size),
                str(d_used),
                str(d_size - d_used),
                f"{round(d_used / d_size * 100)}%",
                "D:\\",
            ]
        )
    )

    # ---- services: AD/DC services. The Meridian Backup Agent (the cleanup
    #      service) is stopped once we leave healthy -> the root cause. Note:
    #      individual Windows services alert only with a "Windows Services"
    #      monitoring rule (no default); the C: fill is the guaranteed red. --- #
    backup_state = "stopped/auto" if disk_dying() else "running/auto"
    a("<<<services>>>")
    services = [
        ("MeridianBackupAgent", backup_state, "Meridian Backup & Disk Cleanup Agent"),
        ("NTDS", "running/auto", "Active Directory Domain Services"),
        ("DNS", "running/auto", "DNS Server"),
        ("Netlogon", "running/auto", "Netlogon"),
        ("Kdc", "running/auto", "Kerberos Key Distribution Center"),
        ("ADWS", "running/auto", "Active Directory Web Services"),
        ("DFSR", "running/auto", "DFS Replication"),
        ("W32Time", "running/auto", "Windows Time"),
        ("Dnscache", "running/auto", "DNS Client"),
        ("LanmanServer", "running/auto", "Server"),
        ("LanmanWorkstation", "running/auto", "Workstation"),
        ("EventLog", "running/auto", "Windows Event Log"),
        ("Schedule", "running/auto", "Task Scheduler"),
        ("gpsvc", "running/auto", "Group Policy Client"),
        ("Dhcp", "running/auto", "DHCP Client"),
        ("RpcSs", "running/auto", "Remote Procedure Call (RPC)"),
        ("Power", "running/auto", "Power"),
        ("Winmgmt", "running/auto", "Windows Management Instrumentation"),
        ("BFE", "running/auto", "Base Filtering Engine"),
        ("mpssvc", "running/auto", "Windows Defender Firewall"),
        ("WinDefend", "running/auto", "Microsoft Defender Antivirus Service"),
        ("CheckMkService", "running/auto", "Checkmk Agent"),
        ("TermService", "running/auto", "Remote Desktop Services"),
        ("ProfSvc", "running/auto", "User Profile Service"),
        ("SamSs", "running/auto", "Security Accounts Manager"),
        ("LSM", "running/auto", "Local Session Manager"),
        ("AJRouter", "stopped/demand", "AllJoyn Router Service"),
        ("ALG", "stopped/demand", "Application Layer Gateway Service"),
        ("AppMgmt", "stopped/demand", "Application Management"),
        ("BTAGService", "stopped/demand", "Bluetooth Audio Gateway Service"),
        ("MapsBroker", "stopped/auto", "Downloaded Maps Manager"),
        ("TabletInputService", "stopped/demand", "Touch Keyboard and Handwriting Panel"),
        ("WbioSrvc", "stopped/demand", "Windows Biometric Service"),
        ("XblAuthManager", "stopped/demand", "Xbox Live Auth Manager"),
        ("wuauserv", "running/demand", "Windows Update"),
    ]
    for name, status, descr in services:
        a(f"{name} {status} {descr}")

    # ---- deployed agent plugins (Windows provenance) ----------------------- #
    a("<<<checkmk_agent_plugins_win:sep(0)>>>")
    a("pluginsdir C:\\ProgramData\\checkmk\\agent\\plugins")
    a("localdir C:\\ProgramData\\checkmk\\agent\\local")
    a(
        f'C:\\ProgramData\\checkmk\\agent\\plugins\\cmk_update_agent.checkmk.py:CMK_VERSION = "{AGENT_VERSION}"'
    )
    a(f'C:\\ProgramData\\checkmk\\agent\\plugins\\mk_inventory.vbs:CMK_VERSION = "{AGENT_VERSION}"')

    # ---- processes (Windows ps:sep(9)). lsass holds the AD database on a DC;
    #      format: (user,VSZkb,WSkb,0,pid,handle?,usertime,kerneltime,handles,
    #      threads,uptime)\tname.exe — values are static-ish, ps has no default
    #      alert. ----------------------------------------------------------- #
    a("<<<ps:sep(9)>>>")
    proc_named = [
        ("SYSTEM", 0, 8, 0, 2, "System Idle Process"),
        ("SYSTEM", 560, 140, 4, 113, "System"),
        ("SYSTEM", 1648, 412, 276, 2, "smss.exe"),
        ("SYSTEM", 23968, 5992, 588, 6, "services.exe"),
        ("\\\\NT AUTHORITY\\SYSTEM", 232800, 58200, 608, 52, "lsass.exe"),
        ("\\\\NT AUTHORITY\\SYSTEM", 204000, 51000, 940, 50, "svchost.exe"),
        ("\\\\NT AUTHORITY\\NETWORK SERVICE", 124800, 31200, 1820, 24, "dns.exe"),
        (
            "\\\\NT AUTHORITY\\SYSTEM",
            113600,
            28400,
            2140,
            18,
            "Microsoft.ActiveDirectory.WebServices.exe",
        ),
        ("\\\\NT AUTHORITY\\SYSTEM", 79200, 19800, 2360, 12, "dfsrs.exe"),
        ("\\\\NT AUTHORITY\\LOCAL SERVICE", 58400, 14600, 1280, 9, "svchost.exe"),
        ("\\\\NT AUTHORITY\\SYSTEM", 88400, 22100, 3120, 14, "MsMpEng.exe"),
        ("\\\\NT AUTHORITY\\SYSTEM", 39200, 9800, 3480, 7, "check_mk_agent.exe"),
    ]
    for usr, vsz, ws, pid, threads, name in proc_named:
        a(
            f"({usr},{vsz},{ws},0,{pid},{threads * 2},{pid * 156250},{pid * 312500},"
            f"{threads * 30},{threads},{uptime}){TAB}{name}"
        )

    # ---- system time (compared to the monitoring server's clock) ----------- #
    a("<<<systemtime>>>")
    a(str(now))

    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence (state + START for uptime continuity; no counters)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-win-dc-state.json")


def save_state() -> None:
    if not STATE_FILE:
        return
    with _state_lock:
        data = {
            "version": 1,
            "start": START,
            "state": _state,
            "degraded_since": _degraded_since,
            "broken_since": _broken_since,
            "state_since": _state_since,
        }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"[state] save failed: {exc}")


def load_state() -> None:
    global START, _state, _degraded_since, _broken_since, _state_since
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[state] load failed ({exc}) — starting fresh")
        return
    with _state_lock:
        START = data["start"]
        _state = data.get("state", _state)
        _degraded_since = data.get("degraded_since")
        _broken_since = data.get("broken_since")
        _state_since = data.get("state_since", time.time())
    print(f"[state] restored: state={_state!r}, uptime continuous")


# --------------------------------------------------------------------------- #
#  Servers + admin UI
# --------------------------------------------------------------------------- #
class AgentHandler(StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.wfile.write(build_agent_output(get_state()))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        save_state()


class AgentServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


STATE_META = {
    "healthy": {
        "color": "#2e7d32",
        "label": "HEALTHY",
        "tagline": "All green. C: ~53 % used, every service running.",
        "effects": [
            "every service OK — the starting picture",
            "Filesystem C:/ ~53 % (default magnitude levels 80/90)",
            "MeridianBackupAgent running — nightly C: cleanup happening",
        ],
    },
    "degraded": {
        "color": "#f9a825",
        "label": "DEGRADED",
        "tagline": "The backup/cleanup service crashed; C: starts filling. "
        + (
            f"Auto-escalates after {AUTO_BREAK_AFTER_MIN:g} min."
            if AUTO_BREAK_AFTER_MIN > 0
            else ""
        ),
        "effects": [
            "MeridianBackupAgent -> stopped (the root cause) — shows in <<<services>>> "
            "(CRIT only if you add a 'Windows Services' rule for it; documented)",
            "Filesystem C:/ climbs 53 -> ~84 % -> WARN; the WU cache file grows live",
            "everything else stays green — one root cause",
        ],
    },
    "broken": {
        "color": "#c62828",
        "label": "BROKEN",
        "tagline": "C: is nearly full. "
        + (f"Crosses 90 % over ~{BREAK_RAMP_MIN:g} min." if BREAK_RAMP_MIN > 0 else "Instant."),
        "effects": [
            "Filesystem C:/ > 90 % and still GROWING live -> CRIT (the headline)",
            "MeridianBackupAgent still stopped; SoftwareDistribution cache file large",
            "the AI fuses the stopped cleanup service + the steep C: slope + the growing "
            "cache file: 'restart the agent and clear the WU cache; C: full in <N> h'",
        ],
    },
}
ACTION_TO_STATE = {"heal": "healthy", "degrade": "degraded", "break": "broken"}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _admin_page() -> str:
    state = get_state()
    meta = STATE_META[state]
    c_used = c_drive_used_kb(time.time())
    c_pct = round(c_used / 125_827_068 * 100)
    extras = [f"C: at {c_pct}% used"]
    if disk_dying():
        extras.append(
            f"MeridianBackupAgent stopped for {_fmt_duration(degraded_seconds())} — "
            f"WU cache file {cache_file_bytes() // 1_000_000} MB and growing"
        )
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"C:/ crosses CRIT (auto) in {_fmt_duration(left)}")
    extra_html = "".join(f"<div class='extra'>{e}</div>" for e in extras)

    cards = []
    for action, target in ACTION_TO_STATE.items():
        tmeta = STATE_META[target]
        current = target == state
        effects = "".join(f"<li>{e}</li>" for e in tmeta["effects"])
        btn = (
            "<span class='btn current'>current state</span>"
            if current
            else f"<a class='btn' href='/admin/{action}?ui=1' "
            f"style='background:{tmeta['color']}'>&rarr; {action}</a>"
        )
        cards.append(
            f"<div class='card{' active' if current else ''}' "
            f"style='border-color:{tmeta['color']}'>"
            f"<h2 style='color:{tmeta['color']}'>{tmeta['label']}</h2>"
            f"<p class='tag'>{tmeta['tagline']}</p><ul>{effects}</ul>{btn}</div>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>{HOSTNAME} — demo control</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:72rem; padding:0 1rem; }}
 h1 {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }} h1 b {{ color:#d8dee4; }}
 .state {{ display:inline-block; padding:.4rem 1.1rem; border-radius:.4rem; color:#fff;
          font-weight:700; font-size:1.6rem; letter-spacing:.05em; background:{meta["color"]}; }}
 .since {{ color:#9aa4af; margin:.6rem 0 0; }} .extra {{ color:#f9a825; margin-top:.3rem; }}
 .cards {{ display:flex; gap:1rem; margin-top:2rem; flex-wrap:wrap; }}
 .card {{ flex:1 1 20rem; border:2px solid #333; border-radius:.6rem; padding:1rem 1.2rem;
         background:#22262b; opacity:.85; }}
 .card.active {{ opacity:1; background:#262b31; box-shadow:0 0 14px rgba(255,255,255,.06); }}
 .card h2 {{ margin:.1rem 0 .4rem; font-size:1.1rem; }}
 .card .tag {{ color:#9aa4af; min-height:2.6rem; margin:.2rem 0 .4rem; }}
 .card ul {{ padding-left:1.2rem; margin:.4rem 0 1rem; }}
 .card li {{ margin:.25rem 0; font-size:.92rem; }}
 .btn {{ display:inline-block; padding:.45rem 1.1rem; border-radius:.4rem; color:#fff;
        text-decoration:none; font-weight:600; }}
 .btn.current {{ background:#444; color:#aaa; cursor:default; }}
 .foot {{ margin-top:2rem; color:#666; font-size:.85rem; }}
</style></head><body>
 <h1>demo control — <b>{HOSTNAME}</b> <span style="color:#555">(Windows Server 2022 · auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta["label"]}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b> — {meta["tagline"]}</div>
 {extra_html}
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "windc-demo-ctl/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[http] {self.address_string()} {format % args}")

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
            return self._send_html(_admin_page())
        if path == "/admin/meta":
            return self._send(
                200,
                {
                    "state": get_state(),
                    "in_state_for_s": round(state_since_seconds(), 1),
                    "action_to_state": ACTION_TO_STATE,
                    "states": STATE_META,
                },
            )
        if path.startswith("/admin/") and (action := path[len("/admin/") :]) in ACTION_TO_STATE:
            target = ACTION_TO_STATE[action]
            set_state(target)
            print(f"[ctl] -> {target.upper()}")
            if "ui=1" in query:
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.end_headers()
                return None
            return self._send(200, {"state": target})
        state = get_state()
        auto_break_in = (
            round(max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds()))
            if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0
            else None
        )
        return self._send(
            200,
            {
                "state": state,
                "in_state_for_s": round(state_since_seconds(), 1),
                "c_drive_used_pct": round(c_drive_used_kb(time.time()) / 125_827_068 * 100, 1),
                "backup_agent": "stopped" if disk_dying() else "running",
                "auto_break_in_s": auto_break_in,
                "toggles": ["/admin/degrade", "/admin/break", "/admin/heal"],
                "ui": "/admin",
            },
        )


def _auto_break_watchdog() -> None:
    while True:
        time.sleep(5)
        if get_state() == "degraded" and state_since_seconds() >= AUTO_BREAK_AFTER_MIN * 60:
            set_state("broken")
            print(f"[ctl] -> BROKEN (auto: C: filling for {AUTO_BREAK_AFTER_MIN:g} min)")


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(f"[boot] auto-escalation: degraded -> broken after {AUTO_BREAK_AFTER_MIN:g} min")
    print(
        f"[boot] host={HOSTNAME!r} (Windows)  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
        f"start_state={get_state()}"
    )
    print(f"[boot] control UI:   http://localhost:{HTTP_PORT}/admin")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
