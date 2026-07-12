#!/usr/bin/env python3
"""Meridian Retail demo host: fileserver-01 — Samba/NFS shared storage.

An Ubuntu 24.04 file server (`smbd`/`nmbd` for the Windows clients, `nfsd` for
the Linux estate) that exports home directories, shared drives and an
upload/ingest spool. The incident is a classic operational footgun: a bad
config push disabled log rotation on the ingest pipeline, so a runaway batch
importer keeps appending to one log file on the data volume. The file grows
without bound, the data filesystem `/srv/shares` fills, and the Filesystem
service crosses its levels. *"Explain with AI"* fuses the steep fill SLOPE +
projected time-to-full + the single offending growing file (fileinfo) into the
root cause: a log/spool that stopped rotating — rotate/clear it, don't just
delete random files or grow the volume.

Three states (the timeline is part of the story):

  healthy   /srv/shares ~55 % used, slow normal growth + cleanup sawtooth. all
            green. The runaway file is small and rotates normally.
  degraded  the runaway starts: usage climbs steadily from ~55 % toward ~78 %.
            The growth TREND becomes steep (a Filesystem trend rule -> trend
            WARN / projected fill) while magnitude is still green/edging — the
            breadcrumb. The offending log file grows fast in fileinfo. Trigger
            ~18 min before showtime.
  broken    usage crosses 90 % and is STILL GROWING LIVE across re-polls ->
            Filesystem magnitude CRIT. The runaway log is large in fileinfo.
            One root cause, low noise — root / and everything else stay green.

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env (see also AGENT_PORT/HTTP_PORT/START_STATE/STATE_FILE):
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before the fill crosses 90 %
                 auto-fires (default: 18; 0 disables)
  FILL_RAMP_MIN  minutes for the runaway to drive usage degraded->broken
                 (default: 16; the Filesystem graph climbs over this window)
  BREAK_RAMP_MIN minutes for the broken impact (final push over 90 %) to reach
                 full force (default: 4; 0 = instant)
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
from typing import Any

HOSTNAME = os.environ.get("CMK_HOSTNAME", "fileserver-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "18"))
FILL_RAMP_MIN = float(os.environ.get("FILL_RAMP_MIN", "16"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "4"))

START = time.time()
UPTIME_OFFSET = 12 * 86400  # pretend the host has been up ~12 days

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the runaway started (degraded or broken) -> drives the rising fill curve
_degraded_since: float | None = None if _state == "healthy" else START
# when the volume crossed into the broken push -> drives the >90 % CRIT
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


# The single driver of the whole incident: 0 (healthy) .. 1 (volume full).
#   * the runaway fills the volume over FILL_RAMP_MIN while degraded, but only
#     up to 0.74 — enough to make the growth TREND steep (a trend rule -> WARN /
#     projected fill) and the usage edge up, but NOT enough to trip the 80/90 %
#     magnitude levels. That stays the *broken*-state headline.
#   * broken pushes pressure 0.74 -> 1.0 over the break ramp: used % crosses
#     90 % -> Filesystem magnitude CRIT, and keeps growing live.
def pressure() -> float:
    ds = degraded_seconds()
    if ds <= 0:
        deg = 0.0
    elif FILL_RAMP_MIN <= 0:
        deg = 1.0
    else:
        deg = min(1.0, ds / (FILL_RAMP_MIN * 60.0))
    p = 0.74 * deg
    if broken_seconds() > 0:
        p = max(p, 0.74 + 0.26 * break_ramp(1.0))
    return max(0.0, min(1.0, p))


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


# /proc/stat jiffies: 100 Hz * 4 CPUs = ~400 ticks/s. A file server is mostly
# idle; the runaway adds a little write/syscall load (system up) but never a
# CPU alert — the story is the filesystem, not the CPU. Keep noise down.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(28))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(34))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(330))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(8))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(2600))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(4))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(0.4))

# two disks: sda = OS SSD (root + swap), sdb = the data array holding /srv. The
# runaway appends to /srv on sdb -> sdb write IOs climb a little (corroboration,
# stays calm — this is sequential append, not a thrash).
SDA = {
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(5)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(3)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(14)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(11)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(16)),
}
SDB = {
    "rd_ios": Counter("sdb.rd_ios", phase=1.0, start=_aged(40)),
    "rd_ticks": Counter("sdb.rd_ticks", phase=1.2, start=_aged(180)),
    "wr_ios": Counter("sdb.wr_ios", phase=1.4, start=_aged(55)),
    "wr_ticks": Counter("sdb.wr_ticks", phase=1.6, start=_aged(240)),
    "io_ticks": Counter("sdb.io_ticks", phase=1.8, amp=0.05, start=_aged(120)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(420_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(680_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(1100))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(1300))


# --------------------------------------------------------------------------- #
#  SMART (two healthy SSDs, for parity with a real agent dump). Stay green
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
                    "raw": {"value": 17},
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
                    "value": 97,
                    "thresh": 5,
                    "raw": {"value": 71},
                },
            ]
        },
    }
    return json.dumps(doc, separators=(",", ":"))


# --------------------------------------------------------------------------- #
#  Filesystem usage — the whole story lives here.
#  Sizes in kB (df_v2 columns are kB). root / and /home stay green; the data
#  volume /srv/shares fills under `pressure`.
# --------------------------------------------------------------------------- #
SRV_SIZE = 2_147_483_648  # 2 TiB data array (kB)
ROOT_SIZE = 41_943_040  # 40 GiB OS SSD (kB)
HOME_SIZE = 524_288_000  # 500 GiB home dirs (kB)


def srv_used_kb(now: float) -> int:
    """The data volume /srv/shares — climbs with the incident pressure.

    Healthy baseline ~55 % with a normal slow secular creep + a cleanup
    sawtooth (a retention prune every ~30 min). The runaway adds a steady
    linear fill on top, driven by `pressure` 0..1 mapping ~55 % -> ~97 %.
    Modelled as a pure function of wall clock so the curve is continuous
    across re-polls and restarts.
    """
    p = pressure()
    uptime = now - START + UPTIME_OFFSET
    base_frac = 0.55
    # normal life: slow forever-growth (capped) + a 30-min retention sawtooth.
    # As the runaway takes over (pressure up) the cleanup can no longer keep up,
    # so the sawtooth and jitter fade out — the curve goes monotone-up while
    # broken (the incident outpaces retention), no spurious backwards dips.
    quiet = max(0.0, 1.0 - p / 0.30)  # 1 healthy .. 0 by p=0.3
    secular = min(0.012, uptime * 1.0e-8)  # creeps up to +1.2 %
    sawtooth = 0.010 * (1.0 - (now % 1800.0) / 1800.0) * quiet  # cleanup teeth
    wobble = gauge("fs.srv", 0.0, amp_abs=0.0015, period=700) * quiet
    # the incident: linear fill 55 % -> ~97 % across the pressure range
    incident = (0.97 - base_frac) * p
    frac = base_frac + secular + sawtooth + wobble + incident
    frac = max(0.50, min(0.995, frac))
    return int(SRV_SIZE * frac)


def root_used_kb(now: float) -> int:
    """OS SSD / — green, slow log creep + daily logrotate trim."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0
    base = 13_107_200  # ~12.5 GiB of 40
    logs = 1_572_864 * ((now % day) / day)  # 0..1.5 GiB daily
    growth = min(1_048_576, uptime * 0.03)
    return int(base + logs + growth + gauge("fs.root", 0.0, amp_abs=70_000, period=1500))


