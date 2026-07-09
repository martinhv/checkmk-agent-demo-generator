#!/usr/bin/env python3
"""Meridian Retail demo host: core-gw-01 — datacenter gateway / edge router.

The estate's default gateway: a small 1U network appliance (4-core Intel Atom
C3558, 8 GB RAM, one 240 GB Intel DC SATA SSD) running Ubuntu 24.04, FRR and
keepalived. It routes the whole estate to the ISP and is the VRRP *active*
member of a pair (the standby is not monitored). Parent of leaf-sw-01 in the
Checkmk topology. This is a STEADY-GREEN background host — no incident, no
toggle. It exists so the monitoring estate looks like a real company, not a
pile of test boxes.

Characteristics that sell a router vs a server:
  - Three NICs: eth0 WAN uplink (~120/40 Mbit/s), eth1 downlink trunk to
    leaf-sw-01 (~150 Mbit/s down / 60 up — the WAN flows plus inter-VLAN),
    eth2 near-idle management
  - Visible softirq share in the kernel cpu line (packet forwarding),
    while user CPU stays tiny — loadavg ~0.15–0.3
  - Elevated slab (~180 MB — nf_conntrack tables + nftables sets)
  - Very few TCP sessions of its own (~15 ESTABLISHED — routers forward,
    they don't terminate), disk almost idle
  - frr (watchfrr/zebra/mgmtd/staticd), keepalived (VRRP master + children),
    conntrackd, nftables oneshot — every running unit has its processes

All counters and gauges gently wobble — no static lines, but nothing ever
crosses an alert threshold.

Config via env:
  CMK_HOSTNAME   reported hostname (default: core-gw-01.corp.meridian-retail.com)
  AGENT_PORT     plaintext TCP agent port (default: 6568)
  HTTP_PORT      admin HTTP port (default: 8098)
  STATE_FILE     counter persistence file (default: /var/tmp/cmk-demo-core-gw-01.json)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "core-gw-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6568"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8098"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")

START = time.time()
# Pretend the appliance has been up ~140 days — routers don't get rebooted often
UPTIME_OFFSET = 140 * 86400

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
_ALL_COUNTERS: dict[str, "Counter"] = {}


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


def _aged(rate_per_s: float) -> float:
    """Seed a counter with the value it would have after UPTIME_OFFSET seconds."""
    return rate_per_s * UPTIME_OFFSET


# ---------------------------------------------------------------------------
# Counters — a router: nearly all CPU is softirq (packet forwarding), user
# space is tiny. 4 CPUs at 100 Hz = 400 ticks/s total; ~2 % user / 4 % system
# / 6.5 % softirq / ~87 % idle, iowait ≈ 0 (nothing blocks on the SSD).
# Idle gets a small amp so the total tick rate stays near 400/s.
# ---------------------------------------------------------------------------
C_USER    = Counter("cpu.user",    phase=0.3, start=_aged(8))     # ~2 % of 400 ticks/s
C_SYSTEM  = Counter("cpu.system",  phase=1.1, start=_aged(16))    # ~4 %
C_IDLE    = Counter("cpu.idle",    phase=2.4, amp=0.06, start=_aged(348))  # ~87 %
C_IOWAIT  = Counter("cpu.iowait",  phase=3.0, start=_aged(0.2))   # ≈ 0
C_IRQ     = Counter("cpu.irq",     phase=3.4, start=_aged(1))     # hardirq, tiny
C_SOFTIRQ = Counter("cpu.softirq", phase=3.8, start=_aged(26))    # ~6.5 % — the router tell

C_CTXT   = Counter("kernel.ctxt",       phase=4.0, start=_aged(4_200))  # NAPI polling, modest
C_PROC   = Counter("kernel.processes",  phase=4.7, start=_aged(2))      # routers fork little
C_PGMAJ  = Counter("kernel.pgmajfault", phase=5.4, amp=0.25, start=_aged(0.05))  # near zero

# Single Intel DC SATA SSD: OS + logs only. A router barely touches its disk.
SDA = {
    "rd_ios":   Counter("sda.rd_ios",   phase=0.0, start=_aged(1)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(0.25)),   # ~0.25 ms/read (DC SSD)
    "wr_ios":   Counter("sda.wr_ios",   phase=0.4, start=_aged(8)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(2)),      # ~0.25 ms/write
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(4)),  # ~0.4 % util
}

# Network. eth0 = WAN uplink (~120 Mbit/s in / 40 out), eth1 = downlink trunk
# to leaf-sw-01 (the same flows mirrored + inter-VLAN routing: ~150 down / 60
# up), eth2 = management (near idle), lo = local daemons.
# eth1's tx wobble shares eth0's rx phase (and vice versa) so the forwarded
# traffic stays >= the WAN traffic at every instant — packets never vanish.
E0_RX_B = Counter("eth0.rx_bytes", phase=1.6, start=_aged(15_000_000))   # 120 Mbit/s
E0_TX_B = Counter("eth0.tx_bytes", phase=2.3, start=_aged(5_000_000))    # 40 Mbit/s
E0_RX_P = Counter("eth0.rx_pkts",  phase=1.6, start=_aged(14_000))
E0_TX_P = Counter("eth0.tx_pkts",  phase=2.3, start=_aged(9_000))
E0_RX_M = Counter("eth0.rx_mcast", phase=5.0, amp=0.2, start=_aged(0.2))

E1_RX_B = Counter("eth1.rx_bytes", phase=2.3, start=_aged(7_500_000))    # 60 Mbit/s up
E1_TX_B = Counter("eth1.tx_bytes", phase=1.6, start=_aged(18_750_000))   # 150 Mbit/s down
E1_RX_P = Counter("eth1.rx_pkts",  phase=2.3, start=_aged(10_500))
E1_TX_P = Counter("eth1.tx_pkts",  phase=1.6, start=_aged(16_500))
E1_RX_M = Counter("eth1.rx_mcast", phase=5.3, amp=0.2, start=_aged(0.5))  # STP/LLDP from the leaf

E2_RX_B = Counter("eth2.rx_bytes", phase=4.1, start=_aged(2_500))        # ~20 kbit/s mgmt
E2_TX_B = Counter("eth2.tx_bytes", phase=4.6, start=_aged(1_800))
E2_RX_P = Counter("eth2.rx_pkts",  phase=4.1, start=_aged(9))
E2_TX_P = Counter("eth2.tx_pkts",  phase=4.6, start=_aged(7))
E2_RX_M = Counter("eth2.rx_mcast", phase=5.6, amp=0.2, start=_aged(0.3))

LO_B = Counter("lo.bytes", phase=0.9, start=_aged(3_000))                # frr vtys, resolver
LO_P = Counter("lo.pkts",  phase=0.9, start=_aged(12))


# ---------------------------------------------------------------------------
# SMART: one healthy Intel DC SATA SSD. Raw values zero — discovery baseline
# never exceeded. Intel D3-S4510 attribute set (incl. wear indicators).
# ---------------------------------------------------------------------------
def _smart_json(name: str, model: str, serial: str, hours: int, temp: int) -> str:
    doc = {
        "device": {"name": name, "type": "sat", "protocol": "ATA"},
        "model_name": model,
        "serial_number": serial,
        "smart_status": {"passed": True},
        "power_on_time": {"hours": hours},
        "temperature": {"current": temp},
        "ata_smart_attributes": {"table": [
            {"id": 5,   "name": "Reallocated_Sector_Ct",   "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 12,  "name": "Power_Cycle_Count",       "value": 100, "thresh": 0,
             "raw": {"value": 18}},
            {"id": 184, "name": "End-to-End_Error",        "value": 100, "thresh": 90,
             "raw": {"value": 0}},
            {"id": 187, "name": "Reported_Uncorrect",      "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector",  "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count",    "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 232, "name": "Available_Reservd_Space", "value": 99,  "thresh": 10,
             "raw": {"value": 0}},
            {"id": 233, "name": "Media_Wearout_Indicator", "value": 97,  "thresh": 0,
             "raw": {"value": 0}},
        ]},
    }
    return json.dumps(doc, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Filesystem — root (220 GiB usable of the 240 GB SSD, ~14 GiB used) and a
# separate 8 GiB /var/log partition (~35 % used). Both are pure functions of
# wall-clock time: slow secular creep + a daily logrotate sawtooth (livelier
# on /var/log — that's where the firewall/FRR logs land). Way below 80 % WARN.
# ---------------------------------------------------------------------------
def filesystem_usage(now: float) -> tuple[int, int]:
    """Return (root_used_kB, varlog_used_kB)."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: ~14 GiB base, glacial package/journal creep (capped at +2 GiB),
    # small daily apt/tmp sawtooth → sits at ~6 % of 220 GiB forever
    root_base   = 14_680_064                                  # 14 GiB
    root_growth = min(2_097_152, uptime * 0.008)              # ~0.7 MB/day creep
    root_daily  = 131_072 * ((now % day) / day)               # 0..128 MiB saw
    root_used   = int(root_base + root_growth + root_daily
                      + gauge("fs.root", 0, amp_abs=32_768, period=1800))

    # /var/log: nftables/FRR/keepalived logs fill up, logrotate trims daily
    # → 24-h sawtooth between ~29 % and ~38 % of the 8 GiB partition
    log_base  = 2_411_724                                     # ~2.3 GiB base
    log_daily = 838_860 * ((now % day) / day)                 # 0..0.8 GiB saw
    log_used  = int(log_base + log_daily
                    + gauge("fs.log", 0, amp_abs=24_576, period=1200))

    return root_used, log_used


