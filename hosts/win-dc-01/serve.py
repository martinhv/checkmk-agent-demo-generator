#!/usr/bin/env python3
r"""Meridian Retail demo host: win-dc-01 — Windows Server 2022 AD domain controller.

The one Windows box in the estate (see ../FLEET.md). Same plaintext-TCP trick
as the Linux demos (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration), but the agent payload is the
**Windows** format (verified against a real 2.3 Windows agent dump:
check_mk/tests/gui_e2e/data/windows-2.3.0p10): `<<<df:sep(9)>>>`,
`<<<wmi_cpuload:sep(124)>>>`, the Windows `<<<mem>>>` keys, `<<<services>>>`,
`<<<ps:sep(9)>>>`, `<<<winperf_processor>>>`, `<<<winperf_phydisk>>>`,
`<<<winperf_if>>>`, `<<<systemtime>>>`.

Incident (ONE root cause, low noise): the in-house **Meridian Backup Agent**
service crashes, so its job that trims `C:\` (the Windows Update download
cache, un-truncated NTDS/ESE logs, auto-archived event logs) stops running ->
the **system drive C: fills up**. Symptom: Filesystem C:/ crosses the default
magnitude levels (80 % WARN / 90 % CRIT). Disk IO shows the write-side of the
fill; fileinfo shows the culprit files growing at exactly the fill rate. The
AI fuses the stopped service + the steep C: slope + the growing cache files
into "the backup/cleanup service died; C: is filling — restart it and clear
SoftwareDistribution".

Three states:
  healthy   C: ~54 % used, every service running. all green.
  degraded  MeridianBackupAgent stopped (root cause); C: climbs 54 -> ~85 %
            (Filesystem C:/ WARN). The stopped service shows in <<<services>>>
            (CRIT only if you add the "Windows Services" monitoring rule for
            it — documented) and its process vanishes from <<<ps>>>. Trigger
            ~20 min before showtime.
  broken    C: > 90 % and still growing live -> Filesystem C:/ CRIT. When
            fired straight from healthy the fill RAMPS from wherever it stood
            (no one-poll cliff) over BREAK_RAMP_MIN.

Rate sections (winperf_*) follow the Linux hosts' counter discipline: every
counter Checkmk rate-checks is a strictly monotonic accumulator integrating a
state-dependent instantaneous rate (harmonic + AR(1) wobble, clamped), seeded
to the 41-day fake uptime and persisted to STATE_FILE so a redeploy never
makes a counter go backwards. Cumulative values nothing rate-checks (ps CPU
times, cosmetic winperf rows) are pure monotonic functions of wall clock —
restart-continuous by construction. Stdlib only.

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
from typing import Any

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
CPU_CORES = 4

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
_degraded_since: float | None = None if _state == "healthy" else START
_broken_since: float | None = None if _state != "broken" else START
_state_since: float = START
# fill pressure captured the moment `broken` fires: the break ramp continues
# from HERE instead of cliff-jumping to 0.78 (matters for break-from-healthy)
_pressure_at_break: float = 0.78
# when the MeridianBackupAgent process (re)started -> its ps age
_backup_since: float = START - UPTIME_OFFSET
# when its cleanup job last trimmed the WU cache -> healthy fileinfo mtimes
_cleanup_ran: float = 0.0


def get_state() -> str:
    with _state_lock:
        return _state


def set_state(value: str) -> None:
    global \
        _state, \
        _degraded_since, \
        _broken_since, \
        _state_since, \
        _pressure_at_break, \
        _backup_since, \
        _cleanup_ran
    with _state_lock:
        now = time.time()
        prev = _state
        if value != _state:
            _state_since = now
        _state = value
        if value == "healthy":
            if prev != "healthy":
                _backup_since = now  # service restarted -> young process
                _cleanup_ran = now  # its cleanup job trimmed the cache files
            _degraded_since = None
        elif _degraded_since is None:
            _degraded_since = now
        if value == "broken":
            if _broken_since is None:
                ds = 0.0 if _degraded_since is None else now - _degraded_since
                _pressure_at_break = _deg_pressure(ds)
                _broken_since = now
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


def _lerp(healthy: float, broken: float, r: float) -> float:
    return healthy + (broken - healthy) * r


def _deg_pressure(ds: float) -> float:
    """Fill pressure from the degraded phase alone: 0 -> 0.78 over LEAK_FILL_MIN."""
    if ds <= 0:
        return 0.0
    if LEAK_FILL_MIN <= 0:
        return 0.78
    return 0.78 * min(1.0, ds / (LEAK_FILL_MIN * 60.0))


def pressure() -> float:
    """0 (healthy) .. 1 (C: full). Fills to 0.78 while degraded (C: WARN);
    broken ramps from wherever the fill stood when it fired to 1.0 over
    BREAK_RAMP_MIN — smooth even when `break` fires straight from healthy."""
    with _state_lock:
        now = time.time()
        ds = 0.0 if _degraded_since is None else now - _degraded_since
        bs = 0.0 if _broken_since is None else now - _broken_since
        p0 = _pressure_at_break
    p = _deg_pressure(ds)
    if bs > 0:
        ramp = 1.0 if BREAK_RAMP_MIN <= 0 else min(1.0, bs / (BREAK_RAMP_MIN * 60.0))
        p = max(p, _lerp(p0, 1.0, ramp))
    return max(0.0, min(1.0, p))


def c_fill_rate_kb_s() -> float:
    """Instantaneous slope of the C: fill in kB/s. Feeds the write side of
    winperf_phydisk so Disk IO corroborates the df story."""
    with _state_lock:
        now = time.time()
        ds = 0.0 if _degraded_since is None else now - _degraded_since
        bs = 0.0 if _broken_since is None else now - _broken_since
        p0 = _pressure_at_break
    if bs > 0:
        if BREAK_RAMP_MIN <= 0 or bs >= BREAK_RAMP_MIN * 60.0:
            return 0.0
        return C_FILL_KB * (1.0 - p0) / (BREAK_RAMP_MIN * 60.0)
    if ds > 0 and LEAK_FILL_MIN > 0 and ds < LEAK_FILL_MIN * 60.0:
        return C_FILL_KB * 0.78 / (LEAK_FILL_MIN * 60.0)
    return 0.0


def disk_dying() -> bool:
    """The backup/cleanup service is down once we leave healthy."""
    return get_state() in ("degraded", "broken")


# --------------------------------------------------------------------------- #
#  Autocorrelated gauges + monotonic counters (same discipline as the Linux
#  demo hosts: harmonic + AR(1) wobble, long incommensurate periods, no
#  white noise, no aliasing)
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


_ALL_COUNTERS: dict[str, Counter] = {}


class Counter:
    """A strictly monotonic counter integrating a caller-supplied rate.

    Checkmk derives rates as delta(counter)/delta(time) with
    raise_overflow=True (winperf_phydisk's update_value_and_calc_rate, the
    interface/CPU get_rate calls), so the counter must never decrease even
    when the break/heal toggle changes the underlying rate. We integrate the
    current rate over the time since the last sample; flipping state changes
    the slope from now on, never the accumulated value. The wobble is clamped
    to [-1, 1], so the instantaneous rate stays in rate*[1-amp, 1+amp] > 0.
    Persisted by stable name (save_state) so a redeploy never resets it."""

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
    """Start value so a counter looks like it has run for the fake uptime."""
    return rate_per_s * UPTIME_OFFSET


def _run_ticks(
    rate_per_s: float, now: float, phase: float = 0.0, amp: float = 0.25, period: float = 900.0
) -> int:
    """Cumulative counter as a PURE function of wall clock: `rate` since boot
    with a gentle deterministic rate wobble (the closed-form integral of two
    slow sines — strictly monotonic for amp < 1, continuous across restarts
    without persistence). Used for values Checkmk never rate-checks with a
    value store we must protect (ps CPU times, cosmetic winperf rows) and for
    tiny parsed counters where a deterministic slope is fine (nucast pkts)."""
    om = 2.0 * math.pi / period
    t = now - (START - UPTIME_OFFSET)
    wob = -(amp * 0.7 / om) * math.cos(om * now + phase) - (amp * 0.3 / (0.41 * om)) * math.cos(
        0.41 * om * now + 0.6 * phase
    )
    return int(rate_per_s * (t + wob))


# --------------------------------------------------------------------------- #
#  C: fill model — df, fileinfo and Disk IO all derive from ONE variable
#  (pressure / c_fill_extra_kb), so the growing files sum to the C: delta.
# --------------------------------------------------------------------------- #
C_SIZE_KB = 125_827_068  # 120 GiB system drive
C_BASE_KB = 66_700_000  # healthy used (~54 % with the secular term)
C_FILL_KB = 50_100_000  # full incident fill: 54 % -> ~93 %


def c_fill_extra_kb() -> int:
    """kB piled onto C: by the incident — THE shared variable: df adds it to
    the healthy base, the fileinfo growing files split it (shares sum to 1)."""
    return int(C_FILL_KB * pressure())


def c_drive_used_kb(now: float) -> int:
    """Used kB on C: (size ~120 GiB). Healthy ~53 %; once the cleanup service
    dies the WU cache + NTDS logs pile up, filling toward >90 %. Pure function
    of pressure + a slow secular term, with a small wander — continuous across
    re-polls and restarts."""
    secular = min(1_500_000, (now - START + UPTIME_OFFSET) * 0.02)
    return int(
        C_BASE_KB + c_fill_extra_kb() + secular + gauge("c.used", 0, amp_abs=90_000, period=1500)
    )


# The files the dead cleanup service would have trimmed. share = fraction of
# the C: fill this file carries; shares sum to 1.0 so
# sum(growing-file growth) == the df delta at every poll.
GROWING_FILES = (
    (
        "C:\\Windows\\SoftwareDistribution\\Download\\cache.cab",
        1_180_000_000,
        0.72,
    ),  # ~26 MB/s degraded
    (
        "C:\\Windows\\SoftwareDistribution\\Download\\windows10.0-kb5062572-x64.psf",
        640_000_000,
        0.14,
    ),
    (
        "C:\\Windows\\System32\\winevt\\Logs\\Security.evtx",
        268_435_456,
        0.09,
    ),  # audit log, un-archived
    ("C:\\Windows\\NTDS\\edb.log", 10_485_760, 0.05),  # ESE logs, no backup truncation
)
_SEC_EVTX_EPOCH = 1_767_225_600  # security-log growth anchor (2026-01-01)
_NTDS_DIT_EPOCH = 1_700_000_000  # slow ntds.dit growth anchor


def _last_daily(now: float, hour: int, minute: int) -> int:
    """Most recent occurrence of hh:mm UTC — the cleanup task's schedule."""
    midnight = int(now) - int(now) % 86400
    cand = midnight + hour * 3600 + minute * 60
    return cand if cand <= now else cand - 86400