def home_used_kb(now: float) -> int:
    """/home (home directories) — green, slow steady growth + small sawtooth."""
    uptime = now - START + UPTIME_OFFSET
    base = 246_000_000  # ~47 % of 500 GiB
    spool = 4_194_304 * ((now % 3600.0) / 3600.0)  # hourly user churn
    growth = min(8_388_608, uptime * 2.0)
    return int(base + spool + growth + gauge("fs.home", 0.0, amp_abs=300_000, period=900))


def runaway_log_bytes(now: float) -> int:
    """The offending ingest log file on /srv/shares (fileinfo).

    Healthy: a small, normally-rotated log (~a few MB). Degraded/broken: log
    rotation is disabled, so it grows fast and linearly with the incident —
    the single file the AI can name. Grows live across re-polls.
    """
    p = pressure()
    base = 6 * 1024 * 1024  # ~6 MB rotated baseline
    # at full pressure the file has eaten ~520 GB of the spool fill
    runaway = int(560_000_000_000 * p)
    jitter = int(gauge("file.ingest", 0.0, amp_abs=2_000_000, period=400))
    return max(base, base + runaway + jitter)


def fill_rate_gb_per_h() -> float:
    """Current data-volume fill rate in GB/h (for the /admin UI + story)."""
    if FILL_RAMP_MIN <= 0:
        return 0.0
    # incident maps 0.42 of the volume (0.55->0.97) over FILL_RAMP_MIN minutes
    span_kb = (0.97 - 0.55) * SRV_SIZE
    per_min = span_kb / FILL_RAMP_MIN
    if degraded_seconds() <= 0:
        return 0.0
    return per_min * 60.0 / (1024.0 * 1024.0)  # kB/min -> GB/h