# ---------------------------------------------------------------------------
# Agent output — the whole section set
# ---------------------------------------------------------------------------
def build_agent_output() -> bytes:  # noqa: PLR0912, PLR0915
    now  = int(time.time())
    nowf = time.time()
    uptime = int(nowf - START) + UPTIME_OFFSET
    ncpu = 4

    # -----------------------------------------------------------------------
    # Memory: 8 GB appliance, mostly free + a modest page cache (logs and
    # binaries — a router doesn't cache much). The tell is the elevated slab:
    # ~180 MB combined (nf_conntrack tables live in SUnreclaim ~110 MB,
    # dentries/inodes in SReclaimable ~70 MB). No swap on the appliance.
    # CommitLimit = SwapTotal(0) + RAM/2 = 4 GiB.
    # -----------------------------------------------------------------------
    mem_total   = 8_192_000     # kB
    swap_total  = 0             # network appliance — no swap
    commit_limit = mem_total // 2   # kernel default with no swap

    cached    = int(gauge("mem.cached",  1_150_000, amp_frac=0.03, phase=0.4, period=1500))
    buffers   = int(gauge("mem.buffers", 118_000,   amp_frac=0.04, phase=1.2, period=1100))
    sreclaim  = int(gauge("mem.srec",    71_680,    amp_frac=0.03, phase=2.0, period=1300))
    swapcached = 0
    caches    = cached + buffers + swapcached + sreclaim

    # anon usage: frr + keepalived + conntrackd + the usual daemons
    shmem  = int(gauge("mem.shmem", 24_576,  amp_frac=0.02, phase=0.8, period=1600))
    anon   = int(gauge("mem.anon",  460_000, amp_frac=0.03, phase=1.5, period=1400))
    mem_free = max(200_000, mem_total - anon - shmem - caches)

    # LRU split — Active(anon)+Inactive(anon) = AnonPages+Shmem
    anon_lru  = anon + shmem
    file_lru  = max(0, buffers + cached - shmem)
    mem_available = mem_free + file_lru + sreclaim

    a_anon = int(anon_lru * 0.62)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.38)
    i_file = file_lru - a_file

    sunreclaim   = 112_640               # nf_conntrack + nftables sets ~110 MiB
    slab         = sreclaim + sunreclaim  # ~180 MiB combined — router slab tell
    threads      = 185                   # frr/keepalived/daemons + kernel threads
    kernel_stack = threads * 16          # kB
    dirty        = max(256, int(gauge("mem.dirty", 1_536, amp_frac=0.20,
                                      phase=2.2, period=900)))

    # Committed_AS: a router overcommits almost nothing; stays < 45 % of
    # CommitLimit (no WARN at 100 %)
    committed = int(gauge("mem.committed", 1_650_000, amp_frac=0.03,
                          phase=0.9, period=1700))

    # -----------------------------------------------------------------------
    # Load: tiny (~0.22 wobbling 0.15–0.3 on 4 cores). Packet forwarding is
    # softirq work, it barely shows in the loadavg. Default levels: 15-min
    # per-core WARN at 5.0 → we'd need l15 > 20 to alert; we're at ~0.2.
    # -----------------------------------------------------------------------
    base_l = 0.22
    l1  = round(base_l * gauge("load1",  1.0, amp_frac=0.30, phase=0.2, period=300),  2)
    l5  = round(base_l * 0.95 * gauge("load5",  1.0, amp_frac=0.18, phase=1.1, period=900), 2)
    l15 = round(base_l * 0.90 * gauge("load15", 1.0, amp_frac=0.10, phase=2.1, period=2400), 2)
    # Clamp: load must be positive
    l1  = max(0.01, l1)
    l5  = max(0.01, l5)
    l15 = max(0.01, l15)
    runnable    = 1
    total_procs = threads   # /proc/loadavg total = all tasks; matches KernelStack

    # /proc/stat counters
    user    = C_USER.sample(8)
    system  = C_SYSTEM.sample(16)
    idle    = C_IDLE.sample(348)
    iowait  = C_IOWAIT.sample(0.2)
    irq     = C_IRQ.sample(1)
    softirq = C_SOFTIRQ.sample(26)

    # Disk I/O — near idle: logs and conntrackd state flushes
    sda_rd  = SDA["rd_ios"].sample(1)
    sda_rdt = SDA["rd_ticks"].sample(0.25)
    sda_wr  = SDA["wr_ios"].sample(8)
    sda_wrt = SDA["wr_ticks"].sample(2)
    sda_iot = SDA["io_ticks"].sample(4)

    # Network — the WAN flows and their forwarded mirror on the trunk
    e0_rx_b = E0_RX_B.sample(15_000_000)
    e0_tx_b = E0_TX_B.sample(5_000_000)
    e0_rx_p = E0_RX_P.sample(14_000)
    e0_tx_p = E0_TX_P.sample(9_000)
    e0_rx_m = E0_RX_M.sample(0.2)

    e1_rx_b = E1_RX_B.sample(7_500_000)
    e1_tx_b = E1_TX_B.sample(18_750_000)
    e1_rx_p = E1_RX_P.sample(10_500)
    e1_tx_p = E1_TX_P.sample(16_500)
    e1_rx_m = E1_RX_M.sample(0.5)

    e2_rx_b = E2_RX_B.sample(2_500)
    e2_tx_b = E2_TX_B.sample(1_800)
    e2_rx_p = E2_RX_P.sample(9)
    e2_tx_p = E2_TX_P.sample(7)
    e2_rx_m = E2_RX_M.sample(0.3)

    lo_b = LO_B.sample(3_000)
    lo_p = LO_P.sample(12)

    # SMART temperature: healthy DC SSD in a well-cooled rack, 32 ±1.3 °C
    # (wandering, never near the 35 °C WARN)
    sda_temp  = round(gauge("smart.sda.temp", 32.0, amp_abs=1.3, phase=2.1, period=1100))
    sda_hours = int(uptime / 3600) + 26_100   # the appliance is a few years old
    sda_smart = _smart_json("/dev/sda",
                            "INTEL SSDSC2KB240G8",
                            "PHYF018203GB240AGN",
                            sda_hours, sda_temp)

    # Filesystems
    root_used, log_used = filesystem_usage(nowf)
    root_size = 230_686_720   # 220 GiB in kB (usable of the 240 GB SSD)
    log_size  = 8_388_608     # 8 GiB in kB

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
            "uuid": "9d2f6c41-7a05-4e39-bc48-1f83a2e9d574",
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Tue, 03 Jun 2025 09:12:44 +0000",
                "to": cert_to}},
            "remote": "remote_query_disabled"}],
    }, separators=(",", ":")))

    # --- deployed plugins -------------------------------------------------
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"' % AGENT_VERSION)

    # --- filesystems ------------------------------------------------------
    a("<<<df_v2>>>")
    a(f"/dev/sda2 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/sda3 ext4 {log_size} {log_used} {log_size - log_used} "
      f"{round(log_used / log_size * 100)}% /var/log")
    a("[df_inodes_start]")
    a(f"/dev/sda2 ext4 14417920 84213 {14417920 - 84213} 1% /")
    a(f"/dev/sda3 ext4 524288 4120 {524288 - 4120} 1% /var/log")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda2 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sda3 /var/log ext4 rw,noatime 0 0")

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
    a("Mapped:         96256 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a(f"SUnreclaim:     {sunreclaim} kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     6144 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    28672 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         4096 kB")
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
    a("DirectMap4k:    189248 kB")
    a("DirectMap2M:    4005888 kB")
    a("DirectMap1G:    4194304 kB")

    # --- CPU load ---------------------------------------------------------
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} "
      f"{12000 + C_PROC.sample(2) % 9999} {ncpu}")

    # --- uptime -----------------------------------------------------------
    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.5)}.00")

    # --- timesyncd (dynamic timestamps) -----------------------------------
    last_sync = now - 580
    sync_str  = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
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
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
      "RootDelay=8.912ms, RootDispersion=1.047ms, Reference=B97D5A39, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=61, Jitter=1.284ms }")
    a("Timezone=UTC")

    # --- apt --------------------------------------------------------------
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # --- kernel -----------------------------------------------------------
    # cpu line order: user nice system idle iowait irq softirq steal guest gnice
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} {irq} {softirq} 0 0 0")
    a(f"ctxt {C_CTXT.sample(4_200)}")
    a(f"processes {C_PROC.sample(2)}")
    a(f"pgmajfault {C_PGMAJ.sample(0.05)}")

    # --- diskstat ---------------------------------------------------------
    a("<<<diskstat>>>")
    a(str(now))
    # fields: maj min name rdios rdmerges rdsects rdticks wrios wrmerges wrsects wrticks
    #         cur_ios ioticks timeinqueue discards dsectors dsticks flushios flushticks
    a(f"8 0 sda {sda_rd} 0 {sda_rd * 16} {sda_rdt} {sda_wr} 0 "
      f"{sda_wr * 32} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0")

    # --- lnx_if (both variants required — see CLAUDE.md) ------------------
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq "
      "state UP group default qlen 1000")
    a("    link/ether ac:1f:6b:8e:44:d0 brd ff:ff:ff:ff:ff:ff")
    a("3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq "
      "state UP group default qlen 1000")
    a("    link/ether ac:1f:6b:8e:44:d1 brd ff:ff:ff:ff:ff:ff")
    a("4: eth2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq "
      "state UP group default qlen 1000")
    a("    link/ether ac:1f:6b:8e:44:d2 brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"lo: {lo_b} {lo_p} 0 0 0 0 0 0 {lo_b} {lo_p} 0 0 0 0 0 0")
    a(f"eth0: {e0_rx_b} {e0_rx_p} 0 0 0 0 0 {e0_rx_m} "
      f"{e0_tx_b} {e0_tx_p} 0 0 0 0 0 0")
    a(f"eth1: {e1_rx_b} {e1_rx_p} 0 0 0 0 0 {e1_rx_m} "
      f"{e1_tx_b} {e1_tx_p} 0 0 0 0 0 0")
    a(f"eth2: {e2_rx_b} {e2_rx_p} 0 0 0 0 0 {e2_rx_m} "
      f"{e2_tx_b} {e2_tx_p} 0 0 0 0 0 0")
    a("[lo]")
    a("\tLink detected: yes")
    a("Address: 00:00:00:00:00:00")
    a("[eth0]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: ac:1f:6b:8e:44:d0")
    a("[eth1]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: ac:1f:6b:8e:44:d1")
    a("[eth2]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: ac:1f:6b:8e:44:d2")

    # --- TCP connections: a router forwards, it doesn't terminate ---------
    # ~15 ESTABLISHED (sshd sessions, agent controller, conntrackd sync,
    # FRR vtys), a couple of TIME_WAITs, LISTEN 9 (sshd, agent, zebra/mgmtd/
    # staticd vtys, conntrackd)
    estab = round(gauge("tcp.estab", 15, amp_abs=3, phase=0.9, period=700))
    tw    = round(gauge("tcp.timewait", 3, amp_abs=2, phase=2.4, period=500))
    a("<<<tcp_conn_stats>>>")
    a(f"01 {max(8, estab)}")         # ESTABLISHED
    a(f"02 {random.randint(0, 1)}")  # SYN_SENT
    a(f"06 {max(0, tw)}")            # TIME_WAIT
    a("0A 9")                         # LISTEN

    # --- SMART ------------------------------------------------------------
    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # --- processes: FRR + keepalived + conntrackd + OS daemons ------------
    # Every running systemd unit below has its process(es) here; the oneshots
    # (nftables, apparmor, ...) correctly have none.
    zebra_rss = int(gauge("frr.zebra.rss", 29_800, amp_frac=0.03, phase=1.0, period=1100))
    ctd_rss   = int(gauge("conntrackd.rss", 47_500, amp_frac=0.05, phase=1.9, period=900))
    elapsed   = "139-21:12:44"   # matches the ~140-day uptime
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_000, 12_100, "00:04:11", 1, "/sbin/init"),
        ("system.slice/systemd-journald.service", "root", 42_500, 15_900,
         "01:44:09", 412, "/usr/lib/systemd/systemd-journald"),
        ("system.slice/systemd-udevd.service", "root", 25_900, 6_900,
         "00:00:19", 448, "/usr/lib/systemd/systemd-udevd"),
        ("system.slice/systemd-networkd.service", "systemd-network", 21_600, 8_400,
         "00:31:40", 471, "/usr/lib/systemd/systemd-networkd"),
        ("system.slice/systemd-resolved.service", "systemd-resolve", 26_600, 12_400,
         "00:12:23", 489, "/usr/lib/systemd/systemd-resolved"),
        ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_200, 7_100,
         "00:03:57", 501, "/usr/lib/systemd/systemd-timesyncd"),
        ("system.slice/dbus.service", "messagebus", 10_200, 4_700,
         "00:02:44", 517, "@dbus-daemon --system --address=systemd:"),
        ("system.slice/systemd-logind.service", "root", 15_200, 7_300,
         "00:01:31", 523, "/usr/lib/systemd/systemd-logind"),
        ("system.slice/irqbalance.service", "root", 33_400, 4_300,
         "00:26:05", 531, "/usr/sbin/irqbalance --foreground"),
        ("system.slice/polkit.service", "root", 383_000, 8_900,
         "00:00:12", 544, "/usr/lib/polkit-1/polkitd --no-debug"),
        ("system.slice/rsyslog.service", "syslog", 222_400, 6_100,
         "00:19:52", 587, "/usr/sbin/rsyslogd -n -iNONE"),
        ("system.slice/networkd-dispatcher.service", "root", 33_100, 20_400,
         "00:00:41", 592, "/usr/bin/python3 /usr/bin/networkd-dispatcher --run-startup-triggers"),
        ("system.slice/smartmontools.service", "root", 13_000, 6_200,
         "00:05:18", 603, "/usr/sbin/smartd -n"),
        ("system.slice/unattended-upgrades.service", "root", 109_000, 21_800,
         "00:00:09", 615, "/usr/bin/python3 /usr/share/unattended-upgrades/"
         "unattended-upgrade-shutdown --wait-for-signal"),
        ("system.slice/ssh.service", "root", 15_400, 8_600,
         "00:00:44", 671, "sshd: /usr/sbin/sshd -D [listener]"),
        ("system.slice/cron.service", "root", 11_500, 2_300,
         "00:00:58", 688, "/usr/sbin/cron -f -P"),
        # FRR suite — watchfrr supervises zebra/mgmtd/staticd
        ("system.slice/frr.service", "root", 335_000, 4_100,
         "00:14:36", 712, "/usr/lib/frr/watchfrr -d -F traditional zebra mgmtd staticd"),
        ("system.slice/frr.service", "frr", 616_640, zebra_rss,
         "02:58:21", 724, "/usr/lib/frr/zebra -d -F traditional -A 127.0.0.1 -s 90000000"),
        ("system.slice/frr.service", "frr", 484_000, 15_200,
         "00:22:10", 731, "/usr/lib/frr/mgmtd -d -F traditional"),
        ("system.slice/frr.service", "frr", 402_000, 9_400,
         "00:08:47", 738, "/usr/lib/frr/staticd -d -F traditional -A 127.0.0.1"),
        # keepalived — parent + VRRP child + healthcheck child (VRRP MASTER)
        ("system.slice/keepalived.service", "root", 88_000, 3_900,
         "00:06:02", 751, "/usr/sbin/keepalived --dont-fork"),
        ("system.slice/keepalived.service", "root", 90_100, 5_800,
         "03:41:55", 752, "/usr/sbin/keepalived --dont-fork"),
        ("system.slice/keepalived.service", "root", 90_100, 5_200,
         "01:12:38", 753, "/usr/sbin/keepalived --dont-fork"),
        # conntrackd — state sync to the (unmonitored) standby gateway
        ("system.slice/conntrackd.service", "root", 340_000, ctd_rss,
         "05:27:31", 764, "/usr/sbin/conntrackd -d -C /etc/conntrackd/conntrackd.conf"),
        ("system.slice/getty@tty1.service", "root", 6_200, 1_700,
         "00:00:00", 802, "/sbin/agetty -o -p -- \\u --noclear tty1 linux"),
        ("user.slice/user-1000.slice/user@1000.service", "netadmin", 20_500, 11_200,
         "00:00:03", 1210, "/usr/lib/systemd/systemd --user"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} {elapsed} {pid} {cmd}")

    # --- systemd units (~30, all green; oneshots have no processes) -------
    a("<<<systemd_units>>>")
    units = [
        ("frr.service",                      "active", "running", "FRRouting"),
        ("keepalived.service",               "active", "running", "Keepalive Daemon (LVS and VRRP)"),
        ("conntrackd.service",               "active", "running", "Conntrack Daemon"),
        ("smartmontools.service",            "active", "running", "Self Monitoring and Reporting Technology (SMART) Daemon"),
        ("ssh.service",                      "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service",                     "active", "running", "Regular background program processing daemon"),
        ("dbus.service",                     "active", "running", "D-Bus System Message Bus"),
        ("getty@tty1.service",               "active", "running", "Getty on tty1"),
        ("irqbalance.service",               "active", "running", "irqbalance daemon"),
        ("networkd-dispatcher.service",      "active", "running", "Dispatcher daemon for systemd-networkd"),
        ("polkit.service",                   "active", "running", "Authorization Manager"),
        ("rsyslog.service",                  "active", "running", "System Logging Service"),
        ("systemd-journald.service",         "active", "running", "Journal Service"),
        ("systemd-logind.service",           "active", "running", "User Login Management"),
        ("systemd-networkd.service",         "active", "running", "Network Configuration"),
        ("systemd-resolved.service",         "active", "running", "Network Name Resolution"),
        ("systemd-timesyncd.service",        "active", "running", "Network Time Synchronization"),
        ("systemd-udevd.service",            "active", "running", "Rule-based Manager for Device Events and Files"),
        ("unattended-upgrades.service",      "active", "running", "Unattended Upgrades Shutdown"),
        ("user@1000.service",                "active", "running", "User Manager for UID 1000"),
        ("nftables.service",                 "active", "exited",  "netfilter persistent configuration"),
        ("apparmor.service",                 "active", "exited",  "Load AppArmor profiles"),
        ("blk-availability.service",         "active", "exited",  "Availability of block devices"),
        ("console-setup.service",            "active", "exited",  "Set console font and keymap"),
        ("e2scrub_reap.service",             "active", "exited",  "Remove Stale Online ext4 Metadata Check Snapshots"),
        ("finalrd.service",                  "active", "exited",  "Create final runtime dir for shutdown pivot root"),
        ("keyboard-setup.service",           "active", "exited",  "Set the console keyboard layout"),
        ("lvm2-monitor.service",             "active", "exited",  "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or progress polling"),
        ("setvtrgb.service",                 "active", "exited",  "Set console scheme"),
        ("systemd-user-sessions.service",    "active", "exited",  "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # --- scheduled job: nightly nftables ruleset backup (01:45 UTC) -------
    day = 86_400
    since_run = int((now % day - int(1.75 * 3600)) % day)
    a("<<<job>>>")
    a("==> nft-ruleset-backup <==")
    a(f"start_time {now - since_run}")
    a("exit_code 0")
    a("real_time 0:04.7")
    a("user_time 0.31")
    a("system_time 0.09")
    a("max_res_kbytes 12000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# State persistence — counters + uptime survive restarts; graphs keep wobbling
# ---------------------------------------------------------------------------
STATE_FILE = os.environ.get("STATE_FILE",
                            "/var/tmp/cmk-demo-core-gw-01.json")


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
  Role: datacenter gateway / edge router (FRR, VRRP active) &nbsp;|&nbsp;
  No incident — steady-green background host</div>

 <div class="info">
  <h2>What this host presents to Checkmk</h2>
  <ul>
   <li><b>Interfaces</b> eth0 WAN ~120/40 Mbit/s, eth1 trunk to leaf-sw-01
     ~150/60 Mbit/s, eth2 mgmt near-idle — all 1 Gbit FD, zero errors</li>
   <li><b>CPU load</b> ~0.2 (4-core Atom C3558) with a visible softirq share
     — a router's CPU lives in the kernel, not user space</li>
   <li><b>Memory</b> ~2 GiB used of 8 GiB; slab ~180 MB (conntrack tables)</li>
   <li><b>Disk</b> Intel DC SSD (SSDSC2KB240G8), healthy SMART, near-idle I/O</li>
   <li><b>TCP</b> ~15 ESTABLISHED — routers forward, they don't terminate</li>
   <li><b>frr / keepalived / conntrackd</b> active with matching processes;
     nftables oneshot green</li>
   <li><b>nft-ruleset-backup job</b> exit 0 (nightly, 01:45 UTC)</li>
   <li>Time sync, APT, filesystems (/ ~6 %, /var/log ~35 %) — all green</li>
  </ul>
 </div>

 <div class="info">
  <h2>Purpose</h2>
  <ul>
   <li>The estate's gateway to the ISP — parent of leaf-sw-01 in the
     Checkmk topology (Meridian Retail)</li>
   <li>VRRP active member of a pair; the standby is not monitored</li>
   <li>This host has <b>no incident and no toggle</b> — it exists to keep the wall
     of green convincing</li>
   <li>All counters and gauges wobble naturally (no static lines), counters
     survive restarts (state file persists)</li>
  </ul>
 </div>

 <div class="foot">JSON status: <a style="color:#9aa4af" href="/">http://localhost:{HTTP_PORT}/</a></div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "core-gw-demo/1.0"

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
            return self._send_json({
                "state": "healthy",
                "in_state_for_s": round(time.time() - START, 1),
                "action_to_state": {},
                "states": {"healthy": {
                    "label": "HEALTHY", "color": "#2e7d32",
                    "tagline": "Steady-green background host — no incident and no toggle. "
                               "The estate's gateway; parent of leaf-sw-01.",
                    "effects": [
                        "eth0 WAN ~120/40 Mbit/s, eth1 trunk ~150/60 Mbit/s, "
                        "eth2 mgmt near-idle — all wobbling, zero errors",
                        "CPU ~0.2 load with a visible softirq share (packet "
                        "forwarding); slab ~180 MB conntrack tables",
                        "frr/keepalived (VRRP active)/conntrackd running, "
                        "nftables oneshot green, nft-ruleset-backup exit 0",
                        "No state to change — this host never alerts",
                    ]}},
            })
        return self._send_json({
            "host": HOSTNAME,
            "role": "datacenter gateway / edge router (FRR, VRRP active)",
            "state": "healthy",
            "uptime_s": int(time.time() - START) + UPTIME_OFFSET,
        })


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http  = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    print(f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
          f"state=healthy (steady-green — no incident)")
    print(f"[boot] admin UI:   http://localhost:{HTTP_PORT}/admin")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
