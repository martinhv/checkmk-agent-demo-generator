#!/usr/bin/env python3
"""Meridian Retail demo host: app-worker-01 — the order/settlement worker.

A Java background worker (Spring Boot, `order-worker.service`) that pulls jobs
off the payments queue. The incident is the *mirror image* of the dying-disk
db host: there the CPU-load page was a red herring (the disk was the cause);
here the resource exhaustion is **real** — a memory leak in the worker fills
RAM, spills into swap (major-fault thrash), and the OOM killer finally reaps
the JVM, so `order-worker.service` flaps. The AI fuses Memory + Swap + major
page faults + the failed unit into "application heap leak, being OOM-killed —
fix the worker, don't add RAM".

Three states (the timeline is part of the story):

  healthy   ~6.5 GiB RAM used of 16, swap empty, worker running. all green.
  degraded  the leak grows: RAM climbs, swap starts filling, major page faults
            spike (the box starts thrashing). The Memory service crosses WARN
            on Committed_AS (the JVM commits heap past the commit limit) while
            virtual usage and the swap graph climb — the breadcrumb. The unit
            is still up. Trigger ~20 min before showtime.
  broken    memory is full: virtual (RAM+swap) usage > 90 % -> Memory CRIT, and
            the OOM killer reaps the JVM -> `order-worker.service` failed ->
            Systemd Service Summary CRIT. The worker flaps (restart count /
            OOM-kill count climb live). Two reds, one root cause.

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env (see also AGENT_PORT/HTTP_PORT/START_STATE/STATE_FILE):
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before the OOM kill auto-fires
                 (default: 20; 0 disables)
  LEAK_FILL_MIN  minutes for the leak to fill memory while degraded
                 (default: 18; the Memory graph climbs over this window)
  BREAK_RAMP_MIN minutes for the broken impact (swap peg -> virtual CRIT) to
                 reach full force (default: 4; 0 = instant)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "app-worker-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "20"))
LEAK_FILL_MIN = float(os.environ.get("LEAK_FILL_MIN", "18"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "4"))

START = time.time()
UPTIME_OFFSET = 9 * 86400  # pretend the host has been up ~9 days

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the leak started (degraded or broken) -> drives the rising memory curve
_degraded_since: float | None = None if _state == "healthy" else START
# when the OOM/incident started -> drives swap peg + service-failed + OOM count
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


# The single driver of the whole incident: 0 (healthy) .. 1 (memory full).
#   * the leak fills memory over LEAK_FILL_MIN while degraded, but only up to
#     0.78 — enough for the Memory service to WARN (Committed_AS over the limit)
#     and the swap graph to climb, but NOT enough to trip the virtual-usage
#     CRIT. That stays the *broken*-state headline.
#   * broken pegs swap and pushes pressure 0.78 -> 1.0 over the break ramp:
#     virtual (RAM+swap) usage crosses 90 % -> Memory CRIT.
def pressure() -> float:
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


def oom_kills() -> int:
    """How many times the OOM killer has reaped the JVM since the break.

    Once memory is full the worker is killed and systemd restarts it; it
    re-leaks and gets killed again — a flap. ~1 kill per 90 s, shown live on
    /admin and reflected in the unit's restart count.
    """
    bs = broken_seconds()
    return 0 if bs <= 0 else 1 + int(bs / 90.0)


# --------------------------------------------------------------------------- #
#  Autocorrelated gauges + monotonic counters (verbatim machinery from the
#  dying-disk reference; see CLAUDE.md for why a single sine is wrong).
# --------------------------------------------------------------------------- #
_ALL_COUNTERS: dict[str, Counter] = {}


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


class Counter:
    def __init__(
        self,
        name: str,
        phase: float = 0.0,
        amp: float = 0.30,
        period: float = 1200.0,
        start: float = 0.0,
    ) -> None:
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


def _aged(rate_per_s: float) -> float:
    return rate_per_s * UPTIME_OFFSET


# /proc/stat jiffies: 100 Hz * 4 CPUs = ~400 ticks/s. The worker GCs hard under
# the leak (user up) and faults on swap (system/iowait up); never a CPU alert.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(70))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(30))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(280))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(10))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(3400))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(5))
# the star of the show: major page faults (swap-in). Healthy ~1/s; thrashing
# climbs to several hundred/s. The Kernel Performance check graphs it (no
# default alert) — a strong arrow at memory pressure.
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(1))

SDA = {  # single system SSD; root + swap live here. Swap-in bumps it (calm).
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(6)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(4)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(20)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(15)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(22)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(180_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(140_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(620))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(560))

# queue throughput: jobs the worker drains/s. Collapses while it's thrashing /
# being OOM-killed (visible via the app's own pushed metric — here a TCP conn
# proxy + the postgres-free story keeps it graph-only corroboration).
C_JOBS = Counter("app.jobs_done", phase=2.0, start=_aged(38))


# --------------------------------------------------------------------------- #
#  SMART (one healthy SSD, for parity with a real agent dump). Stays green
#  forever — raw values are zero so the discovery baseline is never exceeded.
# --------------------------------------------------------------------------- #
def _smart_json(name: str, model: str, serial: str, hours: int, temp: int) -> str:
    doc = {
        "device": {"name": name, "type": "sat", "protocol": "ATA"},
        "model_name": model,
        "serial_number": serial,
        "smart_status": {"passed": True},
        "power_on_time": {"hours": hours},
        "temperature": {"current": temp},
        "ata_smart_attributes": {
            "table": [
                {
                    "id": 5,
                    "name": "Reallocated_Sector_Ct",
                    "value": 100,
                    "thresh": 10,
                    "raw": {"value": 0},
                },
                {
                    "id": 12,
                    "name": "Power_Cycle_Count",
                    "value": 100,
                    "thresh": 0,
                    "raw": {"value": 23},
                },
                {
                    "id": 187,
                    "name": "Reported_Uncorrect",
                    "value": 100,
                    "thresh": 0,
                    "raw": {"value": 0},
                },
                {
                    "id": 197,
                    "name": "Current_Pending_Sector",
                    "value": 100,
                    "thresh": 0,
                    "raw": {"value": 0},
                },
                {
                    "id": 199,
                    "name": "UDMA_CRC_Error_Count",
                    "value": 200,
                    "thresh": 0,
                    "raw": {"value": 0},
                },
                {
                    "id": 177,
                    "name": "Wear_Leveling_Count",
                    "value": 96,
                    "thresh": 5,
                    "raw": {"value": 88},
                },
            ]
        },
    }
    return json.dumps(doc, separators=(",", ":"))


def _kb(mib: float) -> int:
    return int(mib * 1024)


def filesystem_usage(now: float) -> tuple[int, int]:
    """root / and /opt/orderworker — both green, growing + cleaned over time."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0
    root_base = 13_631_488  # ~13 GiB of 40
    root_logs = 1_048_576 * ((now % day) / day)  # 0..1 GiB daily
    root_growth = min(1_572_864, uptime * 0.04)
    root_used = int(
        root_base + root_logs + root_growth + gauge("fs.root", 0, amp_abs=80_000, period=1500)
    )
    opt_base = 28_311_552  # ~27 GiB of 80
    opt_spool = 2_097_152 * ((now % 1800.0) / 1800.0)  # job spool, 30-min teeth
    opt_growth = min(3_145_728, uptime * 0.6)
    opt_used = int(
        opt_base + opt_spool + opt_growth + gauge("fs.opt", 0, amp_abs=160_000, period=900)
    )
    return root_used, opt_used


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4
    broken = state == "broken"
    p = pressure()

    # ---- memory: the whole story. MemUsed = MemTotal - MemFree - Caches
    #      (Caches = Cached + Buffers + SwapCached + SReclaimable). virtual
    #      (RAM+Swap) used % has default levels 80/90 and is always shown ->
    #      the CRIT lever. Committed_AS vs CommitLimit warns at 100 % -> the
    #      degraded WARN. We drive cached/free/swap from `pressure`. ---------- #
    mem_total = 16_384_000  # kB
    swap_total = 4_194_300
    commit_limit = swap_total + mem_total // 2  # kernel default

    # leak fills RAM and reclaims page cache; swap engages past ~35 % pressure.
    mem_used_t = _lerp(6_500_000, 15_200_000, p)
    swap_used_t = _lerp(0, 3_950_000, max(0.0, min(1.0, (p - 0.35) / 0.65)))
    cached = int(
        gauge("mem.cached", _lerp(6_000_000, 900_000, p), amp_frac=0.02, phase=0.4, period=1500)
    )
    buffers = int(_lerp(180_000, 40_000, p))
    sreclaim = int(_lerp(488_256, 144_000, p))
    swapcached = int(_lerp(0, 120_000, max(0.0, (p - 0.35) / 0.65)))
    caches = cached + buffers + swapcached + sreclaim
    mem_free = max(120_000, mem_total - int(mem_used_t) - caches)
    swap_free = max(40_000, swap_total - int(swap_used_t))
    # JVM commits heap aggressively -> Committed_AS crosses the commit limit
    # (12.19 GiB) around mid-leak, the Memory service's degraded WARN.
    committed = int(
        gauge(
            "mem.committed", _lerp(7_000_000, 16_800_000, p), amp_frac=0.01, phase=1.2, period=1700
        )
    )

    # anon LRU = AnonPages + Shmem ; file LRU = Buffers + Cached - Shmem
    shmem = 49_152
    anon = max(2_000_000, mem_total - mem_free - caches - 700_000)  # ~MemUsed - slab/pagetables
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = max(mem_free, mem_free + file_lru + sreclaim)
    a_anon = int(anon_lru * 0.57)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.31)
    i_file = file_lru - a_file
    slab = sreclaim + 122_880
    # threads: a leaking JVM spawns more (~16 KiB kernel stack each)
    threads = int(_lerp(360, 540, p))
    kernel_stack = threads * 16
    dirty = max(8_192, int(gauge("mem.dirty", 18_432, amp_frac=0.15, phase=2.0, period=800)))

    # ---- load: elevated by GC + swap-in D-state, but stays GREEN (the root
    #      cause is memory, not CPU — keep the noise down). 15-min < 20 WARN. -- #
    r1, r5, r15 = break_ramp(0.55), break_ramp(0.75), break_ramp(1.0)
    base_l = _lerp(0.7, 7.5, p)
    l1 = round(base_l * gauge("load1", 1.0, amp_frac=0.25, phase=0.2, period=300), 2)
    l5 = round(base_l * 0.92 * gauge("load5", 1.0, amp_frac=0.14, phase=1.0, period=900), 2)
    l15 = round(base_l * 0.84 * gauge("load15", 1.0, amp_frac=0.07, phase=2.0, period=2400), 2)
    runnable = 1 + round(2 * p)
    total_procs = round(_lerp(322, 470, p))

    # ---- /proc/stat: GC burns user, page faults burn system, swap-in -> iowait
    user = C_USER.sample(_lerp(70, 150, p))
    system = C_SYSTEM.sample(_lerp(30, 95, p))
    idle = C_IDLE.sample(_lerp(280, 95, p))
    iowait = C_IOWAIT.sample(_lerp(10, 60, p))
    # major page faults: the thrash signal — climbs hard with pressure
    pgmaj_rate = _lerp(1, 760, p)

    # ---- diskstat: single SSD. swap-in adds read load (corroborates), calm. -- #
    sda_rd = SDA["rd_ios"].sample(_lerp(6, 140, p))
    sda_rdt = SDA["rd_ticks"].sample(_lerp(4, 90, p))
    sda_wr = SDA["wr_ios"].sample(_lerp(20, 60, p))
    sda_wrt = SDA["wr_ticks"].sample(_lerp(15, 70, p))
    sda_iot = SDA["io_ticks"].sample(_lerp(22, 240, p))

    rx_bytes = C_RX_B.sample(180_000)
    tx_bytes = C_TX_B.sample(140_000)
    rx_pkts = C_RX_P.sample(620)
    tx_pkts = C_TX_P.sample(560)
    jobs = C_JOBS.sample(_lerp(38, 3, p))  # throughput collapses under thrash

    sda_temp = round(gauge("smart.sda.temp", 31, amp_abs=1.2, phase=2.1, period=1100))
    sda_smart = _smart_json(
        "/dev/sda",
        "SAMSUNG MZ7L3480HCHQ-00A07",
        "S6KSNX0T214518",
        int(uptime / 3600) + 18000,
        sda_temp,
    )

    lines: list[str] = []
    a = lines.append

    a("<<<check_mk>>>")
    a(f"Version: {AGENT_VERSION}")
    a("AgentOS: linux")
    a(f"Hostname: {HOSTNAME}")
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
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now + 318 * 86400))
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
                        "uuid": "b21e7740-1f0c-4c2a-9d33-2a7c5e9b4d02",
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
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"' % AGENT_VERSION)

    a("<<<df_v2>>>")
    root_size = 41_943_040
    opt_size = 83_886_080
    root_used, opt_used = filesystem_usage(time.time())
    a(
        f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
        f"{round(root_used / root_size * 100)}% /"
    )
    a(
        f"/dev/sda2 ext4 {opt_size} {opt_used} {opt_size - opt_used} "
        f"{round(opt_used / opt_size * 100)}% /opt/orderworker"
    )
    a("[df_inodes_start]")
    a(f"/dev/sda1 ext4 2621440 286331 {2621440 - 286331} 11% /")
    a(f"/dev/sda2 ext4 5242880 142887 {5242880 - 142887} 3% /opt/orderworker")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sda2 /opt/orderworker ext4 rw,relatime 0 0")

    a("<<<mem>>>")
    a(f"MemTotal:       {mem_total} kB")
    a(f"MemFree:        {mem_free} kB")
    a(f"MemAvailable:   {mem_available} kB")
    a(f"Buffers:        {buffers} kB")
    a(f"Cached:         {cached} kB")
    a(f"SwapCached:     {swapcached} kB")
    a(f"Active:         {a_anon + a_file} kB")
    a(f"Inactive:       {i_anon + i_file} kB")
    a(f"Active(anon):   {a_anon} kB")
    a(f"Inactive(anon): {i_anon} kB")
    a(f"Active(file):   {a_file} kB")
    a(f"Inactive(file): {i_file} kB")
    a("Unevictable:    0 kB")
    a("Mlocked:        0 kB")
    a(f"SwapTotal:      {swap_total} kB")
    a(f"SwapFree:       {swap_free} kB")
    a("Zswap:          0 kB")
    a("Zswapped:       0 kB")
    a(f"Dirty:          {dirty} kB")
    a("Writeback:      0 kB")
    a(f"AnonPages:      {anon} kB")
    a("Mapped:         410624 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     122880 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     78848 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    54272 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         14336 kB")
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
    a("DirectMap4k:    284620 kB")
    a("DirectMap2M:    6008832 kB")
    a("DirectMap1G:    11534336 kB")

    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {28000 + C_PROC.sample(5) % 9999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.2)}.00")

    # sawtooths 0->34min (poll interval), anchored to boot so it's continuous
    # across restarts and independent of push-lagged payload timestamps.
    last_sync = now - int((now - START) % 2048)
    sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = random.randint(-1800, 1800)
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.56 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: B97D5A38")
    a("    Precision: 1us (-25)")
    a("Root distance: 12.041ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 19.882ms")
    a(f"       Jitter: {random.randint(800, 3200) / 1000:.3f}ms")
    a(f" Packet count: {610 + int((time.time() - START) / 2048)}")
    a("    Frequency: +9.114ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a(
        "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
        "RootDelay=9.323ms, RootDispersion=1.221ms, Reference=B97D5A38, "
        f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
        f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
        "Ignored=no, PacketCount=58, Jitter=1.118ms }"
    )
    a("Timezone=UTC")

    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(3400)}")
    a(f"processes {C_PROC.sample(5)}")
    a(f"pgmajfault {C_PGMAJ.sample(pgmaj_rate)}")

    a("<<<diskstat>>>")
    a(str(now))
    a(
        f"8 0 sda {sda_rd} 0 {sda_rd * 24} {sda_rdt} {sda_wr} 0 "
        f"{sda_wr * 40} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0"
    )

    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a(
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
        "state UP group default qlen 1000"
    )
    a("    link/ether 02:42:ac:11:00:2a brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:2a")

    a("<<<tcp_conn_stats>>>")
    a(f"01 {round(gauge('tcp.estab', 22, amp_abs=5, phase=0.9, period=700))}")
    a(f"02 {random.randint(0, 1)}")
    a(f"06 {round(gauge('tcp.timewait', 9, amp_abs=3, phase=2.4, period=500))}")
    a("0A 3")

    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # ---- processes: the JVM worker (RSS climbs with the leak) + daemons. ---- #
    java_rss = int(_lerp(2_500_000, 12_800_000, p))
    java_vsz = java_rss + 2_600_000
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_000, 12_800, "00:00:31", 1, "/sbin/init"),
        (
            "system.slice/systemd-journald.service",
            "root",
            58_300,
            19_400,
            "00:01:21",
            401,
            "/usr/lib/systemd/systemd-journald",
        ),
        (
            "system.slice/systemd-udevd.service",
            "root",
            25_900,
            7_900,
            "00:00:03",
            438,
            "/usr/lib/systemd/systemd-udevd",
        ),
        (
            "system.slice/systemd-resolved.service",
            "systemd-resolve",
            26_600,
            13_100,
            "00:00:44",
            489,
            "/usr/lib/systemd/systemd-resolved",
        ),
        (
            "system.slice/systemd-timesyncd.service",
            "systemd-timesync",
            91_000,
            7_600,
            "00:00:10",
            503,
            "/usr/lib/systemd/systemd-timesyncd",
        ),
        (
            "system.slice/dbus.service",
            "messagebus",
            10_200,
            5_100,
            "00:00:18",
            515,
            "@dbus-daemon --system --address=systemd:",
        ),
        (
            "system.slice/rsyslog.service",
            "syslog",
            222_400,
            6_700,
            "00:00:39",
            612,
            "/usr/sbin/rsyslogd -n -iNONE",
        ),
        (
            "system.slice/ssh.service",
            "root",
            15_400,
            9_000,
            "00:00:01",
            690,
            "sshd: /usr/sbin/sshd -D [listener]",
        ),
        (
            "system.slice/cron.service",
            "root",
            11_500,
            2_500,
            "00:00:02",
            705,
            "/usr/sbin/cron -f -P",
        ),
        (
            "system.slice/containerd.service",
            "root",
            1_810_000,
            41_200,
            "01:42:11",
            760,
            "/usr/bin/containerd",
        ),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 09-02:41:40 {pid} {cmd}")
    # the leaking worker
    a(
        f"0::/system.slice/order-worker.service worker {java_vsz} {java_rss} "
        f"02:51:44 {'0-00:00:%02d' % (broken_seconds() % 60) if broken else '09-02:39:50'} "
        "1180 /usr/bin/java -Xmx10g -XX:+UseG1GC -jar /opt/orderworker/order-worker.jar"
    )

    # ---- systemd units: ~30, all green EXCEPT order-worker.service which the
    #      OOM killer reaped while broken -> "failed" -> Service Summary CRIT. - #
    a("<<<systemd_units>>>")
    worker_act, worker_sub = ("failed", "failed") if broken else ("active", "running")
    units = [
        ("order-worker.service", worker_act, worker_sub, "Meridian order settlement worker"),
        ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service", "active", "running", "Regular background program processing daemon"),
        ("containerd.service", "active", "running", "containerd container runtime"),
        ("dbus.service", "active", "running", "D-Bus System Message Bus"),
        ("getty@tty1.service", "active", "running", "Getty on tty1"),
        ("irqbalance.service", "active", "running", "irqbalance daemon"),
        ("multipathd.service", "active", "running", "Device-Mapper Multipath Device Controller"),
        (
            "networkd-dispatcher.service",
            "active",
            "running",
            "Dispatcher daemon for systemd-networkd",
        ),
        ("polkit.service", "active", "running", "Authorization Manager"),
        ("rsyslog.service", "active", "running", "System Logging Service"),
        ("snapd.service", "active", "running", "Snap Daemon"),
        ("systemd-journald.service", "active", "running", "Journal Service"),
        ("systemd-logind.service", "active", "running", "User Login Management"),
        ("systemd-networkd.service", "active", "running", "Network Configuration"),
        ("systemd-resolved.service", "active", "running", "Network Name Resolution"),
        ("systemd-timesyncd.service", "active", "running", "Network Time Synchronization"),
        (
            "systemd-udevd.service",
            "active",
            "running",
            "Rule-based Manager for Device Events and Files",
        ),
        ("udisks2.service", "active", "running", "Disk Manager"),
        ("unattended-upgrades.service", "active", "running", "Unattended Upgrades Shutdown"),
        ("user@1000.service", "active", "running", "User Manager for UID 1000"),
        ("apparmor.service", "active", "exited", "Load AppArmor profiles"),
        ("blk-availability.service", "active", "exited", "Availability of block devices"),
        ("console-setup.service", "active", "exited", "Set console font and keymap"),
        ("finalrd.service", "active", "exited", "Create final runtime dir for shutdown pivot root"),
        ("keyboard-setup.service", "active", "exited", "Set the console keyboard layout"),
        (
            "lvm2-monitor.service",
            "active",
            "exited",
            "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or progress polling",
        ),
        ("setvtrgb.service", "active", "exited", "Set console scheme"),
        ("snapd.seeded.service", "active", "exited", "Wait until snapd is fully seeded"),
        ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # ---- scheduled job: the nightly artifact cleanup (green) --------------- #
    a("<<<job>>>")
    a("==> artifact-prune <==")
    a(f"start_time {now - 9 * 3600}")
    a("exit_code 0")
    a("real_time 1:48.7")
    a("user_time 1.20")
    a("system_time 0.60")
    a("max_res_kbytes 88000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence (counters/uptime/incident — see CLAUDE.md)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-app-worker-state.json")


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
        saved = data.get("counters", {})
        restored = 0
        for name, c in _ALL_COUNTERS.items():
            if name in saved:
                c.acc, c.last = saved[name]
                restored += 1
    print(
        f"[state] restored: state={_state!r}, "
        f"{restored}/{len(_ALL_COUNTERS)} counters, uptime continuous"
    )


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
        "tagline": "All green. ~6.5 GiB RAM used of 16, swap empty, worker draining the queue.",
        "effects": [
            "every service OK — the starting picture",
            "Memory: virtual (RAM+swap) usage ~32 %, Committed_AS below the commit limit",
            "order-worker.service active/running, major page faults ~1/s",
        ],
    },
    "degraded": {
        "color": "#f9a825",
        "label": "DEGRADED",
        "tagline": "The heap leak grows. Memory climbs, swap starts filling, the box thrashes — "
        "but the worker is still up. Trigger ~20 min before showtime."
        + (
            f" Auto-escalates (OOM) after {AUTO_BREAK_AFTER_MIN:g} min."
            if AUTO_BREAK_AFTER_MIN > 0
            else ""
        ),
        "effects": [
            "Memory service crosses WARN on Committed_AS (the JVM commits heap past the "
            "12.19 GiB commit limit) — the breadcrumb",
            "Swap usage graph climbs from 0; major page faults climb to hundreds/s (thrash)",
            "virtual (RAM+swap) usage climbs toward — but stays under — the 80 % WARN; "
            "order-worker.service still active/running",
        ],
    },
    "broken": {
        "color": "#c62828",
        "label": "BROKEN",
        "tagline": "Memory is full — the OOM killer reaps the JVM and the worker flaps. "
        + (
            f"Swap pegs / virtual CRIT ramps over ~{BREAK_RAMP_MIN:g} min."
            if BREAK_RAMP_MIN > 0
            else "Instant."
        ),
        "effects": [
            "Memory CRIT: virtual (RAM+swap) usage > 90 % (default levels 80/90) — the headline",
            "order-worker.service FAILED (OOM-killed) -> Systemd Service Summary CRIT — the symptom",
            "the worker flaps: OOM-kill / restart count climbs live (~1 per 90 s)",
            "major page faults pinned (heavy swap thrash), queue throughput collapsed",
            "load elevated but GREEN, CPU not the cause — the AI fuses Memory + Swap + "
            "page faults + the failed unit into 'heap leak, OOM-killed; fix the worker, not the RAM'",
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
    extras = []
    if degraded_seconds() > 0:
        extras.append(
            f"heap leaking for {_fmt_duration(degraded_seconds())} — "
            f"memory pressure ~{pressure() * 100:.0f} %"
        )
    if broken_seconds() > 0:
        extras.append(
            f"OOM-killed {oom_kills()}x (worker flapping for {_fmt_duration(broken_seconds())})"
        )
        if break_ramp() < 1.0:
            extras.append(f"virtual-memory CRIT ramping: {break_ramp() * 100:.0f} %")
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"OOM kill auto-fires in {_fmt_duration(left)}")
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
 h1 {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }}
 h1 b {{ color:#d8dee4; }}
 .state {{ display:inline-block; padding:.4rem 1.1rem; border-radius:.4rem;
          color:#fff; font-weight:700; font-size:1.6rem; letter-spacing:.05em;
          background:{meta["color"]}; }}
 .since {{ color:#9aa4af; margin:.6rem 0 0; }}
 .extra {{ color:#f9a825; margin-top:.3rem; }}
 .cards {{ display:flex; gap:1rem; margin-top:2rem; flex-wrap:wrap; }}
 .card {{ flex:1 1 20rem; border:2px solid #333; border-radius:.6rem;
         padding:1rem 1.2rem; background:#22262b; opacity:.85; }}
 .card.active {{ opacity:1; background:#262b31; box-shadow:0 0 14px rgba(255,255,255,.06); }}
 .card h2 {{ margin:.1rem 0 .4rem; font-size:1.1rem; }}
 .card .tag {{ color:#9aa4af; min-height:2.6rem; margin:.2rem 0 .4rem; }}
 .card ul {{ padding-left:1.2rem; margin:.4rem 0 1rem; }}
 .card li {{ margin:.25rem 0; font-size:.92rem; }}
 .btn {{ display:inline-block; padding:.45rem 1.1rem; border-radius:.4rem;
        color:#fff; text-decoration:none; font-weight:600; }}
 .btn.current {{ background:#444; color:#aaa; cursor:default; }}
 .foot {{ margin-top:2rem; color:#666; font-size:.85rem; }}
</style></head><body>
 <h1>demo control — <b>{HOSTNAME}</b> <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta["label"]}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta["tagline"]}</div>
 {extra_html}
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "worker-demo-ctl/1.0"

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
                "memory_pressure_pct": round(pressure() * 100, 1),
                "leaking_for_s": round(degraded_seconds(), 1),
                "oom_kills": oom_kills(),
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
            print(f"[ctl] -> BROKEN (auto: OOM after {AUTO_BREAK_AFTER_MIN:g} min leaking)")


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(
            f"[boot] auto-escalation: degraded -> broken (OOM) after {AUTO_BREAK_AFTER_MIN:g} min"
        )
    print(
        f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
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
