#!/usr/bin/env python3
"""Meridian Retail fleet — ONE process serving every steady-green bulk host.

Where hosts/*/serve.py are hand-crafted incident demos (one process each),
this serves the ~140-host steady-green bulk of the 300-host estate from a
single process: fleet/profiles.py declares host classes (role, hardware,
processes, services), this file turns each instance into a full fake agent
output — Linux payloads built like hosts/web-frontend-01 (the steady-green
reference), Windows payloads like hosts/win-dc-01's healthy state. All the
section-parity rules from CLAUDE.md apply (full check_mk header, pretend TLS
registration, both lnx_if variants, full /proc/meminfo, timesyncd with dynamic
timestamps, apt sentinel, ~30 systemd units, monotonic wobbled counters,
autocorrelated gauges, restart-persisted counter state).

Per-instance variation is deterministic (seeded by the host name): uptime,
load, memory, MAC, disk serials, agent UUID all differ between svc-catalog-01
and svc-catalog-02, and survive restarts.

Everything is synthesized — no recorded customer data is replayed here.

The delivery shell (deploy/piggyback/serve.py) spawns this as ONE child and
fetches per-host payloads over HTTP:

  GET /                  roster JSON (name, fqdn, os, role, parent, descr)
  GET /agent/<short>     that host's full agent output (text/plain)

Config via env:
  ESTATE_DOMAIN   DNS domain for the FQDNs (default corp.meridian-retail.com)
  HTTP_PORT       HTTP port (default 8102)
  AGENT_VERSION   reported agent version
  STATE_FILE      counter persistence (default /var/tmp/cmk-demo-fleet-state.json)
  FLEET_CLASSES   comma list of class prefixes to serve (default: all)
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import profiles

DOMAIN = os.environ.get("ESTATE_DOMAIN", "corp.meridian-retail.com")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8102"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-fleet-state.json")

START = time.time()


# --------------------------------------------------------------------------- #
#  Physics — identical machinery to hosts/db-postgres-01 (the reference):
#  incommensurate long-period harmonics + AR(1) noise clamped to [-1, 1];
#  counters integrate the current rate (monotonic, restart-persisted).
# --------------------------------------------------------------------------- #
class _Wobble:
    def __init__(self, phase: float = 0.0, period: float = 1200.0) -> None:
        self.phase = phase
        self.omega = 2.0 * math.pi / period
        self.noise = 0.0

    def step(self, now: float) -> float:
        harm = (0.60 * math.sin(self.omega * now + self.phase)
                + 0.28 * math.sin(self.omega * 2.7 * now + self.phase * 1.7)
                + 0.18 * math.sin(self.omega * 0.41 * now + self.phase * 0.5))
        self.noise = max(-1.5, min(1.5, self.noise * 0.9 + random.gauss(0.0, 0.25)))
        return max(-1.0, min(1.0, (harm + 0.45 * self.noise) / 1.8))


_GAUGES: dict[str, _Wobble] = {}
_GAUGE_LOCK = threading.Lock()


def gauge(name: str, base: float, *, amp_abs: float | None = None,
          amp_frac: float | None = None, phase: float = 0.0,
          period: float = 1200.0) -> float:
    with _GAUGE_LOCK:
        w = _GAUGES.get(name)
        if w is None:
            w = _GAUGES[name] = _Wobble(phase, period)
        d = w.step(time.time())
    if amp_abs is not None:
        return base + amp_abs * d
    return base * (1.0 + (amp_frac or 0.0) * d)


_ALL_COUNTERS: dict[str, "Counter"] = {}


class Counter:
    def __init__(self, name: str, phase: float = 0.0, amp: float = 0.30,
                 period: float = 1200.0, start: float = 0.0) -> None:
        self.acc = start
        self.last = time.time()
        self.amp = amp
        self.wob = _Wobble(phase, period)
        self.lock = threading.Lock()
        _ALL_COUNTERS[name] = self

    def sample(self, rate_per_s: float) -> int:
        now = time.time()
        with self.lock:
            dt = max(0.0, now - self.last)
            inst = rate_per_s * (1.0 + self.amp * self.wob.step(now))
            self.acc += inst * dt
            self.last = now
            return int(self.acc)


# --------------------------------------------------------------------------- #
#  Shared building blocks
# --------------------------------------------------------------------------- #
def _ctl_status_json(uuid: str) -> str:
    """Pretend TLS registration (allow_legacy_pull false + a pull connection
    with a live cert ~330 days out) — see CLAUDE.md '_check_transport'."""
    now = int(time.time())
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 330 * 86400))
    return json.dumps({
        "version": AGENT_VERSION, "agent_socket_operational": True,
        "ip_allowlist": [], "allow_legacy_pull": False,
        "connections": [{
            "site_id": "monitoring/prod", "receiver_port": 8000,
            "uuid": uuid,
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Tue, 03 Jun 2025 09:12:44 +0000", "to": cert_to}},
            "remote": "remote_query_disabled"}]}, separators=(",", ":"))


def _smart_json(dev: str, model: str, serial: str, hours: int, temp: int) -> str:
    return json.dumps({
        "device": {"name": dev, "type": "sat", "protocol": "ATA"},
        "model_name": model, "serial_number": serial,
        "smart_status": {"passed": True},
        "power_on_time": {"hours": hours},
        "temperature": {"current": temp},
        "ata_smart_attributes": {"table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100,
             "thresh": 10, "raw": {"value": 0}},
            {"id": 12, "name": "Power_Cycle_Count", "value": 100,
             "thresh": 0, "raw": {"value": 14}},
            {"id": 187, "name": "Reported_Uncorrect", "value": 100,
             "thresh": 0, "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100,
             "thresh": 0, "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200,
             "thresh": 0, "raw": {"value": 0}},
            {"id": 177, "name": "Wear_Leveling_Count", "value": 96,
             "thresh": 5, "raw": {"value": 88}},
            {"id": 179, "name": "Used_Rsvd_Blk_Cnt_Tot", "value": 100,
             "thresh": 10, "raw": {"value": 0}},
        ]},
    }, separators=(",", ":"))


# Base OS daemons every Ubuntu box runs (ps_lnx rows; role procs are appended).
_LNX_BASE_PROCS = [
    ("init.scope", "root", 168_000, 12_800, 1, "/sbin/init"),
    ("system.slice/systemd-journald.service", "root", 59_100, 18_600, 401,
     "/usr/lib/systemd/systemd-journald"),
    ("system.slice/systemd-udevd.service", "root", 25_900, 7_800, 437,
     "/usr/lib/systemd/systemd-udevd"),
    ("system.slice/systemd-resolved.service", "systemd-resolve", 26_600, 13_000,
     488, "/usr/lib/systemd/systemd-resolved"),
    ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_200, 7_500,
     502, "/usr/lib/systemd/systemd-timesyncd"),
    ("system.slice/systemd-networkd.service", "systemd-network", 22_100, 8_400,
     495, "/usr/lib/systemd/systemd-networkd"),
    ("system.slice/systemd-logind.service", "root", 18_400, 8_100, 509,
     "/usr/lib/systemd/systemd-logind"),
    ("system.slice/dbus.service", "messagebus", 10_200, 5_000, 514,
     "@dbus-daemon --system --address=systemd:"),
    ("system.slice/rsyslog.service", "syslog", 222_400, 6_600, 611,
     "/usr/sbin/rsyslogd -n -iNONE"),
    ("system.slice/ssh.service", "root", 15_400, 8_900, 689,
     "sshd: /usr/sbin/sshd -D [listener]"),
    ("system.slice/cron.service", "root", 11_500, 2_400, 704,
     "/usr/sbin/cron -f -P"),
    ("system.slice/irqbalance.service", "root", 20_600, 4_100, 610,
     "/usr/sbin/irqbalance --foreground"),
    ("system.slice/polkit.service", "root", 236_400, 9_200, 617,
     "/usr/lib/polkit-1/polkitd --no-debug"),
    ("system.slice/snapd.service", "root", 1_248_000, 34_500, 655,
     "/usr/lib/snapd/snapd"),
    ("system.slice/unattended-upgrades.service", "root", 108_000, 21_000, 720,
     "/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal"),
]

# Base systemd units (all green; ~29 like a real Ubuntu 24.04 server).
_LNX_BASE_UNITS = [
    ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
    ("cron.service", "active", "running", "Regular background program processing daemon"),
    ("dbus.service", "active", "running", "D-Bus System Message Bus"),
    ("getty@tty1.service", "active", "running", "Getty on tty1"),
    ("irqbalance.service", "active", "running", "irqbalance daemon"),
    ("multipathd.service", "active", "running", "Device-Mapper Multipath Device Controller"),
    ("networkd-dispatcher.service", "active", "running", "Dispatcher daemon for systemd-networkd"),
    ("polkit.service", "active", "running", "Authorization Manager"),
    ("rsyslog.service", "active", "running", "System Logging Service"),
    ("snapd.service", "active", "running", "Snap Daemon"),
    ("systemd-journald.service", "active", "running", "Journal Service"),
    ("systemd-logind.service", "active", "running", "User Login Management"),
    ("systemd-networkd.service", "active", "running", "Network Configuration"),
    ("systemd-resolved.service", "active", "running", "Network Name Resolution"),
    ("systemd-timesyncd.service", "active", "running", "Network Time Synchronization"),
    ("systemd-udevd.service", "active", "running", "Rule-based Manager for Device Events and Files"),
    ("udisks2.service", "active", "running", "Disk Manager"),
    ("unattended-upgrades.service", "active", "running", "Unattended Upgrades Shutdown"),
    ("user@1000.service", "active", "running", "User Manager for UID 1000"),
    ("apparmor.service", "active", "exited", "Load AppArmor profiles"),
    ("blk-availability.service", "active", "exited", "Availability of block devices"),
    ("console-setup.service", "active", "exited", "Set console font and keymap"),
    ("finalrd.service", "active", "exited", "Create final runtime dir for shutdown pivot root"),
    ("keyboard-setup.service", "active", "exited", "Set the console keyboard layout"),
    ("lvm2-monitor.service", "active", "exited", "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or progress polling"),
    ("setvtrgb.service", "active", "exited", "Set console scheme"),
    ("snapd.seeded.service", "active", "exited", "Wait until snapd is fully seeded"),
    ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
]

# Base Windows services (win-dc-01's list minus the AD roles; role services
# from the profile are inserted after the first entry).
_WIN_BASE_SERVICES = [
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

_WIN_BASE_PROCS = [
    ("SYSTEM", 0, 8, 0, 2, "System Idle Process"),
    ("SYSTEM", 560, 140, 4, 113, "System"),
    ("SYSTEM", 1648, 412, 276, 2, "smss.exe"),
    ("SYSTEM", 23968, 5992, 588, 6, "services.exe"),
    ("\\\\NT AUTHORITY\\SYSTEM", 86_000, 21_500, 608, 12, "lsass.exe"),
    ("\\\\NT AUTHORITY\\SYSTEM", 204_000, 51_000, 940, 50, "svchost.exe"),
    ("\\\\NT AUTHORITY\\LOCAL SERVICE", 58_400, 14_600, 1280, 9, "svchost.exe"),
    ("\\\\NT AUTHORITY\\SYSTEM", 88_400, 22_100, 3120, 14, "MsMpEng.exe"),
    ("\\\\NT AUTHORITY\\SYSTEM", 39_200, 9_800, 3480, 7, "check_mk_agent.exe"),
]

_DISK_MODELS = [
    "Samsung SSD 870 EVO 500GB", "Samsung SSD 883 DCT 960GB",
    "INTEL SSDSC2KB480G8", "Micron 5300 MTFDDAK480TDS",
    "WDC WDS500G1R0A-68A4W0", "KINGSTON SEDC500M480G",
]


def _mac(rnd: random.Random) -> str:
    return "02:42:" + ":".join(f"{rnd.randrange(256):02x}" for _ in range(4))


def _uuid(rnd: random.Random) -> str:
    h = "".join(rnd.choice("0123456789abcdef") for _ in range(32))
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _serial(rnd: random.Random, prefix: str = "S5") -> str:
    return prefix + "".join(rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789")
                            for _ in range(12))


# --------------------------------------------------------------------------- #
#  Linux fleet host
# --------------------------------------------------------------------------- #
class LinuxHost:
    def __init__(self, short: str, spec: dict, guests: list["Host"] | None = None) -> None:
        self.short = short
        self.spec = spec
        self.os = "linux"
        # upstream network device (short name), assigned by expand_roster():
        # the hypervisor for a VM, an access switch for physical iron.
        self.net_parent: str | None = None
        self.guests = guests or []          # kvm hypervisors: their VMs
        rnd = random.Random(f"{short}:fleet-v1")
        lo, hi = spec.get("uptime_days", (15, 120))
        self.uptime_offset = rnd.uniform(lo, hi) * 86400
        self.jit = rnd.uniform(0.88, 1.12)  # per-instance scale on load/net
        self.phase = rnd.uniform(0, 6.28)
        self.mac = _mac(rnd)
        self.uuid = _uuid(rnd)
        self.ncpu = spec.get("ncpu", 4)
        self.mem_total = spec.get("mem_mb", 8192) * 1024      # kB
        self.load1 = spec.get("load1", 0.3) * self.jit
        rx, tx = spec.get("net_mbs", (1.0, 1.0))
        self.rx_bps = rx * 1e6 * self.jit
        self.tx_bps = tx * 1e6 * self.jit
        model, size_gb = spec.get("disk") or (rnd.choice(_DISK_MODELS), 480)
        self.disk_model, self.disk_gb = model, size_gb
        self.disk_serial = _serial(rnd)
        self.disk_hours = int(rnd.uniform(4_000, 32_000))
        # filesystems: "/" always; extra mounts ride a second disk (sdb)
        self.extra_fs = []
        for mount, gib, used in spec.get("fs", []):
            self.extra_fs.append((mount, gib * 1_048_576,
                                  min(0.72, used + rnd.uniform(-0.04, 0.04))))
        self.data_serial = _serial(rnd, "S6")
        self.root_kb = spec.get("root_gb", 40) * 1_048_576
        self.root_used_frac = rnd.uniform(0.26, 0.38)
        anon_f, cached_f, shmem_f = spec.get("mem_profile", (0.15, 0.35, 0.03))
        self.anon_f, self.cached_f, self.shmem_f = anon_f, cached_f, shmem_f

        # cpu tick split from the load (util = busy fraction of all cores)
        total_ticks = self.ncpu * 100.0
        util = max(0.02, min(0.80, self.load1 / self.ncpu * 0.75))
        c = lambda n, r, ph=0.0, amp=0.30: Counter(  # noqa: E731
            f"{short}.{n}", phase=self.phase + ph, amp=amp,
            start=r * self.uptime_offset)
        self.c_user = c("cpu.user", total_ticks * util * 0.72, 0.3)
        self.c_system = c("cpu.system", total_ticks * util * 0.22, 1.1)
        self.c_iowait = c("cpu.iowait", total_ticks * 0.015, 3.0)
        self.r_user = total_ticks * util * 0.72
        self.r_system = total_ticks * util * 0.22
        self.r_iowait = total_ticks * 0.015
        self.r_idle = max(5.0, total_ticks - self.r_user - self.r_system
                          - self.r_iowait)
        self.c_idle = c("cpu.idle", self.r_idle, 2.4, 0.02)
        self.c_ctxt = c("kernel.ctxt", 900 * self.ncpu, 4.0)
        self.c_proc = c("kernel.processes", 4, 4.7)
        self.c_pgmaj = c("kernel.pgmajfault", 0.2, 5.4, 0.25)
        # disk io: modest system-disk activity, busier data disk if present
        self.sda = {k: c(f"sda.{k}", r, ph, amp) for k, r, ph, amp in (
            ("rd_ios", 3, 0.0, 0.3), ("rd_ticks", 2, 0.2, 0.3),
            ("wr_ios", 14, 0.4, 0.3), ("wr_ticks", 9, 0.6, 0.3),
            ("io_ticks", 11, 0.8, 0.05))}
        self.sdb = None
        if self.extra_fs:
            self.sdb = {k: c(f"sdb.{k}", r, ph, amp) for k, r, ph, amp in (
                ("rd_ios", 22, 1.0, 0.3), ("rd_ticks", 14, 1.2, 0.3),
                ("wr_ios", 35, 1.4, 0.3), ("wr_ticks", 22, 1.6, 0.3),
                ("io_ticks", 48, 1.8, 0.05))}
        self.c_rx_b = c("net.rx_bytes", self.rx_bps, 1.6)
        self.c_tx_b = c("net.tx_bytes", self.tx_bps, 2.3)
        self.c_rx_p = c("net.rx_pkts", self.rx_bps / 900, 3.0)
        self.c_tx_p = c("net.tx_pkts", self.tx_bps / 900, 3.7)

    @property
    def fqdn(self) -> str:
        return f"{self.short}.{DOMAIN}"

    def _procs(self) -> list[tuple[str, str, int, int, int, str]]:
        """(cgroup, user, vsz, rss, pid, cmd) — base daemons + role processes
        (+ one qemu process per guest on a hypervisor)."""
        rows = list(_LNX_BASE_PROCS)
        pid = 1200
        for user, vsz, rss, cmd in self.spec.get("procs", []):
            unit = (self.spec.get("units") or [("app.service", "")])[0][0]
            rows.append((f"system.slice/{unit}", user, vsz, rss, pid, cmd))
            pid += 3
        for g in self.guests:
            mem_kb = g.mem_total if hasattr(g, "mem_total") else \
                g.spec.get("mem_mb", 8192) * 1024
            rows.append((
                "machine.slice/machine-qemu.scope", "libvirt-qemu",
                mem_kb + 2_400_000, int(mem_kb * 0.82), pid,
                f"/usr/bin/qemu-system-x86_64 -name guest={g.short},"
                f"debug-threads=on -machine pc-q35-8.2 -m "
                f"{g.spec.get('mem_mb', 8192)}"))
            pid += 7
        return rows

    def build(self) -> bytes:  # noqa: PLR0915
        s = self.short
        now = int(time.time())
        nowf = time.time()
        uptime = int(nowf - START + self.uptime_offset)

        # ---- memory ----------------------------------------------------------
        mt = self.mem_total
        if self.guests:  # hypervisor: anon tracks the qemu RSS sum
            anon_base = int(sum(g.mem_total * 0.82 for g in self.guests))
            anon_base = min(anon_base, int(mt * 0.85))
        else:
            anon_base = int(mt * self.anon_f)
        anon = int(gauge(f"{s}.mem.anon", anon_base, amp_frac=0.03,
                         phase=self.phase + 1.5, period=1400))
        cached = int(gauge(f"{s}.mem.cached", mt * self.cached_f, amp_frac=0.03,
                           phase=self.phase + 0.4, period=1500))
        shmem = int(gauge(f"{s}.mem.shmem", mt * self.shmem_f, amp_frac=0.02,
                          phase=self.phase + 0.8, period=1600))
        buffers = int(gauge(f"{s}.mem.buffers", mt * 0.02, amp_frac=0.04,
                            phase=self.phase + 1.2, period=1100))
        sreclaim = int(gauge(f"{s}.mem.srec", mt * 0.03, amp_frac=0.03,
                             phase=self.phase + 2.0, period=1300))
        caches = cached + buffers + sreclaim
        mem_free = max(int(mt * 0.03), mt - anon - shmem - caches)
        anon_lru = anon + shmem
        file_lru = max(0, buffers + cached - shmem)
        mem_avail = mem_free + file_lru + sreclaim
        a_anon = int(anon_lru * 0.62)
        a_file = int(file_lru * 0.38)
        sunreclaim = int(mt * 0.008)
        threads = 220 + 30 * len(self.spec.get("procs", [])) \
            + 40 * len(self.guests)
        dirty = max(4_096, int(gauge(f"{s}.mem.dirty", mt * 0.001,
                                     amp_frac=0.20, phase=self.phase + 2.2,
                                     period=900)))
        committed = int(min(anon * 1.8, mt * 0.48))

        # ---- load ------------------------------------------------------------
        l1 = max(0.01, round(self.load1 * gauge(f"{s}.load1", 1.0, amp_frac=0.30,
                                                phase=self.phase + 0.2,
                                                period=300), 2))
        l5 = max(0.01, round(self.load1 * 0.92 * gauge(
            f"{s}.load5", 1.0, amp_frac=0.18, phase=self.phase + 1.1,
            period=900), 2))
        l15 = max(0.01, round(self.load1 * 0.85 * gauge(
            f"{s}.load15", 1.0, amp_frac=0.10, phase=self.phase + 2.1,
            period=2400), 2))

        user = self.c_user.sample(self.r_user)
        system = self.c_system.sample(self.r_system)
        idle = self.c_idle.sample(self.r_idle)
        iowait = self.c_iowait.sample(self.r_iowait)

        sda = {k: c.sample(r) for (k, c), r in zip(
            self.sda.items(), (3, 2, 14, 9, 11))}
        sdb = None
        if self.sdb:
            sdb = {k: c.sample(r) for (k, c), r in zip(
                self.sdb.items(), (22, 14, 35, 22, 48))}

        rx_b = self.c_rx_b.sample(self.rx_bps)
        tx_b = self.c_tx_b.sample(self.tx_bps)
        rx_p = self.c_rx_p.sample(self.rx_bps / 900)
        tx_p = self.c_tx_p.sample(self.tx_bps / 900)

        # ---- filesystems: slow creep + daily logrotate saw, well under 80 % --
        day = 86_400.0
        root_used = int(self.root_kb * self.root_used_frac
                        + min(self.root_kb * 0.04, uptime * 0.03)
                        + gauge(f"{s}.fs.root", 0, amp_abs=65_536, period=1800,
                                phase=self.phase))

        lines: list[str] = []
        a = lines.append

        a("<<<check_mk>>>")
        a(f"Version: {AGENT_VERSION}")
        a("AgentOS: linux")
        a(f"Hostname: {self.fqdn}")
        a("InstallationDirectory: /opt/checkmk/agent/default")
        a("PackageDirectory: /opt/checkmk/agent/default/package")
        a("RuntimeDirectory: /opt/checkmk/agent/default/runtime")
        a("OSType: linux")
        a("OSName: Ubuntu")
        a("OSVersion: 24.04")
        a("OSPlatform: ubuntu")
        a("FailedPythonReason: ")
        a("SSHClient: ")

        a("<<<cmk_agent_ctl_status:sep(0)>>>")
        a(_ctl_status_json(self.uuid))

        a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
        a("pluginsdir /opt/checkmk/agent/default/package/plugins")
        a("localdir /opt/checkmk/agent/default/package/local")
        a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"'
          % AGENT_VERSION)

        # ---- df + mounts -----------------------------------------------------
        a("<<<df_v2>>>")
        a(f"/dev/sda1 ext4 {self.root_kb} {root_used} {self.root_kb - root_used} "
          f"{round(root_used / self.root_kb * 100)}% /")
        for n, (mount, size_kb, used_frac) in enumerate(self.extra_fs, start=1):
            used = int(size_kb * used_frac
                       + size_kb * 0.012 * ((nowf % day) / day)   # daily saw
                       + gauge(f"{s}.fs.{mount}", 0, amp_abs=size_kb * 0.002,
                               period=1600, phase=self.phase + n))
            a(f"/dev/sdb{n} ext4 {size_kb} {used} {size_kb - used} "
              f"{round(used / size_kb * 100)}% {mount}")
        a("[df_inodes_start]")
        a(f"/dev/sda1 ext4 2621440 214380 {2621440 - 214380} 8% /")
        for n, (mount, size_kb, _) in enumerate(self.extra_fs, start=1):
            ino = max(65536, size_kb // 16)
            a(f"/dev/sdb{n} ext4 {ino} {ino // 40} {ino - ino // 40} 3% {mount}")
        a("[df_inodes_end]")
        a("<<<mounts>>>")
        a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
        for n, (mount, _, _) in enumerate(self.extra_fs, start=1):
            a(f"/dev/sdb{n} {mount} ext4 rw,noatime 0 0")

        # ---- meminfo (full Ubuntu 24.04 key set) -----------------------------
        a("<<<mem>>>")
        a(f"MemTotal:       {mt} kB")
        a(f"MemFree:        {mem_free} kB")
        a(f"MemAvailable:   {mem_avail} kB")
        a(f"Buffers:        {buffers} kB")
        a(f"Cached:         {cached} kB")
        a("SwapCached:     0 kB")
        a(f"Active:         {a_anon + a_file} kB")
        a(f"Inactive:       {(anon_lru - a_anon) + (file_lru - a_file)} kB")
        a(f"Active(anon):   {a_anon} kB")
        a(f"Inactive(anon): {anon_lru - a_anon} kB")
        a(f"Active(file):   {a_file} kB")
        a(f"Inactive(file): {file_lru - a_file} kB")
        a("Unevictable:    0 kB")
        a("Mlocked:        0 kB")
        a("SwapTotal:      0 kB")
        a("SwapFree:       0 kB")
        a("Zswap:          0 kB")
        a("Zswapped:       0 kB")
        a(f"Dirty:          {dirty} kB")
        a("Writeback:      0 kB")
        a(f"AnonPages:      {anon} kB")
        a(f"Mapped:         {int(mt * 0.02)} kB")
        a(f"Shmem:          {shmem} kB")
        a(f"KReclaimable:   {sreclaim} kB")
        a(f"Slab:           {sreclaim + sunreclaim} kB")
        a(f"SReclaimable:   {sreclaim} kB")
        a(f"SUnreclaim:     {sunreclaim} kB")
        a(f"KernelStack:    {threads * 16} kB")
        a(f"PageTables:     {int(mt * 0.004)} kB")
        a("SecPageTables:  0 kB")
        a("NFS_Unstable:   0 kB")
        a("Bounce:         0 kB")
        a("WritebackTmp:   0 kB")
        a(f"CommitLimit:    {mt // 2} kB")
        a(f"Committed_AS:   {committed} kB")
        a("VmallocTotal:   34359738367 kB")
        a(f"VmallocUsed:    {int(mt * 0.003)} kB")
        a("VmallocChunk:   0 kB")
        a("Percpu:         12288 kB")
        a("HardwareCorrupted: 0 kB")
        a("AnonHugePages:  0 kB")
        a("ShmemHugePages: 0 kB")
        a("ShmemPmdMapped: 0 kB")
        a("FileHugePages:  0 kB")
        a("FilePmdMapped:  0 kB")
        a("CmaTotal:       0 kB")
        a("CmaFree:        0 kB")
        a("Unaccepted:     0 kB")
        a("Balloon:        0 kB")
        a("HugePages_Total:       0")
        a("HugePages_Free:        0")
        a("HugePages_Rsvd:        0")
        a("HugePages_Surp:        0")
        a("Hugepagesize:       2048 kB")
        a("Hugetlb:        0 kB")
        a(f"DirectMap4k:    {int(mt * 0.015)} kB")
        a(f"DirectMap2M:    {int(mt * 0.37)} kB")
        a(f"DirectMap1G:    {int(mt * 0.62)} kB")

        # ---- cpu / uptime ----------------------------------------------------
        a("<<<cpu>>>")
        nproc = 180 + 12 * len(self.spec.get("procs", [])) + 3 * len(self.guests)
        a(f"{l1} {l5} {l15} 2/{nproc} {28000 + self.c_proc.sample(4) % 9999} "
          f"{self.ncpu}")
        a("<<<uptime>>>")
        a(f"{uptime}.00 {int(uptime * (self.ncpu * 0.85)) }.00")

        # ---- timesyncd (dynamic — both timestamps vs wall clock) -------------
        last_sync = now - int((nowf - START) % 2048)
        sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC",
                                 time.gmtime(last_sync))
        offset_us = int(gauge(f"{s}.ntp.offset", 0, amp_abs=1200,
                              phase=self.phase + 1.3, period=600))
        jitter_ms = max(0.1, round(gauge(f"{s}.ntp.jitter", 1.8, amp_abs=0.6,
                                         phase=self.phase + 0.7, period=700), 3))
        a("<<<timesyncd>>>")
        a("       Server: 10.10.0.21 (ntp-01.corp.meridian-retail.com)")
        a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
        a("         Leap: normal")
        a("      Version: 4")
        a("      Stratum: 3")
        a("    Reference: 0A0A0015")
        a("    Precision: 1us (-25)")
        a("Root distance: 12.104ms (max: 5s)")
        a(f"       Offset: {offset_us:+d}us")
        a("        Delay: 1.207ms")
        a(f"       Jitter: {jitter_ms:.3f}ms")
        a(f" Packet count: {420 + int((nowf - START) / 2048)}")
        a("    Frequency: +9.204ppm")
        a(f"[[[{last_sync}]]]")
        a("<<<timesyncd_ntpmessage:sep(10)>>>")
        a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=3, Precision=-25, "
          "RootDelay=2.104ms, RootDispersion=0.847ms, Reference=0A0A0015, "
          f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
          f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
          "Ignored=no, PacketCount=61, Jitter=0.984ms }")
        a("Timezone=UTC")

        a("<<<apt:sep(0)>>>")
        a("No updates pending for installation")

        # ---- kernel / diskstat ----------------------------------------------
        a("<<<kernel>>>")
        a(str(now))
        a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
        a(f"ctxt {self.c_ctxt.sample(900 * self.ncpu)}")
        a(f"processes {self.c_proc.sample(4)}")
        a(f"pgmajfault {self.c_pgmaj.sample(0.2)}")

        a("<<<diskstat>>>")
        a(str(now))
        a(f"8 0 sda {sda['rd_ios']} 0 {sda['rd_ios'] * 16} {sda['rd_ticks']} "
          f"{sda['wr_ios']} 0 {sda['wr_ios'] * 32} {sda['wr_ticks']} 0 "
          f"{sda['io_ticks']} {sda['io_ticks'] * 2} 0 0 0 0")
        if sdb:
            a(f"8 16 sdb {sdb['rd_ios']} 0 {sdb['rd_ios'] * 24} "
              f"{sdb['rd_ticks']} {sdb['wr_ios']} 0 {sdb['wr_ios'] * 48} "
              f"{sdb['wr_ticks']} 0 {sdb['io_ticks']} {sdb['io_ticks'] * 2} "
              "0 0 0 0")

        # ---- lnx_if (both variants) ------------------------------------------
        a("<<<lnx_if>>>")
        a("[start_iplink]")
        a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
          "group default qlen 1000")
        a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
        a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
          "state UP group default qlen 1000")
        a(f"    link/ether {self.mac} brd ff:ff:ff:ff:ff:ff")
        a("[end_iplink]")
        a("<<<lnx_if:sep(58)>>>")
        a(f"eth0: {rx_b} {rx_p} 0 0 0 0 0 0 {tx_b} {tx_p} 0 0 0 0 0 0")
        a("[eth0]")
        a("\tSpeed: 10000Mb/s")
        a("\tDuplex: Full")
        a("\tAuto-negotiation: on")
        a("\tLink detected: yes")
        a(f"Address: {self.mac}")

        # ---- tcp -------------------------------------------------------------
        estab_base = 18 + (self.rx_bps + self.tx_bps) / 250_000
        estab = round(gauge(f"{s}.tcp.estab", estab_base, amp_frac=0.2,
                            phase=self.phase + 0.9, period=700))
        tw = round(gauge(f"{s}.tcp.tw", estab_base * 0.5, amp_frac=0.3,
                         phase=self.phase + 2.4, period=500))
        a("<<<tcp_conn_stats>>>")
        a(f"01 {max(4, estab)}")
        a(f"02 {random.randint(0, 2)}")
        a(f"06 {max(2, tw)}")
        a("0A 9")

        # ---- SMART -----------------------------------------------------------
        temp = round(gauge(f"{s}.smart.sda", 27.0, amp_abs=1.3,
                           phase=self.phase + 2.1, period=1100))
        a("<<<smart_posix_all:sep(0)>>>")
        a(_smart_json("/dev/sda", self.disk_model, self.disk_serial,
                      self.disk_hours + int(uptime / 3600), temp))
        if self.extra_fs:
            temp2 = round(gauge(f"{s}.smart.sdb", 29.0, amp_abs=1.3,
                                phase=self.phase + 3.3, period=1300))
            a(_smart_json("/dev/sdb", "Samsung SSD 883 DCT 1.92TB",
                          self.data_serial,
                          self.disk_hours + int(uptime / 3600), temp2))

        # ---- ps ---------------------------------------------------------------
        up_days = uptime // 86400
        elapsed = f"{up_days}-04:18:52"
        a("<<<ps_lnx>>>")
        a("[time]")
        a(str(now))
        a("[processes]")
        a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
        for cg, usr, vsz, rss, pid, cmd in self._procs():
            a(f"0::/{cg} {usr} {vsz} {rss} 00:00:{pid % 50 + 2:02d} {elapsed} "
              f"{pid} {cmd}")

        # ---- systemd units ----------------------------------------------------
        a("<<<systemd_units>>>")
        units = [(n, "active", "running", d)
                 for n, d in self.spec.get("units", [])]
        units += _LNX_BASE_UNITS
        a("[list-unit-files]")
        for name, _act, _sub, _descr in units:
            a(f"{name} enabled enabled")
        a("[status]")
        a("[all]")
        for name, act, sub, descr in units:
            a(f"{name} loaded {act} {sub} {descr}")

        return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  Windows fleet host
# --------------------------------------------------------------------------- #
class WindowsHost:
    def __init__(self, short: str, spec: dict) -> None:
        self.short = short
        self.spec = spec
        self.os = "windows"
        # upstream network device (short name), assigned by expand_roster()
        self.net_parent: str | None = None
        rnd = random.Random(f"{short}:fleet-v1")
        lo, hi = spec.get("uptime_days", (10, 60))
        self.uptime_offset = rnd.uniform(lo, hi) * 86400
        self.phase = rnd.uniform(0, 6.28)
        self.uuid = _uuid(rnd)
        self.ncpu = spec.get("ncpu", 4)
        self.mem_total = spec.get("mem_mb", 16384) * 1024
        self.c_kb = spec.get("c_gb", 120) * 1_048_576
        self.c_used_frac = min(0.72, spec.get("c_used", 0.45)
                               + rnd.uniform(-0.04, 0.04))
        self.d_drive = spec.get("d_drive")
        if self.d_drive:
            gb, used = self.d_drive
            self.d_kb = gb * 1_048_576
            self.d_used_frac = min(0.75, used + rnd.uniform(-0.04, 0.04))

    @property
    def fqdn(self) -> str:
        return f"{self.short}.{DOMAIN}"

    def build(self) -> bytes:
        s = self.short
        now = int(time.time())
        nowf = time.time()
        uptime = int(nowf - START + self.uptime_offset)
        computername = s.upper()

        lines: list[str] = []
        a = lines.append
        TAB = "\t"

        a("<<<check_mk>>>")
        a(f"Version: {AGENT_VERSION}")
        a("BuildDate: Apr  3 2026")
        a("AgentOS: windows")
        a(f"Hostname: {self.fqdn}")
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

        a("<<<cmk_agent_ctl_status:sep(0)>>>")
        a(_ctl_status_json(self.uuid))

        a("<<<wmi_cpuload:sep(124)>>>")
        qlen = max(0, round(gauge(f"{s}.cpu.qlen", self.ncpu * 0.25,
                                  amp_abs=1.2, phase=self.phase + 0.3,
                                  period=420)))
        a("[system_perf]")
        a("Name|ProcessorQueueLength|Timestamp_PerfTime|Frequency_PerfTime|WMIStatus")
        a(f"|{qlen}|{int(uptime * 10_000_000)}|10000000|OK")
        a("[computer_system]")
        a("Name|NumberOfLogicalProcessors|NumberOfProcessors|WMIStatus")
        a(f"{computername}|{self.ncpu}|1|OK")

        a("<<<uptime>>>")
        a(str(uptime))

        a("<<<mem>>>")
        mt = self.mem_total
        mem_free = int(gauge(f"{s}.mem.free", mt * 0.46, amp_frac=0.04,
                             phase=self.phase + 0.6, period=1500))
        page_total = int(mt * 1.45)
        page_free = int(gauge(f"{s}.page.free", page_total * 0.74,
                              amp_frac=0.02, phase=self.phase + 1.4,
                              period=1700))
        a(f"MemTotal:      {mt} kB")
        a(f"MemFree:       {mem_free} kB")
        a(f"SwapTotal:     {int(mt * 0.45)} kB")
        a(f"SwapFree:      {int(mt * 0.42)} kB")
        a(f"PageTotal:     {page_total} kB")
        a(f"PageFree:      {page_free} kB")
        a("VirtualTotal:  137438953344 kB")
        a("VirtualFree:   137431814144 kB")

        # ---- drives (steady; slow creep + wobble, well under 80/90) ----------
        a("<<<df:sep(9)>>>")
        c_used = int(self.c_kb * self.c_used_frac
                     + min(self.c_kb * 0.02, uptime * 0.02)
                     + gauge(f"{s}.c.used", 0, amp_abs=90_000, period=1500,
                             phase=self.phase))
        a(TAB.join(["C:\\", "NTFS", str(self.c_kb), str(c_used),
                    str(self.c_kb - c_used),
                    f"{round(c_used / self.c_kb * 100)}%", "C:\\"]))
        if self.d_drive:
            d_used = int(self.d_kb * self.d_used_frac
                         + gauge(f"{s}.d.used", 0, amp_abs=400_000,
                                 period=1800, phase=self.phase + 1))
            a(TAB.join(["D:\\", "NTFS", str(self.d_kb), str(d_used),
                        str(self.d_kb - d_used),
                        f"{round(d_used / self.d_kb * 100)}%", "D:\\"]))

        # ---- services ---------------------------------------------------------
        a("<<<services>>>")
        for name, status, descr in (list(self.spec.get("services", []))
                                    + _WIN_BASE_SERVICES):
            a(f"{name} {status} {descr}")

        a("<<<checkmk_agent_plugins_win:sep(0)>>>")
        a("pluginsdir C:\\ProgramData\\checkmk\\agent\\plugins")
        a("localdir C:\\ProgramData\\checkmk\\agent\\local")
        a('C:\\ProgramData\\checkmk\\agent\\plugins\\cmk_update_agent.checkmk.py:CMK_VERSION = "%s"'
          % AGENT_VERSION)
        a('C:\\ProgramData\\checkmk\\agent\\plugins\\mk_inventory.vbs:CMK_VERSION = "%s"'
          % AGENT_VERSION)

        a("<<<ps:sep(9)>>>")
        procs = list(_WIN_BASE_PROCS)
        pid = 2200
        for usr, vsz, ws, exe in self.spec.get("win_procs", []):
            procs.append((usr, vsz, ws, pid, 16, exe))
            pid += 4
        for usr, vsz, ws, ppid, threads, name in procs:
            a(f"({usr},{vsz},{ws},0,{ppid},{threads * 2},{ppid * 156250},"
              f"{ppid * 312500},{threads * 30},{threads},{uptime}){TAB}{name}")

        a("<<<systemtime>>>")
        a(str(now))

        return ("\r\n".join(lines) + "\r\n").encode("utf-8")


Host = LinuxHost  # for type hints in guests lists


# --------------------------------------------------------------------------- #
#  Roster expansion (+ VM -> hypervisor assignment)
# --------------------------------------------------------------------------- #
# Access switches the fleet hangs off — names MUST match snmp/netsim.py's
# REPLAY_ROSTER. Physical DC hosts round-robin across the 8 DC top-of-rack
# switches; each warehouse's iron sits on that warehouse's first access switch.
# (Endpoints belong on an access switch, not the 12-port core.)
DC_TOR = [f"sw-dc-tor-{n:02d}" for n in range(1, 9)]
WH_SWITCH = {"wh1": "wh1-sw-01", "wh2": "wh2-sw-01"}


def expand_roster() -> dict[str, LinuxHost | WindowsHost]:
    wanted = {p.strip() for p in
              os.environ.get("FLEET_CLASSES", "").split(",") if p.strip()} or None
    classes = [c for c in profiles.all_classes()
               if wanted is None or c["prefix"] in wanted]

    hosts: dict[str, LinuxHost | WindowsHost] = {}
    vms_by_site: dict[str, list] = {}
    hv_specs: list[tuple[str, dict, str]] = []   # (short, cls, site)

    for cls in classes:
        first = cls.get("first", 1)
        site = cls.get("site", "dc")
        for n in range(first, first + cls["count"]):
            short = f"{cls['prefix']}-{n:02d}"
            if cls.get("hypervisor"):
                hv_specs.append((short, cls, site))   # built after VM assignment
                continue
            host = (LinuxHost(short, cls) if cls["os"] == "linux"
                    else WindowsHost(short, cls))
            hosts[short] = host
            if cls.get("vm", True):
                vms_by_site.setdefault(site, []).append(host)

    # hypervisors grouped by site
    hv_by_site: dict[str, list] = {}
    for short, cls, site in hv_specs:
        hv_by_site.setdefault(site, []).append(short)

    # Round-robin each site's VMs across that site's hypervisors; a VM becomes
    # a CHILD of its hypervisor (topology) and a qemu process in its ps. A site
    # with no hypervisor of its own falls back to the DC iron.
    guests_of: dict[str, list] = {short: [] for short, _, _ in hv_specs}
    for site, site_vms in vms_by_site.items():
        pool = hv_by_site.get(site) or hv_by_site.get("dc") or []
        for i, vm in enumerate(site_vms):
            if not pool:
                break
            hv_short = pool[i % len(pool)]
            guests_of[hv_short].append(vm)
            vm.net_parent = hv_short

    # Build the hypervisors with their guests; each hangs off an access switch.
    dc_rr = 0
    for short, cls, site in hv_specs:
        hosts[short] = LinuxHost(short, cls, guests=guests_of[short])
        if site == "dc":
            hosts[short].net_parent = DC_TOR[dc_rr % len(DC_TOR)]
            dc_rr += 1
        else:
            hosts[short].net_parent = WH_SWITCH.get(site, DC_TOR[0])

    # Any remaining physical host (e.g. the Veeam server) hangs off a switch too.
    for short, host in hosts.items():
        if getattr(host, "net_parent", None):
            continue
        site = host.spec.get("site", "dc")
        if site == "dc":
            host.net_parent = DC_TOR[dc_rr % len(DC_TOR)]
            dc_rr += 1
        else:
            host.net_parent = WH_SWITCH.get(site, DC_TOR[0])

    return hosts


HOSTS = expand_roster()


# --------------------------------------------------------------------------- #
#  Persistence — counters + START survive restarts (graphs stay continuous)
# --------------------------------------------------------------------------- #
def save_state() -> None:
    if not STATE_FILE:
        return
    data = {
        "version": 1,
        "start": START,
        "counters": {n: [c.acc, c.last] for n, c in _ALL_COUNTERS.items()},
    }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"[state] save failed: {exc}")


def load_state() -> None:
    global START
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[state] load failed ({exc}) — starting fresh")
        return
    START = data.get("start", START)
    saved = data.get("counters", {})
    restored = 0
    for name, c in _ALL_COUNTERS.items():
        if name in saved:
            c.acc, c.last = saved[name]
            restored += 1
    print(f"[state] restored {restored}/{len(_ALL_COUNTERS)} counters, "
          "uptime continuous")


def state_saver() -> None:
    while True:
        time.sleep(60)
        save_state()


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #
class HttpHandler(BaseHTTPRequestHandler):
    server_version = "fleet/1.0"

    def log_message(self, fmt: str, *args) -> None:
        pass  # 200 hosts x polls — keep the log quiet

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.partition("?")[0].rstrip("/") or "/"
        if path.startswith("/agent/"):
            short = path[len("/agent/"):]
            host = HOSTS.get(short)
            if host is None:
                self.send_response(404)
                self.end_headers()
                return
            payload = host.build()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = json.dumps({
            "fleet": [{
                "name": h.short, "fqdn": h.fqdn, "os": h.os,
                "role": h.spec.get("role", "infrastructure"),
                "descr": h.spec.get("descr", ""),
                "parent": h.net_parent,
                "site": h.spec.get("site", "dc"),
            } for h in HOSTS.values()],
            "count": len(HOSTS),
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    load_state()
    threading.Thread(target=state_saver, daemon=True).start()
    lin = sum(1 for h in HOSTS.values() if h.os == "linux")
    win = len(HOSTS) - lin
    print(f"[boot] fleet: {len(HOSTS)} hosts ({lin} linux, {win} windows) "
          f"on http/{HTTP_PORT}")
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
    finally:
        save_state()


if __name__ == "__main__":
    main()
