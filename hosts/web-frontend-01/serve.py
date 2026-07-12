#!/usr/bin/env python3
"""Meridian Retail demo host: web-frontend-01 — nginx reverse proxy / TLS termination.

The estate's front door: an Ubuntu 24.04 box running nginx as a TLS-terminating
reverse proxy for the Meridian Retail platform (payment-api and the app tier
behind it). This is a STEADY-GREEN background host — no incident, no toggle.
It exists so the monitoring estate looks like a real company, not a pile of
test boxes.

Characteristics that sell an edge box vs a backend:
  - Higher network throughput than the app boxes (it's the ingress)
  - More ESTABLISHED TCP connections (~120, client churn means more TIME_WAIT)
  - Moderate CPU load (~0.6), plenty of free memory
  - nginx master + 4 workers in ps_lnx; nginx.service active/running
  - Single healthy SATA SSD (system disk); no data volumes

All counters and gauges gently wobble — no static lines, but nothing ever
crosses an alert threshold.

Config via env:
  CMK_HOSTNAME   reported hostname (default: web-frontend-01.corp.meridian-retail.com)
  AGENT_PORT     plaintext TCP agent port (default: 6556, container)
  HTTP_PORT      admin HTTP port (default: 8080, container)
  STATE_FILE     counter persistence file (default: /var/tmp/cmk-demo-web-frontend-state.json)
  AGENT_VERSION  reported agent version string
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "web-frontend-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")

START = time.time()
# Pretend the host has been up ~14 days — an edge box that gets patched regularly
UPTIME_OFFSET = 14 * 86400

_state_lock = threading.Lock()
_state_since: float = START


def state_since_seconds() -> float:
    with _state_lock:
        return time.time() - _state_since


# ---------------------------------------------------------------------------
# Autocorrelated gauges + monotonic counters — identical machinery to the
# reference implementation (app-worker-01/serve.py). See CLAUDE.md for why a
# single sine is wrong.
# ---------------------------------------------------------------------------
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
    """Seed a counter with the value it would have after UPTIME_OFFSET seconds."""
    return rate_per_s * UPTIME_OFFSET


# ---------------------------------------------------------------------------
# Counters — nginx edge box: moderate CPU, busy network, mostly-idle disk.
# 4 CPUs at 100 Hz = 400 ticks/s total; an edge proxy is ~15 % user / 5 %
# system / 2 % iowait → the load stays comfortably green (0.6 / 4 cores).
# ---------------------------------------------------------------------------
C_USER = Counter("cpu.user", phase=0.3, start=_aged(60))  # ~15 % of 400 ticks/s
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(20))  # ~5 %
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(310))  # ~77 %
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(8))  # ~2 %

C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(5_500))  # context switches
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(6))  # fork rate
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, amp=0.25, start=_aged(0.2))  # near zero

# Single SATA SSD: system + /var/log. Edge proxies are network-heavy, disk-light.
SDA = {
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(3)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(2)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(12)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(8)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(10)),
}

# Network: ingress is busier than any app box — 600+ Mbps bursts in nginx land.
# Steady average ~8 MB/s rx, ~6 MB/s tx (edge proxy, TLS offloaded, lots of
# small request/response pairs).  Packet rate ~4000 rx / 3400 tx per second.
C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(8_000_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(6_000_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(4_000))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(3_400))


# ---------------------------------------------------------------------------
# SMART: one healthy SATA SSD. Raw values zero — discovery baseline never exceeded.
# ---------------------------------------------------------------------------
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
                    "raw": {"value": 31},
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
                    "value": 93,
                    "thresh": 5,
                    "raw": {"value": 147},
                },
                {
                    "id": 179,
                    "name": "Used_Rsvd_Blk_Cnt_Tot",
                    "value": 100,
                    "thresh": 10,
                    "raw": {"value": 0},
                },
            ]
        },
    }
    return json.dumps(doc, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Filesystem — root (40 GiB) + /var/log (10 GiB partition). nginx access logs
# accumulate slowly; logrotate trims daily. Both stay well under 80 % df WARN.
# ---------------------------------------------------------------------------
def filesystem_usage(now: float) -> tuple[int, int]:
    """Return (root_used_kB, varlog_used_kB)."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: ~11 GiB base, slow growth capped at +1.5 GiB, stays at ~30–32 %
    root_base = 11_534_336  # ~11 GiB of 40
    root_growth = min(1_572_864, uptime * 0.03)  # slow package growth
    root_used = int(root_base + root_growth + gauge("fs.root", 0, amp_abs=65_536, period=1800))

    # /var/log: nginx access/error logs fill up, logrotate runs daily at ~02:00
    # → sawtooth with 24-h period, base 1.8 GiB of 10, peak ~3.5 GiB (~35 %)
    log_base = 1_887_437  # ~1.8 GiB base
    log_daily = 1_703_936 * ((now % day) / day)  # 0..1.6 GiB saw
    log_used = int(log_base + log_daily + gauge("fs.log", 0, amp_abs=32_768, period=1200))

    return root_used, log_used