def _ntds_checkpoint(now: float) -> int:
    """Last ESE checkpoint that flushed ntds.dit: a ~30-min schedule with a
    deterministic per-period jitter, so the fileinfo age grows 0 -> ~30 min
    and RESETS instead of sitting at a constant offset from wall clock."""
    idx = int(now) // 1800
    for i in (idx, idx - 1):
        cand = i * 1800 + int(random.Random(i).uniform(35, 155))
        if cand <= now:
            return cand
    return (idx - 1) * 1800


def growing_files(now: float) -> list[tuple[str, int, int]]:
    """(path, size_bytes, mtime) for the cleanup-managed files. mtimes advance
    only while a file is actually being written: the WU cache files are
    anchored to the last cleanup run when healthy (size seeded per anchor, so
    it wanders day-to-day but never changes without a matching mtime); the
    always-written logs stay mtime~now; while degraded/broken everything is
    being appended, mtime~now and each file carries its share of the fill."""
    extra_kb = c_fill_extra_kb()
    dying = disk_dying()
    with _state_lock:
        cleaned = _cleanup_ran
    anchor = max(int(cleaned), _last_daily(now, 3, 15))
    out: list[tuple[str, int, int]] = []
    for i, (path, base, share) in enumerate(GROWING_FILES):
        if path.endswith("Security.evtx"):
            # the event log is always being written; it auto-archives (wraps)
            size = base + int((now - _SEC_EVTX_EPOCH) * 2500) % 3_200_000_000
            mtime = int(now) - 2
        elif path.endswith("edb.log"):
            size = base  # a full ESE log, constantly written
            mtime = int(now) - 1
        else:
            # trimmed daily -> size is a per-anchor deterministic value (only
            # changes when the mtime does) and mtime is the last cleanup run
            size = int(base * (1.0 + 0.06 * (random.Random(anchor + i).random() * 2 - 1)))
            mtime = anchor
        if dying:
            size += int(share * extra_kb) * 1024
            mtime = int(now) - 3
        out.append((path, size, mtime))
    return out