def hours_until_full() -> float | None:
    rate = fill_rate_gb_per_h()
    if rate <= 0:
        return None
    free_kb = max(0, SRV_SIZE - srv_used_kb(time.time()))
    free_gb = free_kb / (1024.0 * 1024.0)
    return free_gb / rate


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4
    p = pressure()

    # ---- filesystem usage (the story) ------------------------------------- #
    root_used = root_used_kb(now)
    home_used = home_used_kb(now)
    srv_used = srv_used_kb(now)
    runaway_log_bytes(now)

    # ---- memory: a calm file server. Mostly page cache (it serves files),
    #      plenty free, swap empty. Stays green in all states. --------------- #
    mem_total = 16_384_000  # kB
    swap_total = 4_194_300
    commit_limit = swap_total + mem_total // 2
    cached = int(gauge("mem.cached", 9_400_000, amp_frac=0.03, phase=0.4, period=1500))
    buffers = int(gauge("mem.buffers", 540_000, amp_frac=0.05, phase=0.9, period=1100))
    sreclaim = 612_352
    swapcached = 0
    caches = cached + buffers + swapcached + sreclaim
    anon = int(gauge("mem.anon", 2_350_000, amp_frac=0.02, phase=1.3, period=1700))
    mem_used_real = anon + 700_000  # anon + slab/pagetables est.
    mem_free = max(180_000, mem_total - mem_used_real - caches)
    swap_free = swap_total
    committed = int(gauge("mem.committed", 4_900_000, amp_frac=0.01, phase=1.2, period=1700))

    shmem = 65_536
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = max(mem_free, mem_free + file_lru + sreclaim)
    a_anon = int(anon_lru * 0.58)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.46)
    i_file = file_lru - a_file
    slab = sreclaim + 138_240
    threads = int(gauge("mem.threads", 280, amp_abs=8, phase=2.0, period=2000))
    kernel_stack = threads * 16
    # the runaway append keeps a few dirty pages around, but they drain fine —
    # not a thrash (contrast the dying-disk host). Stays small and green.
    dirty = max(
        6_144, int(gauge("mem.dirty", 22_000 + 30_000 * p, amp_frac=0.12, phase=2.0, period=800))
    )

    # ---- load: a file server idles; the runaway adds a touch. Stays GREEN
    #      (15-min << 20 WARN per-core). Keep noise down — fs is the story. -- #
    base_l = _lerp(0.5, 1.8, p)
    l1 = round(base_l * gauge("load1", 1.0, amp_frac=0.30, phase=0.2, period=300), 2)
    l5 = round(base_l * 0.9 * gauge("load5", 1.0, amp_frac=0.16, phase=1.0, period=900), 2)
    l15 = round(base_l * 0.82 * gauge("load15", 1.0, amp_frac=0.08, phase=2.0, period=2400), 2)
    runnable = 1 + round(p)
    total_procs = round(_lerp(248, 262, p))

    # ---- /proc/stat: the runaway append nudges system + a little iowait. --- #
    user = C_USER.sample(_lerp(28, 40, p))
    system = C_SYSTEM.sample(_lerp(34, 70, p))
    idle = C_IDLE.sample(_lerp(330, 286, p))
    iowait = C_IOWAIT.sample(_lerp(8, 24, p))
    pgmaj_rate = _lerp(0.4, 1.2, p)

    # ---- diskstat: sda = OS SSD (calm), sdb = data array (write climbs). --- #
    sda_rd = SDA["rd_ios"].sample(_lerp(5, 7, p))
    sda_rdt = SDA["rd_ticks"].sample(_lerp(3, 4, p))
    sda_wr = SDA["wr_ios"].sample(_lerp(14, 18, p))
    sda_wrt = SDA["wr_ticks"].sample(_lerp(11, 14, p))
    sda_iot = SDA["io_ticks"].sample(_lerp(16, 22, p))
    sdb_rd = SDB["rd_ios"].sample(_lerp(40, 48, p))
    sdb_rdt = SDB["rd_ticks"].sample(_lerp(180, 210, p))
    sdb_wr = SDB["wr_ios"].sample(_lerp(55, 220, p))  # the runaway append
    sdb_wrt = SDB["wr_ticks"].sample(_lerp(240, 520, p))
    sdb_iot = SDB["io_ticks"].sample(_lerp(120, 360, p))

    rx_bytes = C_RX_B.sample(420_000)
    tx_bytes = C_TX_B.sample(680_000)
    rx_pkts = C_RX_P.sample(1100)
    tx_pkts = C_TX_P.sample(1300)

    sda_temp = round(gauge("smart.sda.temp", 30, amp_abs=1.2, phase=2.1, period=1100))
    sdb_temp = round(gauge("smart.sdb.temp", 32, amp_abs=1.3, phase=0.7, period=1300))
    sda_smart = _smart_json(
        "/dev/sda",
        "SAMSUNG MZ7L3480HCHQ-00A07",
        "S6KSNX0T301277",
        int(uptime / 3600) + 22000,
        sda_temp,
    )
    sdb_smart = _smart_json(
        "/dev/sdb",
        "INTEL SSDSC2KB038T8",
        "PHYF902300J53P8EGN",
        int(uptime / 3600) + 26000,
        sdb_temp,
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
                        "uuid": "7c44a9e2-0db1-4f5e-8a17-3e6c2b1d9f44",
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
    a(f'/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="{AGENT_VERSION}"')

    a("<<<df_v2>>>")
    a(
        f"/dev/sda1 ext4 {ROOT_SIZE} {root_used} {ROOT_SIZE - root_used} "
        f"{round(root_used / ROOT_SIZE * 100)}% /"
    )
    a(
        f"/dev/mapper/vghome-home ext4 {HOME_SIZE} {home_used} {HOME_SIZE - home_used} "
        f"{round(home_used / HOME_SIZE * 100)}% /home"
    )
    a(
        f"/dev/mapper/vgdata-shares xfs {SRV_SIZE} {srv_used} {SRV_SIZE - srv_used} "
        f"{round(srv_used / SRV_SIZE * 100)}% /srv/shares"
    )
    a("[df_inodes_start]")
    a(f"/dev/sda1 ext4 2621440 268331 {2621440 - 268331} 11% /")
    a(f"/dev/mapper/vghome-home ext4 32768000 4118233 {32768000 - 4118233} 13% /home")
    # a data volume holds many medium files; inode use modest and NOT the alarm
    a(f"/dev/mapper/vgdata-shares xfs 134217728 9438211 {134217728 - 9438211} 8% /srv/shares")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/mapper/vghome-home /home ext4 rw,relatime 0 0")
    a("/dev/mapper/vgdata-shares /srv/shares xfs rw,noatime,nodiratime,attr2,inode64 0 0")

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
    a("Mapped:         286720 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     138240 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     42240 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    61440 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         13312 kB")
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
    a("DirectMap4k:    243596 kB")
    a("DirectMap2M:    6049792 kB")
    a("DirectMap1G:    11534336 kB")

    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {26000 + C_PROC.sample(4) % 9999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.4)}.00")

    # sawtooths 0->34min (poll interval), anchored to boot so it's continuous
    # across restarts and independent of push-lagged payload timestamps.
    last_sync = now - int((now - START) % 2048)
    sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = random.randint(-1600, 1600)
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.56 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: B97D5A38")
    a("    Precision: 1us (-25)")
    a("Root distance: 11.842ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 18.114ms")
    a(f"       Jitter: {random.randint(700, 2900) / 1000:.3f}ms")
    a(f" Packet count: {640 + int((time.time() - START) / 2048)}")
    a("    Frequency: +7.842ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a(
        "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
        "RootDelay=8.911ms, RootDispersion=1.144ms, Reference=B97D5A38, "
        f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
        f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
        "Ignored=no, PacketCount=61, Jitter=1.041ms }"
    )
    a("Timezone=UTC")

    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(2600)}")
    a(f"processes {C_PROC.sample(4)}")
    a(f"pgmajfault {C_PGMAJ.sample(pgmaj_rate)}")

    a("<<<diskstat>>>")
    a(str(now))
    a(
        f"8 0 sda {sda_rd} 0 {sda_rd * 20} {sda_rdt} {sda_wr} 0 "
        f"{sda_wr * 32} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0"
    )
    a(
        f"8 16 sdb {sdb_rd} 0 {sdb_rd * 64} {sdb_rdt} {sdb_wr} 0 "
        f"{sdb_wr * 80} {sdb_wrt} 0 {sdb_iot} {sdb_iot * 2} 0 0 0 0"
    )

    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a(
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
        "state UP group default qlen 1000"
    )
    a("    link/ether 02:42:ac:11:00:31 brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:31")

    a("<<<tcp_conn_stats>>>")
    # smb/nfs clients keep a fair pile of established connections
    a(f"01 {round(gauge('tcp.estab', 64, amp_abs=10, phase=0.9, period=700))}")
    a(f"02 {random.randint(0, 2)}")
    a(f"06 {round(gauge('tcp.timewait', 18, amp_abs=5, phase=2.4, period=500))}")
    a("0A 6")

    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)
    a(sdb_smart)

    # ---- fileinfo: the offending runaway ingest log on /srv/shares -------- #
    #      Legacy format: reftime line, then "<path>\t<size_bytes>\t<mtime>".
    #      Discovery-based; no alert by default — but it's the single file the
    #      AI can NAME, and it grows live. We also list two stable companions
    #      so the service set looks like a real fileinfo rule.
    a("<<<fileinfo>>>")
    a(str(now))
    a(f"/srv/shares/ingest/spool/import-batch.log\t{runaway_log_bytes(now)}\t{now - 1}")
    a(f"/srv/shares/ingest/spool/import-batch.log.1\t{6 * 1024 * 1024}\t{now - 86400}")
    a(f"/var/log/samba/log.smbd\t{18 * 1024 * 1024 + random.randint(0, 65536)}\t{now - 12}")

    # ---- processes: smbd/nmbd + nfsd + the runaway importer + daemons ----- #
    importer_cpu = (
        f"{2 + int(degraded_seconds() // 3600):02d}:"
        f"{int((degraded_seconds() % 3600) // 60):02d}:"
        f"{int(degraded_seconds() % 60):02d}"
    )
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_000, 12_600, "00:00:41", 1, "/sbin/init"),
        (
            "system.slice/systemd-journald.service",
            "root",
            58_300,
            21_100,
            "00:01:48",
            401,
            "/usr/lib/systemd/systemd-journald",
        ),
        (
            "system.slice/systemd-udevd.service",
            "root",
            25_900,
            7_600,
            "00:00:04",
            438,
            "/usr/lib/systemd/systemd-udevd",
        ),
        (
            "system.slice/systemd-resolved.service",
            "systemd-resolve",
            26_600,
            13_400,
            "00:00:51",
            489,
            "/usr/lib/systemd/systemd-resolved",
        ),
        (
            "system.slice/systemd-timesyncd.service",
            "systemd-timesync",
            91_000,
            7_400,
            "00:00:12",
            503,
            "/usr/lib/systemd/systemd-timesyncd",
        ),
        (
            "system.slice/dbus.service",
            "messagebus",
            10_200,
            5_300,
            "00:00:22",
            515,
            "@dbus-daemon --system --address=systemd:",
        ),
        (
            "system.slice/rsyslog.service",
            "syslog",
            222_400,
            7_100,
            "00:00:48",
            612,
            "/usr/sbin/rsyslogd -n -iNONE",
        ),
        (
            "system.slice/ssh.service",
            "root",
            15_400,
            9_200,
            "00:00:01",
            690,
            "sshd: /usr/sbin/sshd -D [listener]",
        ),
        (
            "system.slice/cron.service",
            "root",
            11_500,
            2_600,
            "00:00:02",
            705,
            "/usr/sbin/cron -f -P",
        ),
        # the file-sharing daemons (the role of this host)
        (
            "system.slice/nmbd.service",
            "root",
            88_400,
            8_900,
            "00:02:14",
            820,
            "/usr/sbin/nmbd --foreground --no-process-group",
        ),
        (
            "system.slice/smbd.service",
            "root",
            412_600,
            31_800,
            "00:41:53",
            842,
            "/usr/sbin/smbd --foreground --no-process-group",
        ),
        (
            "system.slice/smbd.service",
            "root",
            414_900,
            18_400,
            "00:08:21",
            901,
            "/usr/sbin/smbd --foreground --no-process-group",
        ),
        ("system.slice/nfs-server.service", "root", 0, 0, "00:12:38", 870, "[nfsd]"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 12-04:18:02 {pid} {cmd}")
    # the runaway batch importer (only present once the incident is running)
    if p > 0:
        a(
            f"0::/system.slice/ingest-importer.service ingest 198400 24600 "
            f"{importer_cpu} {_fmt_elapsed(degraded_seconds())} 1444 "
            "/usr/bin/python3 /opt/ingest/import_batch.py --watch /srv/shares/ingest/spool"
        )

    # ---- systemd units: ~30, all green. nmbd/smbd/nfs-server active. The
    #      filesystem story has NO failed unit — the symptom is the df CRIT. -- #
    a("<<<systemd_units>>>")
    units = [
        ("smbd.service", "active", "running", "Samba SMB Daemon"),
        ("nmbd.service", "active", "running", "Samba NMB Daemon"),
        ("nfs-server.service", "active", "running", "NFS server and services"),
        ("rpcbind.service", "active", "running", "RPC bind portmap service"),
        ("rpc-statd.service", "active", "running", "NFS status monitor for NFSv2/3 locking"),
        ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service", "active", "running", "Regular background program processing daemon"),
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
        ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # ---- scheduled job: the nightly retention prune (green) --------------- #
    a("<<<job>>>")
    a("==> share-retention-prune <==")
    a(f"start_time {now - 7 * 3600}")
    a("exit_code 0")
    a("real_time 3:22.4")
    a("user_time 2.10")
    a("system_time 4.80")
    a("max_res_kbytes 64000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


def _fmt_elapsed(seconds: float) -> str:
    """ps ELAPSED-style [[DD-]HH:]MM:SS."""
    s = int(max(0.0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}-{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
#  State persistence (counters/uptime/incident — see CLAUDE.md)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-fileserver-state.json")


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
        "tagline": "All green. /srv/shares ~55 % used, normal growth + cleanup sawtooth, "
        "Samba/NFS serving happily.",
        "effects": [
            "every service OK — the starting picture",
            "Filesystem /srv/shares ~55 % used, growth flat (cleanup keeps up)",
            "the ingest log rotates normally (small in fileinfo)",
        ],
    },
    "degraded": {
        "color": "#f9a825",
        "label": "DEGRADED",
        "tagline": "A bad config push disabled log rotation; a runaway importer fills "
        "/srv/shares. Usage climbs, the growth trend turns steep — but it's "
        "still under the 80 % level. Trigger ~18 min before showtime."
        + (
            f" Auto-escalates (crosses 90 %) after {AUTO_BREAK_AFTER_MIN:g} min."
            if AUTO_BREAK_AFTER_MIN > 0
            else ""
        ),
        "effects": [
            "Filesystem /srv/shares usage climbs steadily from ~55 % toward ~78 %",
            "the growth TREND becomes steep -> trend WARN / projected fill (needs a "
            "Filesystem trend rule — see README) — the breadcrumb",
            "the runaway log file (import-batch.log) grows fast in fileinfo; "
            "magnitude still GREEN/edging; root / and /home stay green",
        ],
    },
    "broken": {
        "color": "#c62828",
        "label": "BROKEN",
        "tagline": "The volume is filling past 90 % and STILL growing live. "
        + (f"Crosses 90 % over ~{BREAK_RAMP_MIN:g} min." if BREAK_RAMP_MIN > 0 else "Instant."),
        "effects": [
            "Filesystem /srv/shares > 90 % used (default magnitude levels 80/90) -> "
            "CRIT — the headline",
            "usage keeps GROWING across re-polls (the incident is live, not static)",
            "import-batch.log is large in fileinfo — the single file to blame",
            "root /, /home, memory, CPU, network all GREEN — the AI fuses the steep "
            "fill slope + time-to-full + the one growing file into 'rotation stopped on "
            "the ingest log; rotate/clear it, don't just grow the volume'",
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


def _srv_pct() -> float:
    return srv_used_kb(time.time()) / SRV_SIZE * 100.0


def _admin_page() -> str:
    state = get_state()
    meta = STATE_META[state]
    extras = []
    extras.append(f"/srv/shares now <b>{_srv_pct():.1f} %</b> used")
    if degraded_seconds() > 0:
        rate = fill_rate_gb_per_h()
        ttf = hours_until_full()
        msg = f"runaway filling for {_fmt_duration(degraded_seconds())} — ~{rate:.0f} GB/h"
        if ttf is not None:
            msg += f", full in ~{_fmt_duration(ttf * 3600)}"
        extras.append(msg)
    if broken_seconds() > 0 and break_ramp() < 1.0:
        extras.append(f"crossing 90 %: {break_ramp() * 100:.0f} %")
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"crosses 90 % (CRIT) auto-fires in {_fmt_duration(left)}")
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
 <h1>demo control — <b>{HOSTNAME}</b>
 <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta["label"]}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta["tagline"]}</div>
 {extra_html}
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "fileserver-demo-ctl/1.0"

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
        ttf = hours_until_full()
        return self._send(
            200,
            {
                "state": state,
                "in_state_for_s": round(state_since_seconds(), 1),
                "srv_shares_used_pct": round(_srv_pct(), 1),
                "fill_rate_gb_per_h": round(fill_rate_gb_per_h(), 1),
                "hours_until_full": round(ttf, 2) if ttf is not None else None,
                "runaway_log_bytes": runaway_log_bytes(time.time()),
                "filling_for_s": round(degraded_seconds(), 1),
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
            print(
                f"[ctl] -> BROKEN (auto: /srv/shares crossed 90 % after "
                f"{AUTO_BREAK_AFTER_MIN:g} min filling)"
            )


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(
            f"[boot] auto-escalation: degraded -> broken (>90 %) after {AUTO_BREAK_AFTER_MIN:g} min"
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
