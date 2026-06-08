#!/usr/bin/env python3
"""Conf #12 demo: a fake 'black-box dependency' host for the live host-add demo.

Runs two servers in one process:

  1. TCP :6556  -> emits Check_MK agent output in *plaintext*.
     The Checkmk 2.5 fetcher reads the first 2 bytes (`<<` of `<<<check_mk>>>`),
     recognises TransportProtocol.PLAIN, and (with no encryption ruleset, the
     default is ANY_AND_PLAIN) accepts it with NO TLS / NO controller / NO
     registration. So we just write the raw section text and close. See
     check_mk:packages/cmk-check-engine/cmk/fetchers/_tcp.py.

  2. HTTP :8080 -> a service endpoint for check_http / check_httpv2.
     /             health endpoint: 200 when healthy, 503 (slow) when broken
     /admin        control UI (state cards + toggle buttons, auto-refresh)
     /admin/break  flip to broken   /admin/heal  flip to healthy
     /admin/status current state as JSON

The story (low noise, one root cause): the HOST stays green in both states —
the break flips exactly two things: the **symptom** (HTTP 503 + slow) and the
**root cause** (`payment-worker.service` failed -> Systemd Service Summary
CRIT). Everything else merely *corroborates* without alerting: 3 of 4
gunicorn workers gone, the survivor leaking (RSS grows live), TIME_WAIT
creeping up from client retries, tx throughput collapsed.

Stdlib only -> the container is plain python:slim, no pip install.

Config via env:
  CMK_HOSTNAME    host name baked into the agent output   (default: payment-api.corp.meridian-retail.com)
  AGENT_PORT      TCP port for the agent                  (default: 6556)
  HTTP_PORT       TCP port for the HTTP endpoint          (default: 8080)
  START_BROKEN    start in the broken state ("1"/"0")     (default: 1)
  BROKEN_DELAY_MS extra latency when broken, in ms        (default: 1500;
                  wobbled +/-30 % so the response-time graph looks organic)
  AGENT_VERSION   version string in the <<<check_mk>>> hdr (default: 2.5.0-2026.04.03)
  STATE_FILE      persistence file for counters/uptime/incident state, so a
                  restart never resets counters (which would mark Checkmk's
                  rate-based services stale) or the running incident
                  (default: /var/tmp/cmk-demo-payment-api-state.json; "" = off)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "payment-api.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
BROKEN_DELAY_MS = int(os.environ.get("BROKEN_DELAY_MS", "1500"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")

START = time.time()
UPTIME_OFFSET = 5 * 86400 + 3600  # pretend the host has been up ~5 days

_state_lock = threading.Lock()
_broken = os.environ.get("START_BROKEN", "1") not in ("0", "false", "False", "")
_broken_since: float | None = START if _broken else None
_state_since: float = START  # when the *current* state was entered


def is_broken() -> bool:
    with _state_lock:
        return _broken


def set_broken(value: bool) -> None:
    global _broken, _broken_since, _state_since
    with _state_lock:
        if value != _broken:
            _state_since = time.time()
        _broken = value
        if value:
            if _broken_since is None:
                _broken_since = time.time()
        else:
            _broken_since = None
    save_state()  # toggles must survive a restart mid-demo


def state_since_seconds() -> float:
    with _state_lock:
        return time.time() - _state_since


def broken_seconds() -> float:
    with _state_lock:
        return 0.0 if _broken_since is None else time.time() - _broken_since


def ramp(seconds: float) -> float:
    """0 -> 1 over `seconds` since the break. The unit crash itself is a step
    (crashes ARE discrete events), but the *secondary* signals — clients
    noticing, retrying, sockets piling up, traffic collapsing — take a couple
    of minutes to develop. No vertical cliffs in graphs that shouldn't have
    them."""
    bs = broken_seconds()
    return 0.0 if bs <= 0 else min(1.0, bs / seconds)


def _lerp(healthy: float, broken: float, r: float) -> float:
    return healthy + (broken - healthy) * r


def worker_leak_kb() -> int:
    """The surviving gunicorn worker leaks ~110 kB/s while broken (the same
    bad deploy that killed payment-worker). Live-growing across re-polls —
    capped at ~450 MB so Memory stays comfortably green (the host must NOT
    alert; this is graph-visible corroboration only)."""
    return min(450_000, int(broken_seconds() * 110))


# --------------------------------------------------------------------------- #
#  Monotonic, state-aware counters
# --------------------------------------------------------------------------- #
_ALL_COUNTERS: dict[str, "Counter"] = {}


class _Wobble:
    """Smooth, autocorrelated deviation in ~[-1, 1] for both counter rates and
    instantaneous gauges.

    A single fixed-period sine is wrong two ways: (1) it renders as an
    obviously synthetic clockwork sawtooth (one identical peak every `period`
    seconds), and (2) if `period` is shorter than 2x the monitoring poll
    interval (~60 s) the wave ALIASES into high-frequency jigsaw on a 1-min
    graph (Nyquist). So: THREE incommensurate harmonics with LONG periods
    (tens of minutes, well above Nyquist -> no repeat, no aliasing) plus an
    AR(1) random walk for the aperiodic minute-to-minute texture a pure sine
    can't produce. Clamped to [-1, 1].
    """

    def __init__(self, phase: float = 0.0, period: float = 1200.0) -> None:
        self.phase = phase
        self.omega = 2.0 * math.pi / period
        self.noise = 0.0  # AR(1) state (ephemeral; converges in a few steps)

    def step(self, now: float) -> float:
        harm = (0.60 * math.sin(self.omega * now + self.phase)
                + 0.28 * math.sin(self.omega * 2.7 * now + self.phase * 1.7)
                + 0.18 * math.sin(self.omega * 0.41 * now + self.phase * 0.5))
        # mean-reverting, bounded -> irregular but smooth (no white-noise jitter)
        self.noise = max(-1.5, min(1.5, self.noise * 0.9 + random.gauss(0.0, 0.25)))
        return max(-1.0, min(1.0, (harm + 0.45 * self.noise) / 1.8))


_GAUGES: dict[str, _Wobble] = {}
_GAUGE_LOCK = threading.Lock()


def gauge(name: str, base: float, *, amp_abs: float | None = None,
          amp_frac: float | None = None, phase: float = 0.0,
          period: float = 1200.0) -> float:
    """An instantaneous value that wanders smoothly around `base` instead of
    being static (a flat line screams "fake") or white-noise jittery
    (`random.uniform` every poll has no autocorrelation -> spiky garbage).

    `amp_abs` wanders +/- that many absolute units; `amp_frac` wanders by that
    fraction of `base`. Gauge state is ephemeral — a small jump at restart is
    invisible for an instantaneous value (unlike a counter, which must never
    go backwards).
    """
    with _GAUGE_LOCK:
        w = _GAUGES.get(name)
        if w is None:
            w = _GAUGES[name] = _Wobble(phase, period)
        d = w.step(time.time())
    if amp_abs is not None:
        return base + amp_abs * d
    return base * (1.0 + (amp_frac or 0.0) * d)


class Counter:
    """A strictly monotonic counter that integrates a *caller-supplied* rate.

    Checkmk derives rates as delta(counter)/delta(time), so the counter must
    never decrease even when the break/heal toggle changes the underlying
    rate. We integrate the current rate over the time since the last sample:
    flipping state changes the slope from now on, never the accumulated value.
    The rate is multiplied by `1 + amp * wobble` (see _Wobble) and the wobble
    is clamped to [-1, 1], so the instantaneous rate stays in
    `rate * [1-amp, 1+amp]` > 0 — the counter can never go backwards (which
    would re-trigger the staleness cascade). Distinct phases per counter keep
    the metrics from peaking together.
    """

    def __init__(self, name: str, phase: float = 0.0, amp: float = 0.30,
                 period: float = 1200.0, start: float = 0.0) -> None:
        self.acc = start
        self.last = time.time()
        self.amp = amp
        self.wob = _Wobble(phase, period)
        self.lock = threading.Lock()
        # stable name as persistence key: adding/removing counters in a code
        # update must not reset the surviving ones (a reset counter goes
        # backwards, and Checkmk repairs only ONE backwards counter per check
        # cycle -> minutes of staleness)
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
    """Start value so the counter looks like it has run for the fake uptime."""
    return rate_per_s * UPTIME_OFFSET


# /proc/stat is in jiffies: 100 Hz * 4 CPUs = ~400 ticks/s TOTAL — the four
# fields must sum to ~400, not more (a kernel person will add them up).
# Healthy: a lightly loaded app server, ~12 % busy.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(35))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(12))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(348))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(4))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(2400))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(6))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(0.05))

# diskstat counters per device: rd_ios, rd_ticks(ms), wr_ios, wr_ticks(ms),
# io_ticks(ms, drives the utilization %). Both are SSD-backed virtio volumes:
# sub-ms latencies, single-digit utilization — an app server's disks are
# write-mostly (logs/spool), reads are all page-cache hits.
# io_ticks gets a near-flat amplitude: utilization has a hard 100 % ceiling,
# a +/-30 % swing on a high base would render impossible values.
VDA = {  # root volume: logs
    "rd_ios": Counter("vda.rd_ios", phase=0.0, start=_aged(3)),
    "rd_ticks": Counter("vda.rd_ticks", phase=0.2, start=_aged(1.2)),
    "wr_ios": Counter("vda.wr_ios", phase=0.4, start=_aged(26)),
    "wr_ticks": Counter("vda.wr_ticks", phase=0.6, start=_aged(18)),
    "io_ticks": Counter("vda.io_ticks", phase=0.8, amp=0.01, start=_aged(22)),
}
VDB = {  # /data volume: spool + exports
    "rd_ios": Counter("vdb.rd_ios", phase=1.0, start=_aged(6)),
    "rd_ticks": Counter("vdb.rd_ticks", phase=1.2, start=_aged(3)),
    "wr_ios": Counter("vdb.wr_ios", phase=1.4, start=_aged(14)),
    "wr_ticks": Counter("vdb.wr_ticks", phase=1.6, start=_aged(10)),
    "io_ticks": Counter("vdb.io_ticks", phase=1.8, amp=0.01, start=_aged(14)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(180_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(320_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(950))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(900))


def filesystem_usage(now: float) -> tuple[int, int]:
    """Realistic used-space (kB) for / and /data over time.

    A static usage line is a dead giveaway. Real volumes show secular GROWTH
    with periodic CLEANUP sawteeth. All terms are pure functions of wall-clock
    `now` (+ persisted START), so the curve is continuous across re-polls and
    restarts:

      * root /: app + system logs creep up, journald/logrotate trims them
        daily (a ~0.9 GiB sawtooth) on a slow secular base.
      * /data: the settlement spool fills and is flushed hourly (~0.5 GiB
        teeth), daily exports are purged by retention (~2 GiB teeth), and the
        archive grows slowly forever. Stays ~60-70 % — well under the 80/90 %
        df defaults (green corroboration, never an alert).
    """
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: ~6.8 GiB base + log sawtooth + slow creep, of 20 GiB
    root_base = 7_130_000
    root_logs = 943_718 * ((now % day) / day)          # 0..0.9 GiB daily teeth
    root_growth = min(1_572_864, uptime * 0.03)        # capped ~1.5 GiB
    root_used = int(root_base + root_logs + root_growth
                    + gauge("fs.root", 0, amp_abs=60_000, period=1500))

    # data: ~29 GiB base + hourly spool teeth + daily export purge + archive
    data_base = 30_408_704
    spool = 524_288 * ((now % 3600.0) / 3600.0)        # 0..0.5 GiB hourly
    exports = 2_097_152 * ((now % day) / day)          # 0..2 GiB daily
    archive = min(4_194_304, uptime * 1.0)             # ~1 kB/s, capped ~4 GiB
    data_used = int(data_base + spool + exports + archive
                    + gauge("fs.data", 0, amp_abs=150_000, period=900))
    return root_used, data_used


# --------------------------------------------------------------------------- #
#  Agent output generation
# --------------------------------------------------------------------------- #
def _kb(mib: float) -> int:
    return int(mib * 1024)


def build_agent_output(broken: bool) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4

    # NOTE: host-level resources stay HEALTHY in both states on purpose. The
    # whole point of the demo is "the host looks fine, yet the service is
    # down". The ONLY reds the broken flag produces are the symptom (HTTP 503,
    # via the active check) and the root cause (payment-worker.service failed
    # -> Systemd Service Summary CRIT). Everything below that changes with the
    # state is *graph-visible corroboration* that never alerts.

    # ---- the broken-state physics ------------------------------------------ #
    # 3 of 4 gunicorn workers crashed (instant — crashes are steps) and the
    # survivor leaks; secondary signals (clients retrying, traffic collapsing)
    # ramp over a few minutes.
    leak = worker_leak_kb() if broken else 0
    n_workers = 1 if broken else 4
    # 3 workers x ~240 MB anon vanish at the crash, then the leak grows back
    anon_shift = (-720_000 + leak) if broken else 0
    r_net = ramp(180)  # clients/retries take ~3 min to fully develop

    # ---- memory: a healthy 8 GiB app server. Full /proc/meminfo (58 keys,
    #      Ubuntu 24.04 set) so the Memory service yields the whole metric
    #      set. The LRU arithmetic must hold up (a kernel person will sum it):
    #        anon LRU = AnonPages + Shmem; file LRU = Buffers + Cached - Shmem
    #        MemAvailable ~ MemFree + file LRU + SReclaimable
    #      CommitLimit uses the real kernel formula SwapTotal + RAM/2. ------- #
    mem_total = 8_028_400
    swap_total = 2_097_148
    commit_limit = swap_total + mem_total // 2
    buffers = 182_000
    cached = 1_910_000  # includes Shmem
    shmem = 81_920
    sreclaimable = 224_000
    anon_pages = 2_350_000 + anon_shift
    active_anon = 1_530_000 + anon_shift
    inactive_anon = 901_920
    active_file = 642_000
    inactive_file = 1_368_080
    mem_free = int(gauge("mem.free", 2_650_000 - anon_shift,
                         amp_frac=0.015, phase=0.4, period=1500))
    mem_available = int(gauge("mem.avail", 4_870_000 - anon_shift,
                              amp_frac=0.012, phase=1.2, period=1700))
    committed = 3_260_000 + int(anon_shift * 1.15)
    dirty = max(2_048, int(gauge("mem.dirty", 14_500, amp_frac=0.18,
                                 phase=2.0, period=800)))
    writeback = max(0, int(gauge("mem.writeback", 220, amp_abs=220,
                                 phase=3.1, period=600)))

    # ---- load: healthy in BOTH states, well under one per core. Broken even
    #      dips slightly (the box is doing *less* work — another "host looks
    #      fine" tell). Each timescale wanders on its own clock: 1-min noisy
    #      and fast, 15-min heavily smoothed, like the kernel's real EWMAs. -- #
    load_base = _lerp(0.55, 0.42, r_net)
    l1 = round(load_base * gauge("load1", 1.0, amp_frac=0.22,
                                 phase=0.2, period=300), 2)
    l5 = round(load_base * gauge("load5", 1.0, amp_frac=0.12,
                                 phase=1.0, period=900), 2)
    l15 = round(load_base * gauge("load15", 1.0, amp_frac=0.06,
                                  phase=2.0, period=2400), 2)
    total_procs = 248 - (3 if broken else 0)

    # ---- /proc/stat: ~400 ticks/s total on 4 CPUs. Broken: a touch less
    #      user time (fewer requests served), idle picks it up. -------------- #
    user = C_USER.sample(_lerp(35, 24, r_net))
    system = C_SYSTEM.sample(_lerp(12, 10, r_net))
    idle = C_IDLE.sample(_lerp(348, 361, r_net))
    iowait = C_IOWAIT.sample(4)

    # ---- disks: calm in both states; broken writes a few more error log
    #      lines to the root volume. ----------------------------------------- #
    vda_rd = VDA["rd_ios"].sample(3)
    vda_rdt = VDA["rd_ticks"].sample(1.2)
    vda_wr = VDA["wr_ios"].sample(_lerp(26, 36, r_net))
    vda_wrt = VDA["wr_ticks"].sample(_lerp(18, 26, r_net))
    vda_iot = VDA["io_ticks"].sample(_lerp(22, 28, r_net))
    vdb_rd = VDB["rd_ios"].sample(6)
    vdb_rdt = VDB["rd_ticks"].sample(3)
    vdb_wr = VDB["wr_ios"].sample(_lerp(14, 4, r_net))   # spool starves
    vdb_wrt = VDB["wr_ticks"].sample(_lerp(10, 3, r_net))
    vdb_iot = VDB["io_ticks"].sample(_lerp(14, 6, r_net))

    # ---- network: the service-level collapse, visible in graphs only.
    #      rx climbs a little (clients retrying), tx falls off a cliff over a
    #      few minutes (503 bodies are tiny). -------------------------------- #
    rx_bytes = C_RX_B.sample(_lerp(180_000, 205_000, r_net))
    tx_bytes = C_TX_B.sample(_lerp(320_000, 55_000, r_net))
    rx_pkts = C_RX_P.sample(_lerp(950, 1150, r_net))
    tx_pkts = C_TX_P.sample(_lerp(900, 640, r_net))

    # ---- tcp states: ESTABLISHED dips (clients bail), TIME_WAIT piles up
    #      from the fast-failing retry storm. No default levels -> graph-only
    #      corroboration. ---------------------------------------------------- #
    established = round(gauge("tcp.estab", _lerp(26, 17, r_net),
                              amp_abs=4, phase=0.9, period=700))
    time_wait = round(gauge("tcp.timewait",
                            34 + (min(58.0, broken_seconds() / 30.0) if broken else 0),
                            amp_abs=5, phase=2.4, period=500))
    syn_sent = max(0, round(gauge("tcp.synsent", 0.4, amp_abs=0.8,
                                  phase=3.3, period=400)))

    lines: list[str] = []
    a = lines.append

    # Header mirrors a real 2.5 agent install (diffed against a real Linux
    # agent dump).
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

    # --- agent controller status + deployed plugins. The controller pretends
    #     to be REGISTERED (TLS): the Check_MK Agent service only warns "TLS
    #     is not activated" when allow_legacy_pull is true — with a registered
    #     pull connection + cert it reads like a properly TLS-registered host
    #     (the actual transport is never checked). The cert expiry is compared
    #     to wall clock (WARN/CRIT below 30/15 days), so `to` is dynamic. ---
    a("<<<cmk_agent_ctl_status:sep(0)>>>")
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 330 * 86400))
    a(json.dumps({
        "version": AGENT_VERSION,
        "agent_socket_operational": True,
        "ip_allowlist": [],
        "allow_legacy_pull": False,
        "connections": [{
            "site_id": "monitoring/prod",
            "receiver_port": 8000,
            "uuid": "9b2c41da-77e1-4ac1-93f4-2d0e6b8a4f27",
            "local": {
                "connection_mode": "pull-agent",
                "cert_info": {
                    "issuer": "Site 'prod' local CA",
                    "from": "Tue, 03 Jun 2025 09:12:44 +0000",
                    "to": cert_to,
                },
            },
            "remote": "remote_query_disabled",
        }],
    }, separators=(",", ":")))
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"'
      % AGENT_VERSION)

    # --- filesystems: usage grows and gets cleaned over time (see
    #     filesystem_usage) — spool/export sawteeth on a slow growth trend.
    #     Never fills up, stays green. ---
    a("<<<df_v2>>>")
    root_size = 20_961_280    # 20 GiB
    data_size = 52_428_800    # 50 GiB
    root_used, data_used = filesystem_usage(time.time())
    a(f"/dev/vda1 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/vdb1 ext4 {data_size} {data_used} {data_size - data_used} "
      f"{round(data_used / data_size * 100)}% /data")
    # inode usage (the reference dump carries it): ordinary root, a spool
    # volume holds moderately many small files.
    a("[df_inodes_start]")
    root_inodes = 1_310_720
    a(f"/dev/vda1 ext4 {root_inodes} 301244 {root_inodes - 301244} 23% /")
    data_inodes = 3_276_800
    a(f"/dev/vdb1 ext4 {data_inodes} 94312 {data_inodes - 94312} 3% /data")
    a("[df_inodes_end]")

    # --- mount options (+ the empty marker sections every real agent emits:
    #     their absence vs. the reference dump is a tell) ---
    a("<<<labels:sep(0)>>>")
    a("<<<nfsmounts_v2:sep(0)>>>")
    a("<<<cifsmounts>>>")
    a("<<<mounts>>>")
    a("/dev/vda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/vdb1 /data ext4 rw,noatime 0 0")

    # --- memory: full /proc/meminfo, Ubuntu 24.04 key set ---
    a("<<<mem>>>")
    a(f"MemTotal:       {mem_total} kB")
    a(f"MemFree:        {mem_free} kB")
    a(f"MemAvailable:   {mem_available} kB")
    a(f"Buffers:        {buffers} kB")
    a(f"Cached:         {cached} kB")
    a("SwapCached:     0 kB")
    a(f"Active:         {active_anon + active_file} kB")
    a(f"Inactive:       {inactive_anon + inactive_file} kB")
    a(f"Active(anon):   {active_anon} kB")
    a(f"Inactive(anon): {inactive_anon} kB")
    a(f"Active(file):   {active_file} kB")
    a(f"Inactive(file): {inactive_file} kB")
    a("Unevictable:    0 kB")
    a("Mlocked:        0 kB")
    a(f"SwapTotal:      {swap_total} kB")
    a(f"SwapFree:       {swap_total} kB")
    a("Zswap:          0 kB")
    a("Zswapped:       0 kB")
    a(f"Dirty:          {dirty} kB")
    a(f"Writeback:      {writeback} kB")
    a(f"AnonPages:      {anon_pages} kB")
    a("Mapped:         286720 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaimable} kB")
    a("Slab:           310000 kB")
    a(f"SReclaimable:   {sreclaimable} kB")
    a("SUnreclaim:     86000 kB")
    a("KernelStack:    3968 kB")  # ~248 threads x 16 KiB — matches loadavg total
    a("PageTables:     38912 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    38912 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         8192 kB")
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
    a("DirectMap4k:    280556 kB")
    a("DirectMap2M:    5961728 kB")
    a("DirectMap1G:    2097152 kB")

    # --- load average + nproc ---
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} 1/{total_procs} {12000 + C_PROC.sample(6) % 9999} {ncpu}")

    # --- uptime ---
    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.7)}.00")

    # --- systemd-timesyncd: must exist because the unit is running. The check
    #     compares BOTH the [[[epoch]]] marker and the NTPMessage
    #     ReceiveTimestamp against wall clock (defaults: last sync 7500/10800 s,
    #     last NTP message 3600/7200 s), so both are generated dynamically. ---
    last_sync = now - 620  # synced ~10 min ago, well inside the 34 min poll
    sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = random.randint(-1800, 1800)
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.58 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: C0248F88")
    a("    Precision: 1us (-24)")
    a("Root distance: 9.112ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 18.402ms")
    a(f"       Jitter: {random.randint(800, 3200) / 1000:.3f}ms")
    a(f" Packet count: {214 + int((time.time() - START) / 2048)}")
    a("    Frequency: +9.412ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-24, "
      "RootDelay=8.234ms, RootDispersion=1.108ms, Reference=C0248F88, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=41, Jitter=1.221ms }")
    a("Timezone=UTC")

    # --- apt: defaults WARN on any pending normal update and CRIT on security
    #     updates, so a green box reports the exact sentinel string ---
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # --- kernel: /proc/stat cpu line -> "CPU utilization" ---
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(_lerp(2400, 1700, r_net))}")
    a(f"processes {C_PROC.sample(6)}")
    a(f"pgmajfault {C_PGMAJ.sample(0.05)}")

    # --- software RAID: none, but the real agent always emits the section ---
    a("<<<md>>>")
    a("Personalities : ")
    a("unused devices: <none>")

    # --- diskstat: both volumes from df must exist here too ---
    a("<<<diskstat>>>")
    a(str(now))
    # major minor name rd_ios rd_merges rd_sect rd_ms wr_ios wr_merges wr_sect
    # wr_ms in_prog io_ms weighted_ms (+discard fields)
    a(f"252 0 vda {vda_rd} 0 {vda_rd * 24} {vda_rdt} {vda_wr} 0 "
      f"{vda_wr * 32} {vda_wrt} 0 {vda_iot} {vda_iot * 2} 0 0 0 0")
    a(f"252 16 vdb {vdb_rd} 0 {vdb_rd * 40} {vdb_rdt} {vdb_wr} 0 "
      f"{vdb_wr * 56} {vdb_wrt} 0 {vdb_iot} {vdb_iot * 2} 0 0 0 0")

    # --- network interface: the real agent emits BOTH lnx_if variants — the
    #     plain ip-link block and the sep(58) counter section ---
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
      "state UP group default qlen 1000")
    a("    link/ether 02:42:ac:11:00:0a brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:0a")

    # --- tcp connection stats ---
    a("<<<tcp_conn_stats>>>")
    a(f"01 {established}")
    a(f"02 {syn_sent}")
    a(f"06 {time_wait}")
    a("0A 7")

    # --- processes: every running systemd unit below must have its process
    #     here (a Linux person WILL cross-check), plus the app stack:
    #     nginx (master + 2 workers), redis, gunicorn master + workers, and
    #     the settlement worker (gone while broken — its unit failed).
    #     Broken: 3 of 4 gunicorn workers crashed; the survivor leaks, its
    #     RSS/VSZ grow live poll by poll (consistent with the meminfo
    #     AnonPages shift above — same variables). ---------------------------
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, puser, vsz, rss, cputime, pid, cmd in (
            ("init.scope", "root", 168_000, 13_100, "00:00:21", 1, "/sbin/init"),
            ("system.slice/systemd-journald.service", "root", 64_400, 21_300,
             "00:00:54", 412, "/usr/lib/systemd/systemd-journald"),
            ("system.slice/systemd-udevd.service", "root", 26_200, 8_100,
             "00:00:02", 450, "/usr/lib/systemd/systemd-udevd"),
            ("system.slice/systemd-networkd.service", "systemd-network", 21_500, 8_900,
             "00:00:11", 480, "/usr/lib/systemd/systemd-networkd"),
            ("system.slice/systemd-resolved.service", "systemd-resolve", 26_800, 13_400,
             "00:00:24", 501, "/usr/lib/systemd/systemd-resolved"),
            ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_000, 7_700,
             "00:00:05", 520, "/usr/lib/systemd/systemd-timesyncd"),
            ("system.slice/systemd-logind.service", "root", 14_900, 6_800,
             "00:00:03", 525, "/usr/lib/systemd/systemd-logind"),
            ("system.slice/dbus.service", "messagebus", 10_400, 5_100,
             "00:00:09", 530, "@dbus-daemon --system --address=systemd:"),
            ("system.slice/rsyslog.service", "syslog", 222_400, 6_700,
             "00:00:19", 640, "/usr/sbin/rsyslogd -n -iNONE"),
            ("system.slice/irqbalance.service", "root", 16_200, 4_900,
             "00:00:02", 648, "/usr/sbin/irqbalance --foreground"),
            ("system.slice/multipathd.service", "root", 352_000, 19_800,
             "00:00:07", 652, "/sbin/multipathd -d -s"),
            ("system.slice/networkd-dispatcher.service", "root", 33_400, 21_500,
             "00:00:01", 660, "/usr/bin/python3 /usr/bin/networkd-dispatcher "
             "--run-startup-triggers"),
            ("system.slice/polkit.service", "polkitd", 308_000, 9_200,
             "00:00:01", 668, "/usr/lib/polkit-1/polkitd --no-debug"),
            ("system.slice/snapd.service", "root", 1_248_000, 38_500,
             "00:01:12", 674, "/usr/lib/snapd/snapd"),
            ("system.slice/udisks2.service", "root", 402_000, 12_800,
             "00:00:02", 682, "/usr/libexec/udisks2/udisksd"),
            ("system.slice/unattended-upgrades.service", "root", 110_500, 22_100,
             "00:00:00", 690, "/usr/bin/python3 /usr/share/unattended-upgrades/"
             "unattended-upgrade-shutdown --wait-for-signal"),
            ("system.slice/ssh.service", "root", 15_400, 8_900,
             "00:00:00", 710, "sshd: /usr/sbin/sshd -D [listener]"),
            ("system.slice/cron.service", "root", 11_500, 2_500,
             "00:00:01", 720, "/usr/sbin/cron -f -P"),
            ("system.slice/getty@.service/getty@tty1.service", "root", 6_200, 1_700,
             "00:00:00", 728, "/sbin/agetty -o -p -- \\u --noclear tty1 linux"),
            ("user.slice/user-1000.slice/user@1000.service", "deploy", 20_300, 11_200,
             "00:00:00", 980, "/usr/lib/systemd/systemd --user"),
            ("system.slice/redis-server.service", "redis", 272_000, 47_800,
             "00:08:41", 802, "/usr/bin/redis-server 127.0.0.1:6379"),
            ("system.slice/nginx.service", "root", 55_200, 11_400,
             "00:00:01", 830, "nginx: master process /usr/sbin/nginx -g daemon on; "
             "master_process on;"),
            ("system.slice/nginx.service", "www-data", 58_400, 14_200,
             "00:03:22", 831, "nginx: worker process"),
            ("system.slice/nginx.service", "www-data", 58_400, 14_600,
             "00:03:18", 832, "nginx: worker process"),
    ):
        a(f"0::/{cgs} {puser} {vsz} {rss} {cputime} 5-01:11:40 {pid} {cmd}")
    cg = "0::/system.slice/payment-api.service"
    a(f"{cg} payment 248000 96000 00:00:42 5-01:11:08 901 "
      f"/usr/bin/python3 -m gunicorn --workers 4 payment_api.wsgi")
    for i in range(n_workers):
        # the survivor (worker 0) carries the leak; healthy workers are ~240 MB
        w_rss = 240_000 + (i * 37) % 14 * 1000 + (leak if i == 0 else 0)
        w_vsz = 620_000 + (leak if i == 0 else 0)
        a(f"{cg} payment {w_vsz} {w_rss} 00:39:1{i} 5-01:11:05 "
          f"{905 + i} gunicorn: worker [payment-api]")
    if not broken:
        a("0::/system.slice/payment-worker.service payment 380000 168000 "
          "00:12:05 5-01:11:02 940 /usr/bin/python3 /opt/payment-api/worker.py")

    # --- systemd units: a realistic Ubuntu 24.04 server runs ~30 services
    #     (incl. oneshots in "active/exited"), not 5. ALL green except the
    #     ROOT CAUSE: broken => payment-worker.service failed, and the
    #     "Systemd Service Summary" goes CRIT with no extra rules. -----------
    a("<<<systemd_units>>>")
    worker_state = ("failed", "failed") if broken else ("active", "running")
    units = [
        ("payment-api.service", "active", "running", "Payment API (gunicorn)"),
        ("payment-worker.service", *worker_state, "Payment settlement worker"),
        ("nginx.service", "active", "running",
         "A high performance web server and a reverse proxy server"),
        ("redis-server.service", "active", "running", "Advanced key-value store"),
        ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service", "active", "running",
         "Regular background program processing daemon"),
        ("dbus.service", "active", "running", "D-Bus System Message Bus"),
        ("getty@tty1.service", "active", "running", "Getty on tty1"),
        ("irqbalance.service", "active", "running", "irqbalance daemon"),
        ("multipathd.service", "active", "running",
         "Device-Mapper Multipath Device Controller"),
        ("networkd-dispatcher.service", "active", "running",
         "Dispatcher daemon for systemd-networkd"),
        ("polkit.service", "active", "running", "Authorization Manager"),
        ("rsyslog.service", "active", "running", "System Logging Service"),
        ("snapd.service", "active", "running", "Snap Daemon"),
        ("systemd-journald.service", "active", "running", "Journal Service"),
        ("systemd-logind.service", "active", "running", "User Login Management"),
        ("systemd-networkd.service", "active", "running", "Network Configuration"),
        ("systemd-resolved.service", "active", "running",
         "Network Name Resolution"),
        ("systemd-timesyncd.service", "active", "running",
         "Network Time Synchronization"),
        ("systemd-udevd.service", "active", "running",
         "Rule-based Manager for Device Events and Files"),
        ("udisks2.service", "active", "running", "Disk Manager"),
        ("unattended-upgrades.service", "active", "running",
         "Unattended Upgrades Shutdown"),
        ("user@1000.service", "active", "running", "User Manager for UID 1000"),
        # oneshots that already ran — "active/exited" on every real box
        ("apparmor.service", "active", "exited", "Load AppArmor profiles"),
        ("blk-availability.service", "active", "exited",
         "Availability of block devices"),
        ("console-setup.service", "active", "exited", "Set console font and keymap"),
        ("finalrd.service", "active", "exited",
         "Create final runtime dir for shutdown pivot root"),
        ("keyboard-setup.service", "active", "exited",
         "Set the console keyboard layout"),
        ("lvm2-monitor.service", "active", "exited",
         "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or "
         "progress polling"),
        ("setvtrgb.service", "active", "exited", "Set console scheme"),
        ("snapd.seeded.service", "active", "exited",
         "Wait until snapd is fully seeded"),
        ("systemd-user-sessions.service", "active", "exited",
         "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")  # intentionally empty: parser falls back to [all]
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # --- scheduled jobs (mk_job): both green (kept green to reduce noise;
    #     the single root-cause signal is the failed worker unit above). -----
    a("<<<job>>>")
    a("==> settlement-batch <==")
    a(f"start_time {now - 6 * 3600}")
    a("exit_code 0")
    a("real_time 4:12.8")
    a("user_time 3.20")
    a("system_time 0.90")
    a("max_res_kbytes 285000")
    a("avg_mem_kbytes 0")
    a("==> log-archive <==")
    a(f"start_time {now - 11 * 3600}")
    a("exit_code 0")
    a("real_time 1:02.4")
    a("user_time 0.80")
    a("system_time 0.30")
    a("max_res_kbytes 41000")
    a("avg_mem_kbytes 0")

    # --- trailing empty markers the real agent carries ---
    a("<<<vbox_guest>>>")
    a("<<<local:sep(0)>>>")

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence across restarts
#
#  Checkmk's rate-based checks (Disk IO, Kernel Performance, Interface)
#  abort with IgnoreResults when a counter goes BACKWARDS — and every process
#  restart used to re-seed all counters from scratch, marking exactly those
#  services stale until two fresh samples arrived. Persisting the accumulators
#  (plus START for uptime continuity and the incident state incl. its
#  timestamp, so a redeploy mid-demo doesn't reset the story) makes restarts
#  invisible to the monitoring: the next sample simply integrates the current
#  rate across the downtime gap.
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-payment-api-state.json")


def save_state() -> None:
    if not STATE_FILE:
        return
    with _state_lock:
        data = {
            "version": 1,
            "start": START,
            "broken": _broken,
            "broken_since": _broken_since,
            "state_since": _state_since,
            "counters": {name: [c.acc, c.last]
                         for name, c in _ALL_COUNTERS.items()},
        }
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        print(f"[state] save failed: {exc}")


def load_state() -> None:
    global START, _broken, _broken_since, _state_since
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
        _broken = data.get("broken", _broken)
        _broken_since = data.get("broken_since")
        _state_since = data.get("state_since", time.time())
        saved = data.get("counters", {})
        restored = 0
        for name, c in _ALL_COUNTERS.items():
            if name in saved:
                c.acc, c.last = saved[name]
                restored += 1
        # counters not in the file (added by a code update) keep their fresh
        # seeds — only those cost one IgnoreResults cycle, the rest carry on
    print(f"[state] restored: broken={_broken}, "
          f"{restored}/{len(_ALL_COUNTERS)} counters, uptime continuous")


# --------------------------------------------------------------------------- #
#  TCP agent server
# --------------------------------------------------------------------------- #
class AgentHandler(StreamRequestHandler):
    def handle(self) -> None:
        try:
            payload = build_agent_output(is_broken())
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Checkmk closed early; nothing to do
        save_state()  # cheap (~once a minute); survives restarts/reboots


class AgentServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------- #
#  HTTP service endpoint + control UI
# --------------------------------------------------------------------------- #
STATE_META = {
    "healthy": {
        "color": "#2e7d32", "label": "HEALTHY",
        "tagline": "All green — the picture after the incident is resolved.",
        "effects": [
            "HTTP payment-api: 200 OK in ~25 ms",
            "Systemd Service Summary OK — payment-worker.service running",
            "4 gunicorn workers, settlement worker alive, tx ~320 kB/s",
        ],
    },
    "broken": {
        "color": "#c62828", "label": "BROKEN",
        "tagline": "The incident the app's RED metrics point at — while the "
                   "HOST stays green (that's the trap: it's a blind spot "
                   "until you add it to Checkmk).",
        "effects": [
            f"HTTP payment-api → CRIT: 503, ~{BROKEN_DELAY_MS / 1000:.1f} s "
            "response time (the symptom)",
            "Systemd Service Summary → CRIT: payment-worker.service failed "
            "(the root cause)",
            "gunicorn workers 4 → 1; the survivor leaks ~6 MB/min — RSS and "
            "AnonPages grow live, poll by poll (green, graph-visible)",
            "TIME_WAIT creeps up from client retries, tx throughput collapses "
            "320 → ~55 kB/s over ~3 min (graphs only, no alerts)",
            "everything else stays green — wall of green, two reds, one story",
        ],
    },
}
ACTION_TO_STATE = {"heal": "healthy", "break": "broken"}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _admin_page() -> str:
    state = "broken" if is_broken() else "healthy"
    meta = STATE_META[state]
    extras = []
    if broken_seconds() > 0:
        leak_mb = worker_leak_kb() // 1024
        extras.append(f"broken for {_fmt_duration(broken_seconds())} — "
                      f"surviving gunicorn worker at ~{234 + leak_mb} MB RSS "
                      "and growing")
        extras.append(f"TIME_WAIT ~{34 + round(min(58.0, broken_seconds() / 30.0))} "
                      "and creeping (client retry storm)")
    extra_html = "".join(f"<div class='extra'>{e}</div>" for e in extras)

    cards = []
    for action, target in ACTION_TO_STATE.items():
        tmeta = STATE_META[target]
        current = target == state
        effects = "".join(f"<li>{e}</li>" for e in tmeta["effects"])
        btn = ("<span class='btn current'>current state</span>" if current else
               f"<a class='btn' href='/admin/{action}?ui=1' "
               f"style='background:{tmeta['color']}'>&rarr; {action}</a>")
        cards.append(
            f"<div class='card{' active' if current else ''}' "
            f"style='border-color:{tmeta['color']}'>"
            f"<h2 style='color:{tmeta['color']}'>{tmeta['label']}</h2>"
            f"<p class='tag'>{tmeta['tagline']}</p><ul>{effects}</ul>{btn}</div>")

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>{HOSTNAME} — demo control</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:60rem; padding:0 1rem; }}
 h1 {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }}
 h1 b {{ color:#d8dee4; }}
 .state {{ display:inline-block; padding:.4rem 1.1rem; border-radius:.4rem;
          color:#fff; font-weight:700; font-size:1.6rem; letter-spacing:.05em;
          background:{meta['color']}; }}
 .since {{ color:#9aa4af; margin:.6rem 0 0; }}
 .extra {{ color:#f9a825; margin-top:.3rem; }}
 .cards {{ display:flex; gap:1rem; margin-top:2rem; flex-wrap:wrap; }}
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
 <h1>demo control — <b>{HOSTNAME}</b> <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta['label']}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta['tagline']}</div>
 {extra_html}
 <div class="cards">{''.join(cards)}</div>
 <div class="foot">curl API: /admin/break · /admin/heal · /admin/status —
  the monitored endpoint is <code>/</code> (check_httpv2 points there).</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "payment-api/1.4"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
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
            return self._send(200, {
                "state": "broken" if is_broken() else "healthy",
                "in_state_for_s": round(state_since_seconds(), 1),
                "action_to_state": ACTION_TO_STATE,
                "states": STATE_META})

        if path == "/admin/status":
            return self._send(200, {
                "state": "broken" if is_broken() else "healthy",
                "in_state_for_s": round(state_since_seconds(), 1),
                "broken_for_s": round(broken_seconds(), 1),
                "surviving_worker_rss_mb": 234 + worker_leak_kb() // 1024
                if is_broken() else None,
                "toggles": ["/admin/break", "/admin/heal"],
                "ui": "/admin",
            })

        if path.startswith("/admin/") and (action := path[len("/admin/"):]) in ACTION_TO_STATE:
            target = ACTION_TO_STATE[action]
            set_broken(target == "broken")
            print(f"[ctl] -> {target.upper()}")
            if "ui=1" in query:  # button on the /admin screen: bounce back
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.end_headers()
                return None
            return self._send(200, {"state": target})

        # the actual monitored endpoint ("/" or "/health" or anything else).
        # Response time wanders (autocorrelated, not white noise) so the
        # check_httpv2 response-time graph looks organic in both states.
        if is_broken():
            if BROKEN_DELAY_MS > 0:
                delay = max(0.05, BROKEN_DELAY_MS / 1000.0
                            * gauge("http.delay", 1.0, amp_frac=0.30,
                                    phase=0.6, period=240))
                time.sleep(delay)
            return self._send(503, {
                "status": "error",
                "service": "payment-api",
                "detail": "upstream connection pool exhausted",
            })
        time.sleep(max(0.004, gauge("http.ok_delay", 0.022, amp_frac=0.5,
                                    phase=1.4, period=300)))
        return self._send(200, {
            "status": "ok",
            "service": "payment-api",
            "uptime_s": int(time.time() - START),
        })


def main() -> None:
    load_state()  # restart-proof counters/uptime/incident state
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104

    threading.Thread(target=agent.serve_forever, daemon=True).start()
    state = "BROKEN" if is_broken() else "healthy"
    print(f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  http=tcp/{HTTP_PORT}  "
          f"start_state={state}")
    print(f"[boot] control UI:   http://localhost:{HTTP_PORT}/admin")
    print(f"[boot] curl API:     curl localhost:{HTTP_PORT}/admin/break|/admin/heal|/admin/status")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