def cache_file_bytes() -> int:
    """Current size of the headline WU cache file (for the admin page)."""
    return growing_files(time.time())[0][1]


# --------------------------------------------------------------------------- #
#  winperf counter estates (persisted; rates in the sampler functions below)
# --------------------------------------------------------------------------- #
# CPU: per-core user/privileged 100ns-tick counters. Idle is DERIVED
# (elapsed - user - priv), so idle+user+priv is exactly consistent and idle
# stays monotonic (util < 95 %). CPU is state-INDEPENDENT: the story keeps it
# green in all states (low noise, one root cause).
_CPU_SPREAD = (1.18, 0.94, 1.06, 0.82)  # cores never load identically
_CPU_USER_FRAC, _CPU_PRIV_FRAC = 0.058, 0.034  # ~9.2 % util, idle ~91 %
C_CPU_USER = tuple(
    Counter(
        f"cpu{i}.user",
        phase=0.4 + 1.31 * i,
        amp=0.25,
        period=660 + 97 * i,
        start=_aged(1e7 * _CPU_USER_FRAC * _CPU_SPREAD[i]),
    )
    for i in range(CPU_CORES)
)
C_CPU_PRIV = tuple(
    Counter(
        f"cpu{i}.priv",
        phase=2.1 + 1.47 * i,
        amp=0.25,
        period=740 + 83 * i,
        start=_aged(1e7 * _CPU_PRIV_FRAC * _CPU_SPREAD[i]),
    )
    for i in range(CPU_CORES)
)

# PhysicalDisk 0 C: (SSD system disk — carries the fill) and 1 D: (data,
# always calm). time/queue accumulators are 100ns ticks; io amplitudes small.
_DISK_NAMES = ("rd_ios", "wr_ios", "rd_bytes", "wr_bytes", "rd_time", "wr_time", "idle")
# healthy C: 20 r/s @ 0.35 ms + 35 w/s @ 0.55 ms, ~0.56 / ~1.05 MB/s
DISK_C = {
    "rd_ios": Counter("c.rd_ios", phase=0.2, amp=0.12, period=1150, start=_aged(20)),
    "wr_ios": Counter("c.wr_ios", phase=0.9, amp=0.12, period=1300, start=_aged(35)),
    "rd_bytes": Counter("c.rd_bytes", phase=1.6, amp=0.15, period=1240, start=_aged(560_000)),
    "wr_bytes": Counter("c.wr_bytes", phase=2.3, amp=0.15, period=1420, start=_aged(1_050_000)),
    "rd_time": Counter(
        "c.rd_time", phase=3.0, amp=0.12, period=1180, start=_aged(20 * 0.00035 * 1e7)
    ),
    "wr_time": Counter(
        "c.wr_time", phase=3.7, amp=0.12, period=1340, start=_aged(35 * 0.00055 * 1e7)
    ),
    # near-flat amplitude: idle has a hard ceiling (can't be more than idle)
    "idle": Counter("c.idle", phase=4.4, amp=0.0, period=1500, start=_aged(0.997 * 1e7)),
}
# healthy D: 12 r/s @ 0.40 ms + 18 w/s @ 0.60 ms, ~0.38 / ~0.61 MB/s
DISK_D = {
    "rd_ios": Counter("d.rd_ios", phase=5.1, amp=0.12, period=1210, start=_aged(12)),
    "wr_ios": Counter("d.wr_ios", phase=5.8, amp=0.12, period=1370, start=_aged(18)),
    "rd_bytes": Counter("d.rd_bytes", phase=0.5, amp=0.15, period=1290, start=_aged(380_000)),
    "wr_bytes": Counter("d.wr_bytes", phase=1.2, amp=0.15, period=1460, start=_aged(610_000)),
    "rd_time": Counter(
        "d.rd_time", phase=1.9, amp=0.12, period=1230, start=_aged(12 * 0.0004 * 1e7)
    ),
    "wr_time": Counter(
        "d.wr_time", phase=2.6, amp=0.12, period=1390, start=_aged(18 * 0.0006 * 1e7)
    ),
    "idle": Counter("d.idle", phase=3.3, amp=0.0, period=1550, start=_aged(0.998 * 1e7)),
}

# NIC: DC traffic for a ~300-host estate (DNS/LDAP/Kerberos), a few Mbit/s.
NET_IN_BPS, NET_OUT_BPS = 190_000.0, 310_000.0  # ~1.5 / ~2.5 Mbit/s
NET_IN_PPS, NET_OUT_PPS = 430.0, 465.0
NET = {
    "in_oct": Counter("if.in_oct", phase=0.7, amp=0.30, period=1260, start=_aged(NET_IN_BPS)),
    "out_oct": Counter("if.out_oct", phase=1.9, amp=0.30, period=1180, start=_aged(NET_OUT_BPS)),
    "in_u": Counter("if.in_ucast", phase=3.1, amp=0.30, period=1330, start=_aged(NET_IN_PPS)),
    "out_u": Counter("if.out_ucast", phase=4.3, amp=0.30, period=1410, start=_aged(NET_OUT_PPS)),
}
NIC_RAW_NAME = "Intel[R]_82574L_Gigabit_Network_Connection"


def _sample_cpu(uptime: float) -> tuple[list[int], list[int], list[int]]:
    """Per-core (user, priv, idle) 100ns-tick totals. Idle is derived from the
    same accumulators, so the three are consistent by construction."""
    elapsed = int(uptime * 1e7)
    user = [C_CPU_USER[i].sample(1e7 * _CPU_USER_FRAC * _CPU_SPREAD[i]) for i in range(CPU_CORES)]
    priv = [C_CPU_PRIV[i].sample(1e7 * _CPU_PRIV_FRAC * _CPU_SPREAD[i]) for i in range(CPU_CORES)]
    idle = [elapsed - u - p for u, p in zip(user, priv, strict=True)]
    return user, priv, idle


def _sample_disk(
    disk: dict[str, Counter],
    r_iops: float,
    r_lat: float,
    w_iops: float,
    w_lat: float,
    r_bps: float,
    w_bps: float,
) -> dict[str, int]:
    busy = min(0.97, r_iops * r_lat + w_iops * w_lat)
    return {
        "rd_ios": disk["rd_ios"].sample(r_iops),
        "wr_ios": disk["wr_ios"].sample(w_iops),
        "rd_bytes": disk["rd_bytes"].sample(r_bps),
        "wr_bytes": disk["wr_bytes"].sample(w_bps),
        "rd_time": disk["rd_time"].sample(r_iops * r_lat * 1e7),
        "wr_time": disk["wr_time"].sample(w_iops * w_lat * 1e7),
        "idle": disk["idle"].sample((1.0 - busy) * 1e7),
    }