# ---------------------------------------------------------------------------
# Agent output — the whole section set
# ---------------------------------------------------------------------------
def build_agent_output() -> bytes:  # noqa: PLR0912, PLR0915
    now = int(time.time())
    nowf = time.time()
    uptime = int(nowf - START) + UPTIME_OFFSET
    ncpu = 4

    # -----------------------------------------------------------------------
    # Memory: a proxy keeps most RAM in page cache (kernel buffer for TLS I/O);
    # real used ~5.5 GiB of 16 GiB. No swap. Well clear of all thresholds.
    # Shmem ~640 MiB — nginx worker shared memory zones (limit_req, upstream).
    # CommitLimit = SwapTotal(0) + RAM/2 = 8 GiB.
    # -----------------------------------------------------------------------
    mem_total = 16_384_000  # kB
    swap_total = 0  # no swap on a proxy — common in cloud setups
    commit_limit = mem_total // 2  # kernel default with no swap

    # Cache is healthy — kernel buffers TLS read-ahead and accept queues
    cached = int(gauge("mem.cached", 6_400_000, amp_frac=0.03, phase=0.4, period=1500))
    buffers = int(gauge("mem.buffers", 380_000, amp_frac=0.04, phase=1.2, period=1100))
    sreclaim = int(gauge("mem.srec", 512_000, amp_frac=0.03, phase=2.0, period=1300))
    swapcached = 0
    caches = cached + buffers + swapcached + sreclaim

    # anon usage: nginx worker processes + OS
    shmem = int(gauge("mem.shmem", 655_360, amp_frac=0.02, phase=0.8, period=1600))  # ~640 MiB
    anon = int(gauge("mem.anon", 1_900_000, amp_frac=0.03, phase=1.5, period=1400))
    mem_free = max(200_000, mem_total - anon - shmem - caches)

    # LRU split — Active(anon)+Inactive(anon) = AnonPages+Shmem
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = mem_free + file_lru + sreclaim

    a_anon = int(anon_lru * 0.62)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.38)
    i_file = file_lru - a_file

    slab = sreclaim + 138_240  # SUnreclaim ~135 MiB for a busy kernel
    threads = 340  # nginx master + 4 workers + OS threads
    kernel_stack = threads * 16  # kB
    dirty = max(4_096, int(gauge("mem.dirty", 12_288, amp_frac=0.20, phase=2.2, period=900)))

    # Committed_AS: nginx overcommits a little for worker stacks; stays < 50 %
    # of CommitLimit (no WARN at 100 %)
    committed = int(gauge("mem.committed", 3_500_000, amp_frac=0.03, phase=0.9, period=1700))

    # -----------------------------------------------------------------------
    # Load: moderate (0.6 per 4 cores = 0.15 total average). The nginx workers
    # are mostly I/O-bound; the box rarely exceeds 1 on the 15-min average.
    # Default levels: 15-min per-core WARN at 5.0, CRIT at 10.0 → we need
    # l15 < 20 for 4 cores (and we're at ~0.6).
    # -----------------------------------------------------------------------
    base_l = 0.60
    l1 = round(base_l * gauge("load1", 1.0, amp_frac=0.30, phase=0.2, period=300), 2)
    l5 = round(base_l * 0.92 * gauge("load5", 1.0, amp_frac=0.18, phase=1.1, period=900), 2)
    l15 = round(base_l * 0.85 * gauge("load15", 1.0, amp_frac=0.10, phase=2.1, period=2400), 2)
    # Clamp: load must be positive
    l1 = max(0.01, l1)
    l5 = max(0.01, l5)
    l15 = max(0.01, l15)
    runnable = 2
    total_procs = 280

    # /proc/stat counters
    user = C_USER.sample(60)
    system = C_SYSTEM.sample(20)
    idle = C_IDLE.sample(310)
    iowait = C_IOWAIT.sample(8)

    # Disk I/O
    sda_rd = SDA["rd_ios"].sample(3)
    sda_rdt = SDA["rd_ticks"].sample(2)
    sda_wr = SDA["wr_ios"].sample(12)
    sda_wrt = SDA["wr_ticks"].sample(8)
    sda_iot = SDA["io_ticks"].sample(10)

    # Network — rate wobbles around the base; stays clearly within green.
    # 8 MB/s rx ~ 64 Mbit/s — moderate for an edge box, well under 1G.
    rx_bytes = C_RX_B.sample(8_000_000)
    tx_bytes = C_TX_B.sample(6_000_000)
    rx_pkts = C_RX_P.sample(4_000)
    tx_pkts = C_TX_P.sample(3_400)

    # SMART temperature: healthy SATA SSD, 27 ±1.3 °C (well below 35 °C WARN)
    sda_temp = round(gauge("smart.sda.temp", 27.0, amp_abs=1.3, phase=2.1, period=1100))
    sda_hours = int(uptime / 3600) + 9_800  # ~9 800 hours power-on before we started
    sda_smart = _smart_json(
        "/dev/sda", "Samsung SSD 870 EVO 500GB", "S5GYNX0R214807", sda_hours, sda_temp
    )

    # Filesystems
    root_used, log_used = filesystem_usage(nowf)
    root_size = 41_943_040  # 40 GiB in kB
    log_size = 10_485_760  # 10 GiB in kB

    # -----------------------------------------------------------------------
    # Build the output line-by-line
    # -----------------------------------------------------------------------
    lines: list[str] = []
    a = lines.append

    # --- check_mk header ---------------------------------------------------
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

    # --- TLS-registration pretend -----------------------------------------
    a("<<<cmk_agent_ctl_status:sep(0)>>>")
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now + 330 * 86400))
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
                        "uuid": "c4f8a230-3e11-4b7d-ae91-6d5c9f02b811",
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

    # --- deployed plugins -------------------------------------------------
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"' % AGENT_VERSION)

    # --- filesystems ------------------------------------------------------
    a("<<<df_v2>>>")
    a(
        f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
        f"{round(root_used / root_size * 100)}% /"
    )
    a(
        f"/dev/sda2 ext4 {log_size} {log_used} {log_size - log_used} "
        f"{round(log_used / log_size * 100)}% /var/log"
    )
    a("[df_inodes_start]")
    a(f"/dev/sda1 ext4 2621440 198432 {2621440 - 198432} 8% /")
    a(f"/dev/sda2 ext4 655360  86210  {655360 - 86210} 14% /var/log")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sda2 /var/log ext4 rw,noatime 0 0")

    # --- /proc/meminfo (full 58-key Ubuntu 24.04 set) ---------------------
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
    a(f"SwapFree:       {swap_total} kB")
    a("Zswap:          0 kB")
    a("Zswapped:       0 kB")
    a(f"Dirty:          {dirty} kB")
    a("Writeback:      0 kB")
    a(f"AnonPages:      {anon} kB")
    a("Mapped:         380928 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     138240 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     62464 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    51200 kB")
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
    a("DirectMap4k:    237568 kB")
    a("DirectMap2M:    6029312 kB")
    a("DirectMap1G:    11534336 kB")

    # --- CPU load ---------------------------------------------------------
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {28000 + C_PROC.sample(6) % 9999} {ncpu}")

    # --- uptime -----------------------------------------------------------
    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.4)}.00")

    # --- timesyncd (dynamic timestamps) -----------------------------------
    # sawtooths 0->34min (poll interval), anchored to boot so it's continuous
    # across restarts and independent of push-lagged payload timestamps.
    last_sync = now - int((now - START) % 2048)
    sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = int(gauge("ntp.offset", 0, amp_abs=1200, phase=1.3, period=600))
    jitter_ms = round(gauge("ntp.jitter", 1.8, amp_abs=0.6, phase=0.7, period=700), 3)
    jitter_ms = max(0.1, jitter_ms)
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.57 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: B97D5A39")
    a("    Precision: 1us (-25)")
    a("Root distance: 10.781ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 17.442ms")
    a(f"       Jitter: {jitter_ms:.3f}ms")
    a(f" Packet count: {588 + int((nowf - START) / 2048)}")
    a("    Frequency: +11.337ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a(
        "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
        "RootDelay=8.912ms, RootDispersion=1.047ms, Reference=B97D5A39, "
        f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
        f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
        "Ignored=no, PacketCount=61, Jitter=1.284ms }"
    )
    a("Timezone=UTC")

    # --- apt --------------------------------------------------------------
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # --- kernel -----------------------------------------------------------
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(5_500)}")
    a(f"processes {C_PROC.sample(6)}")
    a(f"pgmajfault {C_PGMAJ.sample(0.2)}")

    # --- diskstat ---------------------------------------------------------
    a("<<<diskstat>>>")
    a(str(now))
    # fields: maj min name rdios rdmerges rdsects rdticks wrios wrmerges wrsects wrticks
    #         cur_ios ioticks timeinqueue discards dsectors dsticks flushios flushticks
    a(
        f"8 0 sda {sda_rd} 0 {sda_rd * 16} {sda_rdt} {sda_wr} 0 "
        f"{sda_wr * 32} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0"
    )

    # --- lnx_if (both variants required — see CLAUDE.md) ------------------
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a(
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
        "state UP group default qlen 1000"
    )
    a("    link/ether 02:42:ac:11:00:30 brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:30")

    # --- TCP connections: edge proxy has many ESTABLISHEDs + TIME_WAITs ---
    # ESTABLISHED ~120 (client sessions + upstream keep-alive to payment-api)
    # TIME_WAIT ~80 (short-lived client connections draining)
    # SYN_SENT ~2, LISTEN 8 (nginx + sshd + systemd sockets)
    estab = round(gauge("tcp.estab", 120, amp_abs=18, phase=0.9, period=700))
    tw = round(gauge("tcp.timewait", 80, amp_abs=15, phase=2.4, period=500))
    a("<<<tcp_conn_stats>>>")
    a(f"01 {max(80, estab)}")  # ESTABLISHED
    a(f"02 {random.randint(0, 2)}")  # SYN_SENT
    a(f"06 {max(30, tw)}")  # TIME_WAIT
    a("0A 8")  # LISTEN

    # --- SMART ------------------------------------------------------------
    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # --- processes: nginx master + 4 workers + OS daemons ----------------
    # nginx worker VSZ must be > shmem (shared memory zones ~640 MiB)
    nginx_worker_vsz = 1_310_720  # ~1.25 GiB (maps shared zones + mmap'd TLS buffers)
    nginx_worker_rss = int(
        gauge("nginx.worker.rss", 180_000, amp_frac=0.04, phase=1.0, period=1100)
    )
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_000, 12_800, "00:00:28", 1, "/sbin/init"),
        (
            "system.slice/systemd-journald.service",
            "root",
            59_100,
            18_600,
            "00:01:14",
            401,
            "/usr/lib/systemd/systemd-journald",
        ),
        (
            "system.slice/systemd-udevd.service",
            "root",
            25_900,
            7_800,
            "00:00:02",
            437,
            "/usr/lib/systemd/systemd-udevd",
        ),
        (
            "system.slice/systemd-resolved.service",
            "systemd-resolve",
            26_600,
            13_000,
            "00:00:41",
            488,
            "/usr/lib/systemd/systemd-resolved",
        ),
        (
            "system.slice/systemd-timesyncd.service",
            "systemd-timesync",
            91_200,
            7_500,
            "00:00:09",
            502,
            "/usr/lib/systemd/systemd-timesyncd",
        ),
        (
            "system.slice/dbus.service",
            "messagebus",
            10_200,
            5_000,
            "00:00:16",
            514,
            "@dbus-daemon --system --address=systemd:",
        ),
        (
            "system.slice/rsyslog.service",
            "syslog",
            222_400,
            6_600,
            "00:00:36",
            611,
            "/usr/sbin/rsyslogd -n -iNONE",
        ),
        (
            "system.slice/ssh.service",
            "root",
            15_400,
            8_900,
            "00:00:01",
            689,
            "sshd: /usr/sbin/sshd -D [listener]",
        ),
        (
            "system.slice/cron.service",
            "root",
            11_500,
            2_400,
            "00:00:01",
            704,
            "/usr/sbin/cron -f -P",
        ),
        # nginx master (does not map worker shared memory — small VSZ)
        (
            "system.slice/nginx.service",
            "root",
            8_200,
            4_100,
            "00:00:00",
            801,
            "nginx: master process /usr/sbin/nginx -g daemon off;",
        ),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 14-04:18:52 {pid} {cmd}")
    # nginx workers — 4 of them; they map the shared memory zones
    for i, wport in enumerate([802, 803, 804, 805]):
        rss_w = nginx_worker_rss + i * 4_096
        a(
            f"0::/system.slice/nginx.service www-data {nginx_worker_vsz} {rss_w} "
            f"00:0{i}:3{i} 14-04:18:51 {wport} "
            "nginx: worker process"
        )

    # --- systemd units (~30, all green) -----------------------------------
    a("<<<systemd_units>>>")
    units = [
        (
            "nginx.service",
            "active",
            "running",
            "A high performance web server and a reverse proxy server",
        ),
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
        ("certbot.service", "active", "running", "Certbot"),
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

    # --- scheduled job: nightly cert renewal check (certbot) -------------
    a("<<<job>>>")
    a("==> certbot-renew <==")
    a(f"start_time {now - 6 * 3600}")
    a("exit_code 0")
    a("real_time 0:12.3")
    a("user_time 0.10")
    a("system_time 0.04")
    a("max_res_kbytes 38000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# State persistence — counters + uptime survive restarts; graphs keep wobbling
# ---------------------------------------------------------------------------
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-web-frontend-state.json")


def save_state() -> None:
    if not STATE_FILE:
        return
    with _state_lock:
        data = {
            "version": 1,
            "start": START,
            "state": "healthy",
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
    global START, _state_since
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
        _state_since = data.get("state_since", time.time())
        saved = data.get("counters", {})
        restored = 0
        for name, c in _ALL_COUNTERS.items():
            if name in saved:
                c.acc, c.last = saved[name]
                restored += 1
    print(f"[state] restored: {restored}/{len(_ALL_COUNTERS)} counters, uptime continuous")


# ---------------------------------------------------------------------------
# TCP agent server
# ---------------------------------------------------------------------------
class AgentHandler(StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.wfile.write(build_agent_output())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        save_state()


class AgentServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Admin HTTP server — minimal (no toggle; this host has no incident)
# ---------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _admin_page() -> str:
    uptime_s = int(time.time() - START) + UPTIME_OFFSET
    up_str = _fmt_duration(uptime_s)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>{HOSTNAME} — steady green</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:52rem; padding:0 1rem; }}
 h1   {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }}
 h1 b {{ color:#d8dee4; }}
 .badge {{ display:inline-block; padding:.4rem 1.2rem; border-radius:.4rem;
           color:#fff; font-weight:700; font-size:1.6rem; letter-spacing:.05em;
           background:#2e7d32; }}
 .meta  {{ color:#9aa4af; margin:.7rem 0 1.2rem; }}
 .info  {{ background:#22262b; border:1px solid #333; border-radius:.5rem;
           padding:1rem 1.3rem; margin-bottom:1rem; }}
 .info h2 {{ margin:.1rem 0 .6rem; font-size:1rem; color:#9aa4af;
             text-transform:uppercase; letter-spacing:.06em; }}
 .info ul {{ padding-left:1.2rem; margin:.3rem 0; }}
 .info li {{ margin:.25rem 0; font-size:.93rem; }}
 .foot {{ margin-top:2rem; color:#555; font-size:.85rem; }}
</style></head><body>
 <h1>demo host — <b>{HOSTNAME}</b>
  <span style="color:#555">(auto-refreshes every 10 s)</span></h1>
 <div class="badge">HEALTHY</div>
 <div class="meta">Uptime: <b>{up_str}</b> &nbsp;|&nbsp;
  Role: nginx reverse proxy / TLS termination &nbsp;|&nbsp;
  No incident — steady-green background host</div>

 <div class="info">
  <h2>What this host presents to Checkmk</h2>
  <ul>
   <li><b>CPU load</b> ~0.6 (4 cores) — comfortably green, gently wobbling</li>
   <li><b>Memory</b> ~5.5 GiB used of 16 GiB, page cache healthy — all green</li>
   <li><b>Disk</b> single SATA SSD (Samsung 870 EVO), healthy SMART, calm I/O</li>
   <li><b>Network</b> eth0 ~8 MB/s rx / ~6 MB/s tx — busiest host in the estate</li>
   <li><b>TCP</b> ~120 ESTABLISHED (clients + upstream keep-alive) + ~80 TIME_WAIT</li>
   <li><b>nginx.service</b> active/running; nginx master + 4 workers in ps</li>
   <li><b>certbot-renew job</b> exit 0 (last run 6 h ago)</li>
   <li>Time sync, APT, filesystems — all green</li>
  </ul>
 </div>

 <div class="info">
  <h2>Purpose</h2>
  <ul>
   <li>Makes the monitoring estate look like a real company
     (Meridian Retail front door)</li>
   <li>This host has <b>no incident and no toggle</b> — it exists to keep the wall
     of green convincing</li>
   <li>All counters and gauges wobble naturally (no static lines), counters
     survive restarts (state file persists)</li>
  </ul>
 </div>

 <div class="foot">JSON status: <a style="color:#9aa4af" href="/">http://localhost:{HTTP_PORT}/</a></div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "web-frontend-demo/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")

    def _send_json(self, body: dict) -> None:
        raw = json.dumps(body, indent=2).encode()
        self.send_response(200)
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
        path = self.path.partition("?")[0].rstrip("/") or "/"
        if path == "/admin":
            return self._send_html(_admin_page())
        if path == "/admin/meta":
            return self._send_json(
                {
                    "state": "healthy",
                    "in_state_for_s": round(time.time() - START, 1),
                    "action_to_state": {},
                    "states": {
                        "healthy": {
                            "label": "HEALTHY",
                            "color": "#2e7d32",
                            "tagline": "Steady-green background host — no incident and no toggle. "
                            "It exists to keep the wall of green convincing.",
                            "effects": [
                                "CPU ~0.6 load / memory / single SATA SSD all wobble naturally "
                                "within green — no static lines",
                                "Network eth0 ~8 MB/s rx — the busiest host in the estate, "
                                "still comfortably green",
                                "nginx active (master + 4 workers), certbot-renew exit 0, "
                                "time sync / APT / filesystems all green",
                                "No state to change — this host never alerts",
                            ],
                        }
                    },
                }
            )
        return self._send_json(
            {
                "host": HOSTNAME,
                "role": "nginx reverse proxy / TLS termination",
                "state": "healthy",
                "uptime_s": int(time.time() - START) + UPTIME_OFFSET,
            }
        )


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    print(
        f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
        f"state=healthy (steady-green — no incident)"
    )
    print(f"[boot] admin UI:   http://localhost:{HTTP_PORT}/admin")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
