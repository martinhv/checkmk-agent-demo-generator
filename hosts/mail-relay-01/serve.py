#!/usr/bin/env python3
"""Meridian Retail demo host: mail-relay-01 — the transactional mail relay.

An Ubuntu 24.04 Postfix relay (`postfix@-.service`) that injects and forwards
the shop's transactional mail (order confirmations, receipts) to an upstream
smarthost / downstream MX. The incident is a clean, single-root-cause story:
the downstream MX becomes unreachable, so outbound mail can no longer be
delivered and Postfix keeps re-queueing it — the DEFERRED queue backs up while
the ACTIVE queue and Postfix itself stay perfectly healthy (local injection
still works). The AI fuses "deferred queue growing + active queue fine +
Postfix status OK" into the root cause: *the relay host is healthy; the
downstream MX is unreachable — fix the relay target / DNS, don't touch this
box.*

Three states (the timeline is part of the story):

  healthy   deferred ~1-3 mails, active ~2-6. Mail flows. all green.
  degraded  the MX gets slow/flaky: a fraction of deliveries start failing, so
            the deferred queue climbs (4 -> ~18) but stays UNDER the CRIT (20).
            The breadcrumb — Postfix Queue still OK/green, but the deferred
            graph is visibly rising. Trigger ~15-20 min before showtime.
  broken    the MX is unreachable: nothing outbound delivers, the deferred
            queue grows LIVE past 20 -> Postfix Queue (deferred) CRIT. active
            stays small (local injection still works). One red, one root cause.

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env (see also AGENT_PORT/HTTP_PORT/START_STATE/STATE_FILE):
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before the MX goes fully
                 unreachable (default: 18; 0 disables)
  DEFER_CLIMB_MIN  minutes for the deferred queue to climb to its degraded
                 plateau (~18 mails) while degraded (default: 16)
  BREAK_RAMP_MIN minutes over which the broken deferred-growth rate reaches
                 full force (default: 3; 0 = instant)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "mail-relay-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "18"))
DEFER_CLIMB_MIN = float(os.environ.get("DEFER_CLIMB_MIN", "16"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "3"))

START = time.time()
UPTIME_OFFSET = 12 * 86400  # pretend the host has been up ~12 days
POSTFIX_MASTER_PID = 712

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the MX started flaking (degraded or broken) -> drives the rising queue
_degraded_since: float | None = None if _state == "healthy" else START
# when the MX went fully unreachable -> drives the live-growing deferred queue
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


# --------------------------------------------------------------------------- #
#  The single driver of the whole incident: the deferred-queue length.
#
#    healthy  : 1-3 mails (autocorrelated wander, never near 10)
#    degraded : climbs from the healthy base toward ~18 over DEFER_CLIMB_MIN
#               and plateaus there — clearly rising, still under the CRIT (20).
#    broken   : the MX is unreachable; deferred grows LIVE and unbounded as a
#               function of broken-time (the dying-disk "stuck query grows"
#               pattern) — ~0.8 mail/s once the break ramp is full, so it
#               crosses 20 within a couple of polls and keeps climbing.
#
#  The active queue stays small in every state: local injection always works,
#  Postfix itself is fine — this is what tells the AI the box is healthy.
# --------------------------------------------------------------------------- #
DEFER_DEGRADED_PLATEAU = 18.0  # mails (just under the 20 CRIT)
DEFER_BROKEN_RATE = 0.80  # mails/s accumulating while fully unreachable


def deferred_count() -> int:
    base = gauge("mailq.deferred_base", 2.0, amp_abs=1.2, phase=0.7, period=600)
    base = max(0.0, base)

    ds = degraded_seconds()
    deferred = base
    if ds > 0:
        climb = 1.0 if DEFER_CLIMB_MIN <= 0 else min(1.0, ds / (DEFER_CLIMB_MIN * 60.0))
        deferred = _lerp(base, DEFER_DEGRADED_PLATEAU, climb)

    bs = broken_seconds()
    if bs > 0:
        # live, monotonic growth from the moment of the break. Integrate the
        # ramped rate so flipping degraded->broken never makes it jump back.
        deferred = max(deferred, DEFER_DEGRADED_PLATEAU + DEFER_BROKEN_RATE * bs * break_ramp(1.0))
    return int(round(deferred))


def active_count() -> int:
    # local injection always works -> active queue is small + healthy always.
    return max(0, int(round(gauge("mailq.active", 4.0, amp_abs=2.0, phase=2.1, period=420))))


def deferred_bytes(count: int) -> int:
    # ~42 KiB per transactional mail (HTML receipt + headers), wobbled a touch.
    return int(count * gauge("mailq.avg_size", 43_000, amp_frac=0.05, phase=1.3, period=900))


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


# /proc/stat jiffies: 100 Hz * 2 CPUs = ~200 ticks/s. A mail relay is light:
# mostly idle, a little user/system for the smtp/cleanup/qmgr processes. The
# deferred backlog does NOT burn CPU (queued mail just sits) -> CPU stays calm
# and green in every state, corroborating "the box is fine".
C_USER = Counter("cpu.user", phase=0.3, start=_aged(14))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(7))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(176))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(2))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(1800))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(3))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(0.3))

SDA = {  # single system SSD; root + the mail spool live here. Calm.
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(4)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(3)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(14)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(11)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(16)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(90_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(70_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(310))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(280))


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
                    "raw": {"value": 19},
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


def filesystem_usage(now: float) -> tuple[int, int]:
    """root / and /var/spool/postfix — both green, growing + cleaned over time.

    The mail spool grows with the deferred backlog (queued mail is files on
    disk) but a relay's spool volume is sized huge relative to a few thousand
    small mails, so even a big backlog is well under the 80/90 % df levels —
    green corroboration: the queue is backed up, the disk is fine.
    """
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0
    root_base = 11_534_336  # ~11 GiB of 40
    root_logs = 1_048_576 * ((now % day) / day)  # 0..1 GiB daily mail.log
    root_growth = min(1_048_576, uptime * 0.03)
    root_used = int(
        root_base + root_logs + root_growth + gauge("fs.root", 0, amp_abs=70_000, period=1500)
    )
    # spool: small base + the deferred backlog as files (~42 KiB each) + slow
    # creep, cleaned as mail eventually drains. Stays tiny vs the 20 GiB volume.
    spool_base = 1_572_864  # ~1.5 GiB of 20
    spool_queue = deferred_count() * 42  # KiB of queued mail
    spool_growth = min(262_144, uptime * 0.05)
    spool_used = int(
        spool_base + spool_queue + spool_growth + gauge("fs.spool", 0, amp_abs=40_000, period=900)
    )
    return root_used, spool_used


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 2
    broken = state == "broken"

    deferred = deferred_count()
    active = active_count()
    deferred_sz = deferred_bytes(deferred)
    active_sz = deferred_bytes(active) // 3  # active mail is mid-delivery, smaller spread

    # ---- memory: a light, healthy 8 GiB relay. Nothing here ever alarms; the
    #      backlog lives on disk, not in RAM. Full Ubuntu 24.04 meminfo, self-
    #      consistent LRU arithmetic. -------------------------------------- #
    mem_total = 8_175_104  # kB (~8 GiB)
    swap_total = 2_097_148
    commit_limit = swap_total + mem_total // 2
    mem_used_t = gauge("mem.used", 1_750_000, amp_frac=0.03, phase=0.5, period=1700)
    cached = int(gauge("mem.cached", 3_900_000, amp_frac=0.02, phase=0.4, period=1500))
    buffers = int(gauge("mem.buffers", 210_000, amp_frac=0.03, phase=1.1, period=1300))
    sreclaim = 196_608
    swapcached = 0
    caches = cached + buffers + swapcached + sreclaim
    mem_free = max(160_000, mem_total - int(mem_used_t) - caches)
    swap_free = swap_total  # swap empty: a healthy, unloaded relay
    committed = int(gauge("mem.committed", 2_700_000, amp_frac=0.01, phase=1.2, period=1700))

    shmem = 24_576
    anon = max(900_000, mem_total - mem_free - caches - 360_000)
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = max(mem_free, mem_free + file_lru + sreclaim)
    a_anon = int(anon_lru * 0.55)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.33)
    i_file = file_lru - a_file
    slab = sreclaim + 78_848
    threads = int(gauge("kernel.threads", 210, amp_abs=8, phase=2.0, period=1100))
    kernel_stack = threads * 16
    dirty = max(2_048, int(gauge("mem.dirty", 6_144, amp_frac=0.15, phase=2.0, period=800)))

    # ---- load: a near-idle relay. Stays GREEN always (deferred mail does not
    #      cost CPU — that's a key tell the box is fine). 15-min well under the
    #      per-core 5/10 default (2 cores -> CRIT only above 20). ----------- #
    base_l = gauge("load.base", 0.22, amp_frac=0.30, phase=0.2, period=600)
    base_l = max(0.05, base_l)
    l1 = round(base_l * gauge("load1", 1.0, amp_frac=0.25, phase=0.2, period=300), 2)
    l5 = round(base_l * 0.95 * gauge("load5", 1.0, amp_frac=0.14, phase=1.0, period=900), 2)
    l15 = round(base_l * 0.9 * gauge("load15", 1.0, amp_frac=0.07, phase=2.0, period=2400), 2)
    runnable = 1
    total_procs = round(gauge("procs.total", 188, amp_abs=6, phase=1.4, period=1500))

    # ---- /proc/stat: light, calm in every state ------------------------- #
    user = C_USER.sample(14)
    system = C_SYSTEM.sample(7)
    idle = C_IDLE.sample(176)
    iowait = C_IOWAIT.sample(2)
    pgmaj_rate = 0.3

    # ---- diskstat: single SSD, calm. The spool sees a little extra write as
    #      the backlog grows but nothing dramatic. ----------------------- #
    sda_rd = SDA["rd_ios"].sample(4)
    sda_rdt = SDA["rd_ticks"].sample(3)
    sda_wr = SDA["wr_ios"].sample(14 + (8 if broken else 0))
    sda_wrt = SDA["wr_ticks"].sample(11 + (6 if broken else 0))
    sda_iot = SDA["io_ticks"].sample(16 + (10 if broken else 0))

    # outbound network DROPS when the MX is unreachable (nothing delivers) —
    # corroboration, not an alarm. Inbound (local injection) is steady.
    tx_rate = _lerp(70_000, 9_000, break_ramp(1.0)) if broken_seconds() > 0 else 70_000
    rx_bytes = C_RX_B.sample(90_000)
    tx_bytes = C_TX_B.sample(tx_rate)
    rx_pkts = C_RX_P.sample(310)
    tx_pkts = C_TX_P.sample(_lerp(280, 60, break_ramp(1.0)) if broken_seconds() > 0 else 280)

    sda_temp = round(gauge("smart.sda.temp", 29, amp_abs=1.2, phase=2.1, period=1100))
    sda_smart = _smart_json(
        "/dev/sda",
        "INTEL SSDSC2KB240G8",
        "PHYF019300NT240AGN",
        int(uptime / 3600) + 21000,
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
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now + 321 * 86400))
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
                        "uuid": "7c3a91e4-5b2d-4f18-a0c6-9e1d4a8b3f57",
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
    a(f'/opt/checkmk/agent/default/package/plugins/mk_postfix:CMK_VERSION="{AGENT_VERSION}"')

    a("<<<df_v2>>>")
    root_size = 41_943_040
    spool_size = 20_971_520
    root_used, spool_used = filesystem_usage(time.time())
    a(
        f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
        f"{round(root_used / root_size * 100)}% /"
    )
    a(
        f"/dev/sda2 ext4 {spool_size} {spool_used} {spool_size - spool_used} "
        f"{round(spool_used / spool_size * 100)}% /var/spool/postfix"
    )
    a("[df_inodes_start]")
    # a mail spool holds MANY small files -> noticeable inode use that tracks
    # the queue depth, but a 20 GiB volume still has plenty of inodes spare.
    spool_inodes_used = 38_000 + deferred + active
    a(f"/dev/sda1 ext4 2621440 248114 {2621440 - 248114} 10% /")
    a(
        f"/dev/sda2 ext4 1310720 {spool_inodes_used} {1310720 - spool_inodes_used} "
        f"{max(1, round(spool_inodes_used / 1310720 * 100))}% /var/spool/postfix"
    )
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sda2 /var/spool/postfix ext4 rw,relatime,noatime 0 0")

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
    a("Mapped:         168320 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     78848 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     32768 kB")
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
    a("DirectMap4k:    178176 kB")
    a("DirectMap2M:    4016128 kB")
    a("DirectMap1G:    4194304 kB")

    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {18000 + C_PROC.sample(3) % 9999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 1.8)}.00")

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
    a("Root distance: 11.402ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 18.221ms")
    a(f"       Jitter: {random.randint(800, 3200) / 1000:.3f}ms")
    a(f" Packet count: {610 + int((time.time() - START) / 2048)}")
    a("    Frequency: +7.842ppm")
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
    a(f"ctxt {C_CTXT.sample(1800)}")
    a(f"processes {C_PROC.sample(3)}")
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
    a("    link/ether 02:42:ac:11:00:34 brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:34")

    a("<<<tcp_conn_stats>>>")
    # ESTABLISHED smtp conns drop when the MX is unreachable; SYN_SENT to the
    # dead MX may rise — corroboration only, no default alert on these.
    a(f"01 {round(gauge('tcp.estab', 14, amp_abs=4, phase=0.9, period=700))}")
    synsent = round(gauge("tcp.synsent", 0, amp_abs=1, phase=1.5, period=400))
    a(f"02 {synsent + (3 if broken else 0)}")
    a(f"06 {round(gauge('tcp.timewait', 7, amp_abs=3, phase=2.4, period=500))}")
    a("0A 3")

    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # ---- POSTFIX: the incident sections. ------------------------------------
    #  postfix_mailq (plain, whitespace-sep): per-instance marker [[[default]]]
    #  then "QUEUE_<name> <size_bytes> <count>" for deferred + active. The check
    #  service is "Postfix Queue default"; deferred default levels (10, 20),
    #  active (200, 300). Only deferred is engineered to cross its levels.
    a("<<<postfix_mailq>>>")
    a("[[[default]]]")
    a(f"QUEUE_deferred {deferred_sz} {deferred}")
    a(f"QUEUE_active {active_sz} {active}")
    #  postfix_mailq_status (sep 58 = colon): "<instance>:<status>:PID:<pid>".
    #  Postfix itself is ALWAYS running -> "Postfix status default" stays OK
    #  even when the queue is backed up — the tell that the box is healthy.
    a("<<<postfix_mailq_status:sep(58)>>>")
    a(f"default:the Postfix mail system is running:PID:{POSTFIX_MASTER_PID}")

    # ---- processes: the postfix master + its children (qmgr/pickup/smtp/...) #
    #      + the usual Ubuntu daemons. ~14 processes. ----------------------- #
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    procs = [
        ("init.scope", "root", 167_800, 11_900, "00:00:24", 1, "/sbin/init"),
        (
            "system.slice/systemd-journald.service",
            "root",
            56_100,
            16_800,
            "00:00:58",
            398,
            "/usr/lib/systemd/systemd-journald",
        ),
        (
            "system.slice/systemd-udevd.service",
            "root",
            25_300,
            7_100,
            "00:00:02",
            431,
            "/usr/lib/systemd/systemd-udevd",
        ),
        (
            "system.slice/systemd-resolved.service",
            "systemd-resolve",
            26_400,
            12_700,
            "00:00:31",
            472,
            "/usr/lib/systemd/systemd-resolved",
        ),
        (
            "system.slice/systemd-timesyncd.service",
            "systemd-timesync",
            90_900,
            7_300,
            "00:00:08",
            486,
            "/usr/lib/systemd/systemd-timesyncd",
        ),
        (
            "system.slice/dbus.service",
            "messagebus",
            9_900,
            4_900,
            "00:00:12",
            498,
            "@dbus-daemon --system --address=systemd:",
        ),
        (
            "system.slice/rsyslog.service",
            "syslog",
            221_800,
            6_300,
            "00:00:27",
            561,
            "/usr/sbin/rsyslogd -n -iNONE",
        ),
        (
            "system.slice/ssh.service",
            "root",
            15_400,
            8_800,
            "00:00:01",
            640,
            "sshd: /usr/sbin/sshd -D [listener]",
        ),
        (
            "system.slice/cron.service",
            "root",
            11_500,
            2_400,
            "00:00:02",
            655,
            "/usr/sbin/cron -f -P",
        ),
        # the postfix family: master + persistent children.
        (
            "system.slice/postfix@-.service",
            "root",
            44_800,
            6_200,
            "00:00:46",
            POSTFIX_MASTER_PID,
            "/usr/lib/postfix/sbin/master -w",
        ),
        (
            "system.slice/postfix@-.service",
            "postfix",
            45_100,
            6_600,
            "00:00:33",
            POSTFIX_MASTER_PID + 4,
            "qmgr -l -t unix -u",
        ),
        (
            "system.slice/postfix@-.service",
            "postfix",
            44_600,
            5_400,
            "00:00:11",
            POSTFIX_MASTER_PID + 5,
            "pickup -l -t unix -u",
        ),
        (
            "system.slice/postfix@-.service",
            "postfix",
            45_300,
            6_800,
            "00:00:19",
            POSTFIX_MASTER_PID + 6,
            "tlsmgr -l -t unix -u",
        ),
    ]
    for cgs, usr, vsz, rss, cputime, pid, cmd in procs:
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 12-00:31:40 {pid} {cmd}")
    # smtp delivery agents: a couple while healthy; while the MX is unreachable
    # qmgr keeps spawning smtp clients that hang on the dead MX -> more of them.
    n_smtp = 1 + (4 if broken else (2 if degraded_seconds() > 0 else 0))
    for i in range(n_smtp):
        a(
            f"0::/system.slice/postfix@-.service postfix 45200 6700 "
            f"00:00:0{i % 9} 0-00:0{i}:1{i} {POSTFIX_MASTER_PID + 20 + i} "
            "smtp -t unix -u"
        )

    # ---- systemd units: ~30, ALL green incl. postfix@-.service active/running
    #      (the box is healthy in every state — the queue backs up, the daemon
    #      does not fail). No Service-Summary alarm: this story is queue-only. - #
    a("<<<systemd_units>>>")
    units = [
        ("postfix@-.service", "active", "running", "Postfix Mail Transport Agent (instance -)"),
        ("postfix.service", "active", "exited", "Postfix Mail Transport Agent"),
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

    # ---- scheduled job: nightly log rotation / queue housekeeping (green) -- #
    a("<<<job>>>")
    a("==> postfix-queue-report <==")
    a(f"start_time {now - 7 * 3600}")
    a("exit_code 0")
    a("real_time 0:21.4")
    a("user_time 0.40")
    a("system_time 0.18")
    a("max_res_kbytes 24000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence (counters/uptime/incident — see CLAUDE.md)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-mail-relay-state.json")


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
        "tagline": "Mail flows. Deferred queue ~1-3, active ~2-6, Postfix running. all green.",
        "effects": [
            "every service OK — the starting picture",
            "Postfix Queue default: deferred ~1-3 (levels 10/20), active ~2-6",
            "Postfix status default: OK, the Postfix mail system is running",
        ],
    },
    "degraded": {
        "color": "#f9a825",
        "label": "DEGRADED",
        "tagline": "The downstream MX gets slow/flaky — a fraction of deliveries fail, the "
        "deferred queue climbs (4 -> ~18) but stays UNDER the CRIT. "
        + (
            f"Auto-escalates after {AUTO_BREAK_AFTER_MIN:g} min."
            if AUTO_BREAK_AFTER_MIN > 0
            else ""
        ),
        "effects": [
            "Postfix Queue default: deferred queue climbs toward ~18 over "
            f"{DEFER_CLIMB_MIN:g} min — visibly rising graph, "
            "still OK/green (< 20) — the breadcrumb",
            "active queue stays small (~2-6); local injection still works",
            "Postfix status still OK; outbound bandwidth begins to sag",
        ],
    },
    "broken": {
        "color": "#c62828",
        "label": "BROKEN",
        "tagline": "The downstream MX is unreachable. Nothing outbound delivers — the deferred "
        "queue grows LIVE past 20 and keeps climbing."
        + (f" Ramps over ~{BREAK_RAMP_MIN:g} min." if BREAK_RAMP_MIN > 0 else " Instant."),
        "effects": [
            "Postfix Queue default: deferred > 20 and GROWING live across re-polls "
            "(default levels 10/20) -> CRIT — the headline that pages",
            "active queue STILL small; Postfix status STILL OK — the box is healthy",
            "outbound bandwidth / ESTABLISHED smtp conns drop, SYN_SENT to the dead MX rises",
            "CPU, load, memory, disk all GREEN — the AI fuses 'deferred growing + active "
            "fine + postfix up' into 'downstream MX unreachable; fix the relay target/DNS, "
            "not this host'",
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
    extras: list[str] = []
    if degraded_seconds() > 0:
        extras.append(
            f"MX flaky for {_fmt_duration(degraded_seconds())} — "
            f"deferred queue ~{deferred_count()} mails"
        )
    if broken_seconds() > 0:
        extras.append(
            f"MX unreachable for {_fmt_duration(broken_seconds())} — "
            f"deferred queue {deferred_count()} and growing live"
        )
        if break_ramp() < 1.0:
            extras.append(f"deferred-growth ramping: {break_ramp() * 100:.0f} %")
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"MX goes fully unreachable in {_fmt_duration(left)}")
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
    server_version = "mail-relay-demo-ctl/1.0"

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
                "deferred_queue": deferred_count(),
                "active_queue": active_count(),
                "mx_flaky_for_s": round(degraded_seconds(), 1),
                "mx_unreachable_for_s": round(broken_seconds(), 1),
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
            print(f"[ctl] -> BROKEN (auto: MX unreachable after {AUTO_BREAK_AFTER_MIN:g} min)")


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(
            f"[boot] auto-escalation: degraded -> broken (MX unreachable) after "
            f"{AUTO_BREAK_AFTER_MIN:g} min"
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