def _sample_disks() -> list[dict[str, int]]:
    """C: write side carries the current fill slope (df and Disk IO tell the
    same story); latency creeps up with the write load. D: is always calm."""
    fill = c_fill_rate_kb_s()  # kB/s
    w_iops = 35.0 + fill / 256.0  # the fill lands in ~256 kB writes
    w_lat = 0.00055 + 0.00075 * min(1.0, fill / 36_000.0)
    disk_c = _sample_disk(
        DISK_C, 20.0, 0.00035, w_iops, w_lat, 560_000.0, 1_050_000.0 + fill * 1024.0
    )
    disk_d = _sample_disk(DISK_D, 12.0, 0.0004, 18.0, 0.0006, 380_000.0, 610_000.0)
    return [disk_c, disk_d]


# --------------------------------------------------------------------------- #
#  Windows Server 2022 DC estate: services + processes
#  (every running .exe-backed service has its process below; the ~15 svchost
#  groups map onto distinct svchost.exe instances)
# --------------------------------------------------------------------------- #
def _service_table() -> list[tuple[str, str, str]]:
    backup_state = "stopped/auto" if disk_dying() else "running/auto"
    return [
        ("MeridianBackupAgent", backup_state, "Meridian Backup & Disk Cleanup Agent"),
        ("ADWS", "running/auto", "Active Directory Web Services"),
        ("AJRouter", "stopped/demand", "AllJoyn Router Service"),
        ("ALG", "stopped/demand", "Application Layer Gateway Service"),
        ("AppIDSvc", "stopped/demand", "Application Identity"),
        ("Appinfo", "running/demand", "Application Information"),
        ("AppMgmt", "stopped/demand", "Application Management"),
        ("BFE", "running/auto", "Base Filtering Engine"),
        ("BITS", "running/demand", "Background Intelligent Transfer Service"),
        ("BrokerInfrastructure", "running/auto", "Background Tasks Infrastructure Service"),
        ("bthserv", "stopped/demand", "Bluetooth Support Service"),
        ("CertPropSvc", "stopped/demand", "Certificate Propagation"),
        ("CheckMkService", "running/auto", "Checkmk Agent"),
        ("COMSysApp", "stopped/demand", "COM+ System Application"),
        ("CoreMessagingRegistrar", "running/auto", "CoreMessaging"),
        ("CryptSvc", "running/auto", "Cryptographic Services"),
        ("DcomLaunch", "running/auto", "DCOM Server Process Launcher"),
        ("Dfs", "running/auto", "DFS Namespace"),
        ("DFSR", "running/auto", "DFS Replication"),
        ("Dhcp", "running/auto", "DHCP Client"),
        ("DiagTrack", "running/auto", "Connected User Experiences and Telemetry"),
        ("Dnscache", "running/auto", "DNS Client"),
        ("DNS", "running/auto", "DNS Server"),
        ("DPS", "running/auto", "Diagnostic Policy Service"),
        ("DsmSvc", "stopped/demand", "Device Setup Manager"),
        ("EventLog", "running/auto", "Windows Event Log"),
        ("EventSystem", "running/auto", "COM+ Event System"),
        ("FontCache", "running/auto", "Windows Font Cache Service"),
        ("gpsvc", "running/auto", "Group Policy Client"),
        ("iphlpsvc", "running/auto", "IP Helper"),
        ("IsmServ", "running/auto", "Intersite Messaging"),
        ("Kdc", "running/auto", "Kerberos Key Distribution Center"),
        ("KeyIso", "running/demand", "CNG Key Isolation"),
        ("LanmanServer", "running/auto", "Server"),
        ("LanmanWorkstation", "running/auto", "Workstation"),
        ("lmhosts", "running/demand", "TCP/IP NetBIOS Helper"),
        ("LSM", "running/auto", "Local Session Manager"),
        ("MapsBroker", "stopped/auto", "Downloaded Maps Manager"),
        ("mpssvc", "running/auto", "Windows Defender Firewall"),
        ("MSDTC", "running/auto", "Distributed Transaction Coordinator"),
        ("MSiSCSI", "stopped/demand", "Microsoft iSCSI Initiator Service"),
        ("NcbService", "stopped/demand", "Network Connection Broker"),
        ("Netlogon", "running/auto", "Netlogon"),
        ("netprofm", "running/demand", "Network List Service"),
        ("NlaSvc", "running/auto", "Network Location Awareness"),
        ("nsi", "running/auto", "Network Store Interface Service"),
        ("PerfHost", "stopped/demand", "Performance Counter DLL Host"),
        ("pla", "stopped/demand", "Performance Logs & Alerts"),
        ("PlugPlay", "running/auto", "Plug and Play"),
        ("PolicyAgent", "running/demand", "IPsec Policy Agent"),
        ("Power", "running/auto", "Power"),
        ("ProfSvc", "running/auto", "User Profile Service"),
        ("RasAuto", "stopped/demand", "Remote Access Auto Connection Manager"),
        ("RasMan", "stopped/demand", "Remote Access Connection Manager"),
        ("RemoteRegistry", "stopped/disabled", "Remote Registry"),
        ("RpcEptMapper", "running/auto", "RPC Endpoint Mapper"),
        ("RpcSs", "running/auto", "Remote Procedure Call (RPC)"),
        ("SamSs", "running/auto", "Security Accounts Manager"),
        ("Schedule", "running/auto", "Task Scheduler"),
        ("seclogon", "running/demand", "Secondary Logon"),
        ("SecurityHealthService", "running/demand", "Windows Security Service"),
        ("SENS", "running/auto", "System Event Notification Service"),
        ("SessionEnv", "running/demand", "Remote Desktop Configuration"),
        ("ShellHWDetection", "running/auto", "Shell Hardware Detection"),
        ("SNMPTRAP", "stopped/demand", "SNMP Trap"),
        ("Spooler", "stopped/disabled", "Print Spooler"),
        ("SstpSvc", "stopped/demand", "Secure Socket Tunneling Protocol Service"),
        ("StateRepository", "running/demand", "State Repository Service"),
        ("swprv", "stopped/demand", "Microsoft Software Shadow Copy Provider"),
        ("SystemEventsBroker", "running/auto", "System Events Broker"),
        ("TabletInputService", "stopped/demand", "Touch Keyboard and Handwriting Panel Service"),
        ("TermService", "running/demand", "Remote Desktop Services"),
        ("Themes", "running/auto", "Themes"),
        ("TimeBrokerSvc", "running/demand", "Time Broker"),
        ("TrkWks", "running/auto", "Distributed Link Tracking Client"),
        ("UALSVC", "running/auto", "User Access Logging Service"),
        ("UmRdpService", "running/demand", "Remote Desktop Services UserMode Port Redirector"),
        ("UserManager", "running/auto", "User Manager"),
        ("UsoSvc", "running/auto", "Update Orchestrator Service"),
        ("VaultSvc", "stopped/demand", "Credential Manager"),
        ("VSS", "stopped/demand", "Volume Shadow Copy"),
        ("W32Time", "running/auto", "Windows Time"),
        ("WaaSMedicSvc", "stopped/demand", "Windows Update Medic Service"),
        ("WbioSrvc", "stopped/demand", "Windows Biometric Service"),
        ("Wcmsvc", "running/auto", "Windows Connection Manager"),
        ("WdiServiceHost", "stopped/demand", "Diagnostic Service Host"),
        ("WdiSystemHost", "stopped/demand", "Diagnostic System Host"),
        ("WdNisSvc", "running/demand", "Microsoft Defender Antivirus Network Inspection Service"),
        ("Wecsvc", "stopped/demand", "Windows Event Collector"),
        ("WerSvc", "stopped/demand", "Windows Error Reporting Service"),
        ("WinDefend", "running/auto", "Microsoft Defender Antivirus Service"),
        ("WinHttpAutoProxySvc", "running/demand", "WinHTTP Web Proxy Auto-Discovery Service"),
        ("Winmgmt", "running/auto", "Windows Management Instrumentation"),
        ("WinRM", "running/auto", "Windows Remote Management (WS-Management)"),
        ("WpnService", "running/auto", "Windows Push Notifications System Service"),
        ("wuauserv", "running/demand", "Windows Update"),
        ("XblAuthManager", "stopped/demand", "Xbox Live Auth Manager"),
    ]


_SYS = "\\\\NT AUTHORITY\\SYSTEM"
_NET = "\\\\NT AUTHORITY\\NETWORK SERVICE"
_LOC = "\\\\NT AUTHORITY\\LOCAL SERVICE"

# (user, vsz_kb, ws_kb, pid, pagefile_mb, user_cpu_frac, kernel_cpu_frac,
#  handles, threads, age_offset_s, image name)
# CPU fracs = long-term share of ONE core; ps CPU times are 100ns ticks
# (rate/100000 -> % in cmk.plugins.lib.ps). lsass is the busiest process on a
# DC (holds the AD database); the per-process shares roughly account for the
# winperf busy time (rest = interrupts + short-lived processes).
_PROCS = (
    ("SYSTEM", 56, 140, 4, 0, 0.0, 0.0060, 1480, 112, 0, "System"),
    ("SYSTEM", 0, 64_800, 96, 4, 0.0, 0.0002, 0, 4, 0, "Registry"),
    ("SYSTEM", 1_648, 412, 348, 1, 0.0, 0.00002, 53, 2, 0, "smss.exe"),
    (_SYS, 5_240, 2_140, 472, 2, 0.0002, 0.0009, 580, 10, 0, "csrss.exe"),
    (_SYS, 5_560, 1_420, 556, 1, 0.0, 0.0001, 165, 1, 0, "wininit.exe"),
    (_SYS, 5_330, 1_980, 568, 2, 0.0001, 0.0006, 330, 10, 0, "csrss.exe"),
    (_SYS, 7_420, 3_010, 628, 3, 0.0001, 0.0002, 245, 3, 0, "winlogon.exe"),
    (_SYS, 8_100, 6_480, 676, 5, 0.0016, 0.0021, 690, 7, 0, "services.exe"),
    (_SYS, 3_260_000, 2_540_000, 692, 2480, 0.0550, 0.0320, 3450, 42, 0, "lsass.exe"),
    (_SYS, 22_000, 24_600, 768, 12, 0.0008, 0.0009, 1120, 14, 0, "svchost.exe"),
    (
        "\\\\Font Driver Host\\UMFD-0",
        2_230,
        1_660,
        744,
        1,
        0.0001,
        0.0001,
        64,
        5,
        0,
        "fontdrvhost.exe",
    ),
    (
        "\\\\Font Driver Host\\UMFD-1",
        1_680,
        2_140,
        752,
        1,
        0.0001,
        0.0001,
        64,
        5,
        0,
        "fontdrvhost.exe",
    ),
    (_NET, 16_800, 19_300, 884, 9, 0.0011, 0.0013, 960, 11, 0, "svchost.exe"),
    (_SYS, 12_900, 14_700, 916, 7, 0.0002, 0.0003, 420, 9, 0, "svchost.exe"),
    (_LOC, 34_000, 37_800, 1004, 22, 0.0040, 0.0050, 780, 12, 0, "svchost.exe"),
    ("\\\\Window Manager\\DWM-1", 68_000, 52_400, 1020, 39, 0.0019, 0.0028, 690, 15, 0, "dwm.exe"),
    (_SYS, 74_000, 86_500, 1060, 48, 0.0080, 0.0060, 2350, 38, 0, "svchost.exe"),
    (_SYS, 23_200, 26_400, 1112, 14, 0.0009, 0.0011, 640, 13, 0, "svchost.exe"),
    (_NET, 14_600, 17_200, 1180, 8, 0.0007, 0.0009, 410, 8, 0, "svchost.exe"),
    (_LOC, 8_400, 9_800, 1232, 5, 0.0002, 0.0003, 280, 6, 0, "svchost.exe"),
    (_SYS, 10_900, 12_500, 1296, 6, 0.0005, 0.0011, 380, 9, 0, "svchost.exe"),
    (_NET, 13_400, 15_700, 1352, 8, 0.0003, 0.0004, 460, 10, 0, "svchost.exe"),
    (_SYS, 40_500, 45_800, 1420, 27, 0.0100, 0.0080, 1240, 21, 0, "svchost.exe"),
    (_SYS, 55_000, 61_200, 1488, 39, 0.0012, 0.0009, 890, 17, 0, "svchost.exe"),
    (_LOC, 20_100, 22_700, 1544, 12, 0.0007, 0.0012, 720, 12, 0, "svchost.exe"),
    (_NET, 25_300, 28_600, 1608, 16, 0.0009, 0.0008, 560, 12, 0, "svchost.exe"),
    (_SYS, 29_800, 32_900, 1672, 19, 0.0014, 0.0011, 610, 14, 0, "svchost.exe"),
    (_NET, 18_600, 21_100, 1736, 11, 0.0003, 0.0005, 470, 10, 0, "svchost.exe"),
    ("SYSTEM", 122_000, 148_000, 2208, 0, 0.0, 0.0016, 0, 40, 0, "Memory Compression"),
    (_NET, 512_000, 428_000, 2320, 395, 0.0240, 0.0140, 640, 27, 0, "dns.exe"),
    (
        _SYS,
        132_000,
        97_500,
        2420,
        88,
        0.0028,
        0.0017,
        520,
        22,
        0,
        "Microsoft.ActiveDirectory.WebServices.exe",
    ),
    (_SYS, 84_600, 67_300, 2480, 55, 0.0090, 0.0070, 480, 18, 0, "dfsrs.exe"),
    (_SYS, 7_200, 5_700, 2540, 4, 0.0001, 0.0001, 150, 5, 0, "ismserv.exe"),
    (_SYS, 268_000, 214_000, 2680, 172, 0.0280, 0.0190, 1320, 31, 0, "MsMpEng.exe"),
    (_LOC, 12_300, 9_400, 2790, 6, 0.0004, 0.0006, 210, 7, 0, "NisSrv.exe"),
    (_NET, 12_100, 9_900, 2980, 7, 0.0001, 0.0002, 200, 10, 0, "msdtc.exe"),
    (_SYS, 104_000, 86_200, 3052, 71, 0.0090, 0.0060, 340, 12, 0, "MeridianBackupAgent.exe"),
    (_SYS, 46_800, 38_400, 3140, 29, 0.0060, 0.0040, 290, 14, 0, "check_mk_agent.exe"),
    (_SYS, 18_200, 14_600, 3168, 10, 0.0008, 0.0007, 120, 8, 0, "cmk-agent-ctl.exe"),
    (_NET, 28_900, 23_800, 3324, 17, 0.0021, 0.0016, 340, 11, 262_000, "WmiPrvSE.exe"),
    (_SYS, 19_400, 15_600, 3412, 11, 0.0012, 0.0009, 260, 9, 3_400_000, "WmiPrvSE.exe"),
    (_SYS, 13_800, 10_900, 3548, 8, 0.0002, 0.0002, 190, 9, 0, "dllhost.exe"),
    (_SYS, 14_500, 11_800, 3620, 8, 0.0002, 0.0003, 230, 7, 0, "taskhostw.exe"),
    (_SYS, 12_200, 9_600, 3812, 7, 0.0001, 0.0001, 130, 5, 3_500_000, "conhost.exe"),
    (_SYS, 16_900, 13_400, 3908, 10, 0.0003, 0.0004, 380, 9, 0, "SecurityHealthService.exe"),
)


# --------------------------------------------------------------------------- #
#  Agent output (Windows format)
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now_f = time.time()
    now = int(now_f)
    uptime_f = now_f - START + UPTIME_OFFSET
    uptime = int(uptime_f)

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
    a(f"|{qlen}|{int(uptime_f * 10_000_000)}|10000000|OK")
    a("[computer_system]")
    a("Name|NumberOfLogicalProcessors|NumberOfProcessors|WMIStatus")
    a(f"{COMPUTERNAME}|{CPU_CORES}|1|OK")

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

    # ---- fileinfo: the files the dead cleanup service would trim. Their
    #      growth shares sum to 1.0 x the C: fill, so the df delta is fully
    #      accounted for (needs a File-info rule to alert — corroboration the
    #      AI can quote). mtimes only advance while a file is written. ------- #
    a("<<<fileinfo:sep(124)>>>")
    a(str(now))
    for path, size, mtime in growing_files(now_f):
        a(f"{path}|{size}|{mtime}")
    ck = _ntds_checkpoint(now_f)
    ntds_size = 4_598_000_000 + int((ck - _NTDS_DIT_EPOCH) * 2)
    a(f"C:\\Windows\\NTDS\\ntds.dit|{ntds_size}|{ck}")

    # ---- df: the incident. sep(9) -> TAB-separated; NTFS reformatting in the
    #      parser. C: fills (default magnitude 80/90), D: data stays green. --- #
    a("<<<df:sep(9)>>>")
    c_used = c_drive_used_kb(now_f)
    c_avail = C_SIZE_KB - c_used
    c_pct = round(c_used / C_SIZE_KB * 100)
    a(TAB.join(["C:\\", "NTFS", str(C_SIZE_KB), str(c_used), str(c_avail), f"{c_pct}%", "C:\\"]))
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

    # ---- services: the full 2022-DC set (~95). The Meridian Backup Agent
    #      (the cleanup service) is stopped once we leave healthy -> the root
    #      cause; it is the only stopped auto-start service besides MapsBroker,
    #      so Service Summary stays quiet. Individual Windows services alert
    #      only with a "Windows Services" monitoring rule (no default). ------- #
    a("<<<services>>>")
    for name, status, descr in _service_table():
        a(f"{name} {status} {descr}")

    # ---- deployed agent plugins (Windows provenance) ----------------------- #
    a("<<<checkmk_agent_plugins_win:sep(0)>>>")
    a("pluginsdir C:\\ProgramData\\checkmk\\agent\\plugins")
    a("localdir C:\\ProgramData\\checkmk\\agent\\local")
    a(
        f"C:\\ProgramData\\checkmk\\agent\\plugins\\"
        f'cmk_update_agent.checkmk.py:CMK_VERSION = "{AGENT_VERSION}"'
    )
    a(f'C:\\ProgramData\\checkmk\\agent\\plugins\\mk_inventory.vbs:CMK_VERSION = "{AGENT_VERSION}"')

    # winperf CPU sampled up front: the ps Idle line reuses the same idle
    # ticks (the parser reads CPU cores from Idle's thread field, and a
    # specialist WILL sum ps CPU against the winperf utilization).
    cpu_user, cpu_priv, cpu_idle = _sample_cpu(uptime_f)

    # ---- processes (Windows ps:sep(9)):
    #      (user,VSZkb,WSkb,0,pid,pagefileMB,usertime,kerneltime,handles,
    #      threads,age)\tname — CPU times are 100ns ticks growing at each
    #      process's long-term rate; working sets and handle counts wander
    #      (slow gauges, not static, not white noise). The root-cause service's
    #      MeridianBackupAgent.exe is ABSENT while the service is stopped. --- #
    a("<<<ps:sep(9)>>>")
    # System Idle Process: kernel time = the winperf idle ticks summed over
    # cores; thread count = CPU_CORES (ps_section.py reads core count there).
    a(f"(SYSTEM,0,8,0,0,0,0,{sum(cpu_idle)},0,{CPU_CORES},{uptime}){TAB}System Idle Process")
    with _state_lock:
        backup_age = max(0, int(now_f - _backup_since))
    for user, vsz, ws, pid, pf, uf, kf, handles, threads, age_off, name in _PROCS:
        if name == "MeridianBackupAgent.exe":
            if disk_dying():
                continue  # service stopped -> no process
            age = min(uptime, backup_age)
        else:
            age = max(60, uptime - age_off)
        ws_now = int(
            gauge(
                f"ps.{pid}.ws",
                ws,
                amp_frac=0.03,
                phase=(pid % 97) * 0.35,
                period=800 + (pid % 13) * 110,
            )
        )
        h_now = (
            handles
            if handles < 100
            else int(
                gauge(
                    f"ps.{pid}.h",
                    handles,
                    amp_frac=0.04,
                    phase=(pid % 89) * 0.29,
                    period=900 + (pid % 11) * 130,
                )
            )
        )
        ut = _run_ticks(uf * 1e7, now_f, phase=pid * 0.37) if uf > 0 else 0
        kt = _run_ticks(kf * 1e7, now_f, phase=pid * 0.37 + 1.3) if kf > 0 else 0
        a(f"({user},{vsz},{ws_now},0,{pid},{pf},{ut},{kt},{h_now},{threads},{age}){TAB}{name}")

    # ---- winperf_processor: per-core 100ns tick counters. Verified against
    #      cmk/plugins/windows/agent_based/winperf_processor.py: -232 idle
    #      (100nsec_timer_inv, util = 100 - rate*1e-5), -96 user, -94
    #      privileged; per-core = line[1:-2], _Total = line[-2] (mean). ------ #
    a("<<<winperf_processor>>>")
    a(f"{uptime_f:.2f} 238 10000000")
    a(f"{CPU_CORES + 1} instances: " + " ".join(str(i) for i in range(CPU_CORES)) + " _Total")

    def _cpu_row(rid: str, vals: list[int], typ: str, total: int) -> None:
        a(f"{rid} " + " ".join(str(v) for v in vals) + f" {total} {typ}")

    _cpu_row("-232", cpu_idle, "100nsec_timer_inv", sum(cpu_idle) // CPU_CORES)
    _cpu_row("-96", cpu_user, "100nsec_timer", sum(cpu_user) // CPU_CORES)
    _cpu_row("-94", cpu_priv, "100nsec_timer", sum(cpu_priv) // CPU_CORES)
    # cosmetic (ignored by the parser, expected in a real dump): interrupts +
    # DPCs per core (counter -> _Total is the SUM), DPC queue rawcount
    intr = [
        _run_ticks(900.0 + 55.0 * i, now_f, phase=0.9 * i, period=1100) for i in range(CPU_CORES)
    ]
    dpcs = [
        _run_ticks(340.0 + 30.0 * i, now_f, phase=1.1 * i + 0.5, period=1300)
        for i in range(CPU_CORES)
    ]
    _cpu_row("-90", intr, "counter", sum(intr))
    _cpu_row("1096", dpcs, "counter", sum(dpcs))
    _cpu_row("1098", [0] * CPU_CORES, "rawcount", 0)

    # ---- winperf_phydisk: verified against winperf_phydisk.py (_LINE_TO_
    #      METRIC): -20/-18 ios, -14/-12 throughput, 1168/1170 queue-time
    #      ticks (denominator fixed 1e7), -26/-24 average_timer/average_base
    #      wait pairs (wait = dTimer/(dBase*frequency); base == the matching
    #      ios counter, exactly like a real agent dump). Instances row lists
    #      disks + _Total; per-disk values = row[1:-2]. C: writes carry the
    #      fill slope. ------------------------------------------------------- #
    disks = _sample_disks()
    ft = int((now_f + 11_644_473_600) * 10_000_000)  # FILETIME (100ns since 1601)
    a("<<<winperf_phydisk>>>")
    a(f"{uptime_f:.2f} 234 10000000")
    a("3 instances: 0_C: 1_D: _Total")

    def _dsk_row(rid: str, vals: list[int], typ: str) -> None:
        a(f"{rid} " + " ".join(str(v) for v in vals) + f" {sum(vals)} {typ}")

    d_time = [d["rd_time"] + d["wr_time"] for d in disks]
    d_ios = [d["rd_ios"] + d["wr_ios"] for d in disks]
    d_bytes = [d["rd_bytes"] + d["wr_bytes"] for d in disks]
    ft_row = [ft] * len(disks)
    _dsk_row("-36", [0] * len(disks), "rawcount")
    _dsk_row("-34", d_time, "type(542573824)")
    a(f"-34 {ft} {ft} {ft} type(1073939712)")
    _dsk_row("1166", d_time, "type(5571840)")
    _dsk_row("-32", [d["rd_time"] for d in disks], "type(542573824)")
    a(f"-32 {ft} {ft} {ft} type(1073939712)")
    _dsk_row("1168", [d["rd_time"] for d in disks], "type(5571840)")
    _dsk_row("-30", [d["wr_time"] for d in disks], "type(542573824)")
    a(f"-30 {ft} {ft} {ft} type(1073939712)")
    _dsk_row("1170", [d["wr_time"] for d in disks], "type(5571840)")
    _dsk_row("-28", d_time, "average_timer")
    _dsk_row("-28", d_ios, "average_base")
    _dsk_row("-26", [d["rd_time"] for d in disks], "average_timer")
    _dsk_row("-26", [d["rd_ios"] for d in disks], "average_base")
    _dsk_row("-24", [d["wr_time"] for d in disks], "average_timer")
    _dsk_row("-24", [d["wr_ios"] for d in disks], "average_base")
    _dsk_row("-22", d_ios, "counter")
    _dsk_row("-20", [d["rd_ios"] for d in disks], "counter")
    _dsk_row("-18", [d["wr_ios"] for d in disks], "counter")
    _dsk_row("-16", d_bytes, "bulk_count")
    _dsk_row("-14", [d["rd_bytes"] for d in disks], "bulk_count")
    _dsk_row("-12", [d["wr_bytes"] for d in disks], "bulk_count")
    _dsk_row("-10", d_bytes, "average_bulk")
    _dsk_row("-10", d_ios, "average_base")
    _dsk_row("-8", [d["rd_bytes"] for d in disks], "average_bulk")
    _dsk_row("-8", [d["rd_ios"] for d in disks], "average_base")
    _dsk_row("-6", [d["wr_bytes"] for d in disks], "average_bulk")
    _dsk_row("-6", [d["wr_ios"] for d in disks], "average_base")
    _dsk_row("1248", [d["idle"] for d in disks], "type(542573824)")
    a(f"1248 {ft} {ft} {ft} type(1073939712)")
    _dsk_row(
        "1250",
        [_run_ticks(1.4 + 0.4 * i, now_f, phase=2.2 + i, period=1250) for i in range(len(disks))],
        "counter",
    )
    del ft_row

    # ---- winperf_if: verified against winperf_if.py (row id -> field map:
    #      -246/-4 octets, 14/26 ucast, 16/28 nucast, 18/20/30/32 disc/err,
    #      10 speed bits/s, 34 out qlen, 2002 oper-status pseudo counter; the
    #      header line must contain a '.' to be recognized). ----------------- #
    net_in = NET["in_oct"].sample(NET_IN_BPS)
    net_out = NET["out_oct"].sample(NET_OUT_BPS)
    net_in_u = NET["in_u"].sample(NET_IN_PPS)
    net_out_u = NET["out_u"].sample(NET_OUT_PPS)
    net_in_nu = _run_ticks(5.2, now_f, phase=0.8, period=1150)  # broadcast/mcast
    net_out_nu = _run_ticks(2.1, now_f, phase=2.4, period=1350)
    a("<<<winperf_if>>>")
    a(f"{uptime_f:.2f} 510 10000000")
    a(f"1 instances: {NIC_RAW_NAME}")
    a(f"-122 {net_in + net_out} bulk_count")
    a(f"-110 {net_in_u + net_in_nu + net_out_u + net_out_nu} bulk_count")
    a(f"-244 {net_in_u + net_in_nu} bulk_count")
    a(f"-58 {net_out_u + net_out_nu} bulk_count")
    a("10 1000000000 large_rawcount")
    a(f"-246 {net_in} bulk_count")
    a(f"14 {net_in_u} bulk_count")
    a(f"16 {net_in_nu} bulk_count")
    a("18 137 large_rawcount")
    a("20 0 large_rawcount")
    a("22 0 large_rawcount")
    a(f"-4 {net_out} bulk_count")
    a(f"26 {net_out_u} bulk_count")
    a(f"28 {net_out_nu} bulk_count")
    a("30 0 large_rawcount")
    a("32 0 large_rawcount")
    a("34 0 large_rawcount")
    a("1086 0 large_rawcount")
    a("1088 0 large_rawcount")
    a("1090 0 bulk_count")
    a("1092 0 bulk_count")
    a("1094 4211 large_rawcount")
    a("2002 1 text")

    # ---- system time (compared to the monitoring server's clock) ----------- #
    a("<<<systemtime>>>")
    a(str(now))

    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence (state + START for uptime continuity + every counter
#  accumulator keyed by stable name -> a redeploy mid-demo neither resets the
#  incident nor makes any winperf counter go backwards)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-win-dc-state.json")


def save_state() -> None:
    if not STATE_FILE:
        return
    with _state_lock:
        data = {
            "version": 2,
            "start": START,
            "state": _state,
            "degraded_since": _degraded_since,
            "broken_since": _broken_since,
            "state_since": _state_since,
            "pressure_at_break": _pressure_at_break,
            "backup_since": _backup_since,
            "cleanup_ran": _cleanup_ran,
            "counters": {name: [c.acc, c.last] for name, c in _ALL_COUNTERS.items()},
        }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"[state] save failed: {exc}")


def load_state() -> None:
    global \
        START, \
        _state, \
        _degraded_since, \
        _broken_since, \
        _state_since, \
        _pressure_at_break, \
        _backup_since, \
        _cleanup_ran
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
        _pressure_at_break = data.get("pressure_at_break", 0.78)
        _backup_since = data.get("backup_since", START - UPTIME_OFFSET)
        _cleanup_ran = data.get("cleanup_ran", 0.0)
        saved = data.get("counters", {})
        restored = 0
        for name, c in _ALL_COUNTERS.items():
            if name in saved:
                c.acc, c.last = saved[name]
                restored += 1
        # counters not in the file (added by a code update) keep their fresh
        # seeds — only those cost one IgnoreResults cycle, the rest carry on
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
        "tagline": "All green. C: ~53 % used, every service running.",
        "effects": [
            "every service OK — the starting picture",
            "Filesystem C:/ ~54 % (default magnitude levels 80/90)",
            "MeridianBackupAgent running — nightly C: cleanup happening",
            "CPU utilization ~9 %, Disk IO + Interface calm",
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
            "(CRIT only if you add a 'Windows Services' rule for it; documented) and "
            "its MeridianBackupAgent.exe vanishes from <<<ps>>>",
            "Filesystem C:/ climbs 54 -> ~85 % -> WARN; the WU cache + log files grow "
            "at exactly the fill rate",
            "Disk IO SUMMARY: write throughput ramps to ~37 MB/s (graph corroboration)",
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
            "the AI fuses the stopped cleanup service + the steep C: slope + the "
            "growing cache files + the write-IO ramp: 'restart the agent and clear "
            "the WU cache; C: full in <N> h'",
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
    c_pct = round(c_used / C_SIZE_KB * 100)
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

    cards: list[str] = []
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
 <h1>demo control — <b>{HOSTNAME}</b>
 <span style="color:#555">(Windows Server 2022 · auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta["label"]}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
 — {meta["tagline"]}</div>
 {extra_html}
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "windc-demo-ctl/1.0"

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
        return self._send(
            200,
            {
                "state": state,
                "in_state_for_s": round(state_since_seconds(), 1),
                "c_drive_used_pct": round(c_drive_used_kb(time.time()) / C_SIZE_KB * 100, 1),
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
        f"start_state={get_state()}  counters={len(_ALL_COUNTERS)}"
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
