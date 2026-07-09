#!/usr/bin/env python3
"""Meridian Retail demo host: leaf-sw-01 — whitebox top-of-rack access switch.

The switch every Meridian Retail server hangs off: an Edgecore AS5812-54X-class
whitebox (Intel Atom C2538, 8 GB RAM, 16 GB InnoDisk SATA-DOM 3IE3) running
Cumulus Linux 5.9 (Debian 12 based — a Checkmk agent on Cumulus is a real,
documented practice). This is a STEADY-GREEN background host — no incident, no
toggle. In the parent topology it sits between `core-gw-01` (its parent) and
the whole server rack (its children), so RCA has a real network path to
reason over.

Characteristics that sell a ToR switch vs a server:
  - The heart of the host is the interface table: swp1..swp10 are 10G server
    access ports each carrying that server's distinct traffic; swp49/swp50 are
    40G uplinks bonded as bond0 towards core-gw-01; swp11..swp14 are dark.
    Most traffic is east-west inside the rack (app <-> db <-> storage <->
    backup), so the uplink carries far less than the access-port sum.
  - switchd keeps one Atom core mildly busy (stats polling), softirq shows the
    control-plane punt path, iowait is ~0 (hardware forwards, the CPU doesn't).
  - Almost no disk I/O: the 16 GB SATA-DOM only ever writes logs.
  - Cumulus daemon set in systemd/ps: switchd, mstpd, ptmd, ledmgrd, portwd,
    nvued, FRR (watchfrr/zebra/mgmtd/staticd), lldpd — every unit has its
    process.

All counters and gauges gently wobble — no static lines, but nothing ever
crosses an alert threshold.

Config via env:
  CMK_HOSTNAME   reported hostname (default: leaf-sw-01.corp.meridian-retail.com)
  AGENT_PORT     plaintext TCP agent port (default: 6569)
  HTTP_PORT      admin HTTP port (default: 8100)
  STATE_FILE     counter persistence file (default: /var/tmp/cmk-demo-leaf-sw-01.json)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "leaf-sw-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6569"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8100"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")

START = time.time()
# Pretend the switch has been up ~200 days — network gear reboots on upgrades only
UPTIME_OFFSET = 200 * 86400

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
# Counters — a hardware-forwarding switch: the ASIC moves the packets, the CPU
# only sees control plane + stats polling. 4 Atom cores at 100 Hz = 400
# ticks/s total; ~9 % busy overall (≈0.36 cores — consistent with the ~0.3
# loadavg), softirq visible (punt path), iowait ~0 (the DOM is idle).
# ---------------------------------------------------------------------------
C_USER    = Counter("cpu.user",    phase=0.3, start=_aged(14))     # ~3.5 % of 400 ticks/s
C_SYSTEM  = Counter("cpu.system",  phase=1.1, start=_aged(11))     # ~2.8 %
C_IDLE    = Counter("cpu.idle",    phase=2.4, start=_aged(362))    # ~90.5 %
C_IOWAIT  = Counter("cpu.iowait",  phase=3.0, amp=0.25, start=_aged(0.4))   # ~0.1 %
C_IRQ     = Counter("cpu.irq",     phase=3.6, start=_aged(1.5))    # ~0.4 %
C_SOFTIRQ = Counter("cpu.softirq", phase=4.2, start=_aged(9))      # ~2.2 % — CPU punt path

C_CTXT  = Counter("kernel.ctxt",       phase=4.0, start=_aged(3_500))  # switchd poll threads
C_PROC  = Counter("kernel.processes",  phase=4.7, start=_aged(1.2))    # cron + nv CLI forks
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, amp=0.25, start=_aged(0.05))  # near zero

# 16 GB InnoDisk SATA-DOM: reads are all cached after boot, writes are logs
# only — a few per second, sub-ms, utilization well under 1 %.
SDA = {
    "rd_ios":   Counter("sda.rd_ios",   phase=0.0, start=_aged(0.4)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(0.2)),
    "wr_ios":   Counter("sda.wr_ios",   phase=0.4, start=_aged(6)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(2)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(4)),
}


# ---------------------------------------------------------------------------
# Interfaces — the heart of a switch. One NetPort = four monotonic counters
# (rx/tx bytes + packets), each with its own wobble phase so every port's
# graph looks different. Packet counters share the byte counter's phase so
# the derived avg packet size only wobbles mildly (noise differs).
# ---------------------------------------------------------------------------
class NetPort:
    def __init__(self, name: str, phase: float, rx_bps: float, tx_bps: float,
                 rx_pkt_bytes: float, tx_pkt_bytes: float, server: str = "") -> None:
        self.name = name
        self.server = server
        self.rx_bps = rx_bps
        self.tx_bps = tx_bps
        self.rx_pps = rx_bps / rx_pkt_bytes
        self.tx_pps = tx_bps / tx_pkt_bytes
        self.c_rx_b = Counter(f"net.{name}.rx_bytes", phase=phase,        start=_aged(rx_bps))
        self.c_rx_p = Counter(f"net.{name}.rx_pkts",  phase=phase,        start=_aged(self.rx_pps))
        self.c_tx_b = Counter(f"net.{name}.tx_bytes", phase=phase + 1.9,  start=_aged(tx_bps))
        self.c_tx_p = Counter(f"net.{name}.tx_pkts",  phase=phase + 1.9,  start=_aged(self.tx_pps))

    def sample(self) -> tuple[int, int, int, int, int]:
        """(rx_bytes, rx_pkts, rx_mcast, tx_bytes, tx_pkts) — multicast (~0.1 %:
        STP hellos, LLDP) derived monotonically from the packet counter. It is
        computed per member port so bond0's sum matches the bonding driver."""
        rxp = self.c_rx_p.sample(self.rx_pps)
        return (self.c_rx_b.sample(self.rx_bps), rxp, rxp // 900,
                self.c_tx_b.sample(self.tx_bps), self.c_tx_p.sample(self.tx_pps))


# All front-panel ports, the bridge and the bond share the platform base MAC
# (that is genuinely what Cumulus does); only the mgmt NIC differs.
SWITCH_MAC = "cc:37:ab:e4:9a:20"   # Edgecore/Accton OUI
ETH0_MAC = "cc:37:ab:e4:9a:1f"

# swp1..swp10 — 10G server access ports (rates in B/s from the switch's view:
# rx = what the server sends into the switch). Storage/db rack ports are the
# busiest; most of this stays east-west and never reaches the uplink.
ACCESS_PORTS = [
    #        name   phase  rx B/s      tx B/s      rxpkt txpkt  server behind the port
    NetPort("swp1",  0.00,  5_750_000,  8_000_000, 640,  720, "web-frontend-01"),   # ~46/64 Mbit
    NetPort("swp2",  0.73,  2_250_000,  3_250_000, 520,  560, "app-worker-01"),     # ~18/26
    NetPort("swp3",  1.46,  4_250_000,  3_875_000, 410,  430, "app-redis-01"),      # ~34/31, small ops
    NetPort("swp4",  2.19, 16_500_000, 11_000_000, 780,  700, "db-postgres-01"),    # ~132/88
    NetPort("swp5",  2.92, 12_000_000,  7_625_000, 720,  680, "db-postgres-02"),    # ~96/61
    NetPort("swp6",  3.65, 48_125_000, 30_000_000, 1250, 900, "fileserver-01"),     # ~385/240, busiest
    NetPort("swp7",  4.38,  2_750_000, 42_500_000, 620, 1300, "backup-01"),         # ~22/340, backup sink
    NetPort("swp8",  5.11,    687_500,    812_500, 460,  480, "mail-relay-01"),     # ~5.5/6.5
    NetPort("swp9",  5.84,  1_125_000,  1_500_000, 540,  560, "win-dc-01"),         # ~9/12
    NetPort("swp10", 6.57,  7_250_000,  9_000_000, 980,  940, "payment-api"),       # ~58/72
]

DOWN_PORTS = ["swp11", "swp12", "swp13", "swp14"]   # dark — default discovery skips them

# swp49/swp50 — 40G uplinks to core-gw-01, LACP bond0. Aggregate ~160/70
# Mbit/s (north-south only), hash-split ~55/45 with distinct wobbles;
# bond0's counters are the exact sum of the members.
UPLINK_PORTS = [
    NetPort("swp49", 0.41, 11_000_000, 4_812_500, 850, 820),
    NetPort("swp50", 1.87,  9_000_000, 3_937_500, 850, 820),
]

ETH0 = NetPort("eth0", 2.66, 4_000, 15_000, 320, 380)          # 1G mgmt, near-idle
BR_DEFAULT = NetPort("br_default", 3.31, 45_000, 25_000, 350, 360)  # bridge SVI, small own traffic
LO_B = Counter("net.lo.bytes", phase=4.9, start=_aged(2_000))
LO_P = Counter("net.lo.pkts",  phase=4.9, start=_aged(2_000 / 180))


def _dev_line(name: str, s: tuple[int, int, int, int, int]) -> str:
    """One /proc/net/dev line (multicast is rx field 8)."""
    rxb, rxp, mcast, txb, txp = s
    return (f"{name}: {rxb} {rxp} 0 0 0 0 0 {mcast} "
            f"{txb} {txp} 0 0 0 0 0 0")


# ---------------------------------------------------------------------------
# SMART: the 16 GB InnoDisk SATA-DOM. Healthy: raw values zero — discovery
# baseline never exceeded. Temp ~33 °C (switches run warm, but the default
# WARN is 35 °C — 33 ±1.3 never touches it).
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
            {"id": 5,   "name": "Reallocated_Sector_Ct",  "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 12,  "name": "Power_Cycle_Count",      "value": 100, "thresh": 0,
             "raw": {"value": 128}},
            {"id": 173, "name": "Average_Erase_Count",    "value": 97,  "thresh": 0,
             "raw": {"value": 156}},
            {"id": 187, "name": "Reported_Uncorrect",     "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count",   "value": 200, "thresh": 0,
             "raw": {"value": 0}},
        ]},
    }
    return json.dumps(doc, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Filesystems — / on the 16 GB DOM (~33 % used, image-based OS: growth is
# glacial) and a 2 GB /var/log partition (~35–42 %: log creep, logrotate trims
# at midnight). Both stay far under the 80/90 % df defaults.
# ---------------------------------------------------------------------------
def filesystem_usage(now: float) -> tuple[int, int]:
    """Return (root_used_kB, varlog_used_kB)."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: ~4.8 GiB base of 14.65 GiB; a switch image barely grows
    root_base   = 5_042_176
    root_growth = min(262_144, uptime * 0.0015)               # ~130 kB/day
    root_used   = int(root_base + root_growth
                      + gauge("fs.root", 0, amp_abs=8_192, period=1800))

    # /var/log: syslog + switchd.log + frr logs creep, logrotate trims daily
    # at midnight → sawtooth, base ~713 MiB of 2 GiB, peak ~850 MiB (~42 %)
    log_base  = 730_000
    log_daily = 140_000 * ((now % day) / day)                 # 0..137 MiB saw
    log_used  = int(log_base + log_daily
                    + gauge("fs.log", 0, amp_abs=8_192, period=1200))

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
    # Memory: 8 GB switch — switchd holds ~1.2 GB RSS (ASIC tables mirrored in
    # RAM), FRR/python daemons the rest; over half the RAM stays free. No
    # swap (network gear never swaps). CommitLimit = SwapTotal(0) + RAM/2.
    # -----------------------------------------------------------------------
    mem_total    = 8_062_192    # kB (8 GiB minus kernel reservations)
    swap_total   = 0
    commit_limit = mem_total // 2

    cached     = int(gauge("mem.cached",  1_650_000, amp_frac=0.03, phase=0.4, period=1500))
    buffers    = int(gauge("mem.buffers",    96_000, amp_frac=0.04, phase=1.2, period=1100))
    sreclaim   = int(gauge("mem.srec",      182_000, amp_frac=0.03, phase=2.0, period=1300))
    swapcached = 0
    caches     = cached + buffers + swapcached + sreclaim

    # anon: switchd ~1.2 GB + frr + the python daemons (ledmgrd/portwd/nvued)
    shmem  = int(gauge("mem.shmem",    38_912, amp_frac=0.02, phase=0.8, period=1600))
    anon   = int(gauge("mem.anon",  1_580_000, amp_frac=0.03, phase=1.5, period=1400))
    mem_free = max(200_000, mem_total - anon - shmem - caches)

    # LRU split — Active(anon)+Inactive(anon) = AnonPages+Shmem
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = mem_free + file_lru + sreclaim

    a_anon = int(anon_lru * 0.62)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.38)
    i_file = file_lru - a_file

    slab         = sreclaim + 92_160     # SUnreclaim ~90 MiB
    threads      = 230                   # switchd is heavily threaded (~90) + FRR + OS
    kernel_stack = threads * 16          # kB
    dirty        = max(256, int(gauge("mem.dirty", 1_536, amp_frac=0.20,
                                      phase=2.2, period=900)))

    committed = int(gauge("mem.committed", 2_450_000, amp_frac=0.03,
                          phase=0.9, period=1700))

    # -----------------------------------------------------------------------
    # Load: switchd's polling threads keep one Atom core mildly busy —
    # loadavg wobbles around 0.2–0.4 on 4 cores. 15-min per-core defaults
    # WARN at 5.0 → nowhere close.
    # -----------------------------------------------------------------------
    base_l = 0.30
    l1  = round(base_l * gauge("load1",  1.0, amp_frac=0.30, phase=0.2, period=300),  2)
    l5  = round(base_l * 0.95 * gauge("load5",  1.0, amp_frac=0.18, phase=1.1, period=900), 2)
    l15 = round(base_l * 0.90 * gauge("load15", 1.0, amp_frac=0.10, phase=2.1, period=2400), 2)
    l1  = max(0.01, l1)
    l5  = max(0.01, l5)
    l15 = max(0.01, l15)
    runnable    = 1
    total_procs = threads

    # /proc/stat counters
    user    = C_USER.sample(14)
    system  = C_SYSTEM.sample(11)
    idle    = C_IDLE.sample(362)
    iowait  = C_IOWAIT.sample(0.4)
    irq     = C_IRQ.sample(1.5)
    softirq = C_SOFTIRQ.sample(9)

    # Disk I/O — near-idle DOM
    sda_rd  = SDA["rd_ios"].sample(0.4)
    sda_rdt = SDA["rd_ticks"].sample(0.2)
    sda_wr  = SDA["wr_ios"].sample(6)
    sda_wrt = SDA["wr_ticks"].sample(2)
    sda_iot = SDA["io_ticks"].sample(4)

    # Interfaces — sample every port exactly once per poll; bond0 is the
    # exact sum of its members (as the Linux bonding driver reports it).
    access  = {p.name: p.sample() for p in ACCESS_PORTS}
    uplinks = {p.name: p.sample() for p in UPLINK_PORTS}
    bond0   = tuple(sum(vals) for vals in zip(*uplinks.values()))
    eth0    = ETH0.sample()
    br_def  = BR_DEFAULT.sample()
    lo_b    = LO_B.sample(2_000)
    lo_p    = LO_P.sample(2_000 / 180)

    # SMART temperature: DOM sits at ~33 °C — warm chassis, still under the
    # 35 °C default WARN even at the top of the wobble.
    sda_temp  = round(gauge("smart.sda.temp", 33.0, amp_abs=1.3, phase=2.1, period=1100))
    sda_hours = int(uptime / 3600) + 21_400   # ~2.5 years power-on before this boot
    sda_smart = _smart_json("/dev/sda",
                            "SATADOM-SL 3IE3 V2",
                            "BCA11802130120014",
                            sda_hours, sda_temp)

    # Filesystems
    root_used, log_used = filesystem_usage(nowf)
    root_size = 15_366_144   # ~14.65 GiB usable on the "16 GB" DOM
    log_size  = 2_097_152    # 2 GiB

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
    a("OSName: Cumulus Linux")
    a("OSVersion: 5.9.2")
    a("OSPlatform: cumulus")
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
            "uuid": "9d2e51c7-88af-4c31-b6e4-0f7a3c54d219",
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Sat, 20 Dec 2025 14:02:31 +0000",
                "to": cert_to}},
            "remote": "remote_query_disabled"}],
    }, separators=(",", ":")))

    # --- deployed plugins -------------------------------------------------
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"' % AGENT_VERSION)

    # --- filesystems ------------------------------------------------------
    # sda1/sda2 hold ONIE + the Cumulus image slots; only these two are mounted rw
    a("<<<df_v2>>>")
    a(f"/dev/sda3 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/sda4 ext4 {log_size} {log_used} {log_size - log_used} "
      f"{round(log_used / log_size * 100)}% /var/log")
    a("[df_inodes_start]")
    a(f"/dev/sda3 ext4 960992 74312 {960992 - 74312} 8% /")
    a(f"/dev/sda4 ext4 131072 862 {131072 - 862} 1% /var/log")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda3 / ext4 rw,relatime,discard 0 0")
    a("/dev/sda4 /var/log ext4 rw,noatime,discard 0 0")

    # --- /proc/meminfo (full 58-key set) -----------------------------------
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
    a("Mapped:         214016 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     92160 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     14336 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    84912 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         4864 kB")
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
    a("DirectMap4k:    190444 kB")
    a("DirectMap2M:    8198164 kB")
    a("DirectMap1G:    0 kB")

    # --- CPU load ---------------------------------------------------------
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} "
      f"{18000 + C_PROC.sample(1.2) % 9999} {ncpu}")

    # --- uptime -----------------------------------------------------------
    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.62)}.00")

    # --- timesyncd (dynamic timestamps) -----------------------------------
    last_sync = now - 740
    sync_str  = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = int(gauge("ntp.offset", 0, amp_abs=1500, phase=1.3, period=600))
    jitter_ms = round(gauge("ntp.jitter", 1.2, amp_abs=0.5, phase=0.7, period=700), 3)
    jitter_ms = max(0.1, jitter_ms)
    a("<<<timesyncd>>>")
    a("       Server: 10.10.0.4 (ntp1.corp.meridian-retail.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 3")
    a("    Reference: 0A0A0002")
    a("    Precision: 1us (-24)")
    a("Root distance: 2.311ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 0.421ms")
    a(f"       Jitter: {jitter_ms:.3f}ms")
    a(f" Packet count: {8_412 + int((nowf - START) / 2048)}")
    a("    Frequency: +7.104ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=3, Precision=-24, "
      "RootDelay=0.842ms, RootDispersion=0.913ms, Reference=0A0A0002, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=84, Jitter=0.918ms }")
    a("Timezone=UTC")

    # --- apt (Cumulus is Debian-based; image managed, nothing pending) -----
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # --- kernel -----------------------------------------------------------
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} {irq} {softirq} 0 0 0")
    a(f"ctxt {C_CTXT.sample(3_500)}")
    a(f"processes {C_PROC.sample(1.2)}")
    a(f"pgmajfault {C_PGMAJ.sample(0.05)}")

    # --- diskstat ---------------------------------------------------------
    a("<<<diskstat>>>")
    a(str(now))
    a(f"8 0 sda {sda_rd} 0 {sda_rd * 16} {sda_rdt} {sda_wr} 0 "
      f"{sda_wr * 32} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0")

    # --- lnx_if (both variants required — see CLAUDE.md) ------------------
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    idx = 1
    a(f"{idx}: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "mode DEFAULT group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    idx += 1
    a(f"{idx}: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast "
      "state UP mode DEFAULT group default qlen 1000")
    a(f"    link/ether {ETH0_MAC} brd ff:ff:ff:ff:ff:ff")
    for p in ACCESS_PORTS:
        idx += 1
        a(f"{idx}: {p.name}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9216 qdisc pfifo_fast "
          "master br_default state UP mode DEFAULT group default qlen 1000")
        a(f"    link/ether {SWITCH_MAC} brd ff:ff:ff:ff:ff:ff")
    for name in DOWN_PORTS:
        idx += 1
        a(f"{idx}: {name}: <BROADCAST,MULTICAST> mtu 9216 qdisc noop state DOWN "
          "mode DEFAULT group default qlen 1000")
        a(f"    link/ether {SWITCH_MAC} brd ff:ff:ff:ff:ff:ff")
    for p in UPLINK_PORTS:
        idx += 1
        a(f"{idx}: {p.name}: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 9216 "
          "qdisc pfifo_fast master bond0 state UP mode DEFAULT group default qlen 1000")
        a(f"    link/ether {SWITCH_MAC} brd ff:ff:ff:ff:ff:ff")
    idx += 1
    a(f"{idx}: bond0: <BROADCAST,MULTICAST,MASTER,UP,LOWER_UP> mtu 9216 qdisc noqueue "
      "master br_default state UP mode DEFAULT group default qlen 1000")
    a(f"    link/ether {SWITCH_MAC} brd ff:ff:ff:ff:ff:ff")
    idx += 1
    a(f"{idx}: br_default: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9216 qdisc noqueue "
      "state UP mode DEFAULT group default qlen 1000")
    a(f"    link/ether {SWITCH_MAC} brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")

    a("<<<lnx_if:sep(58)>>>")
    a(f"lo: {lo_b} {lo_p} 0 0 0 0 0 0 {lo_b} {lo_p} 0 0 0 0 0 0")
    a(_dev_line("eth0", eth0))
    for p in ACCESS_PORTS:
        a(_dev_line(p.name, access[p.name]))
    for name in DOWN_PORTS:
        a(f"{name}: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")
    for p in UPLINK_PORTS:
        a(_dev_line(p.name, uplinks[p.name]))
    a(_dev_line("bond0", bond0))
    a(_dev_line("br_default", br_def))

    a("[lo]")
    a("\tLink detected: yes")
    a("[eth0]")
    a("\tSpeed: 1000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a(f"Address: {ETH0_MAC}")
    for p in ACCESS_PORTS:
        a(f"[{p.name}]")
        a("\tSpeed: 10000Mb/s")
        a("\tDuplex: Full")
        a("\tAuto-negotiation: off")
        a("\tLink detected: yes")
        a(f"Address: {SWITCH_MAC}")
    for name in DOWN_PORTS:
        a(f"[{name}]")
        a("\tDuplex: Unknown! (255)")
        a("\tAuto-negotiation: off")
        a("\tLink detected: no")
        a(f"Address: {SWITCH_MAC}")
    for p in UPLINK_PORTS:
        a(f"[{p.name}]")
        a("\tSpeed: 40000Mb/s")
        a("\tDuplex: Full")
        a("\tAuto-negotiation: off")
        a("\tLink detected: yes")
        a(f"Address: {SWITCH_MAC}")
    a("[bond0]")
    a("\tSpeed: 80000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: off")
    a("\tLink detected: yes")
    a(f"Address: {SWITCH_MAC}")
    a("[br_default]")
    a("\tLink detected: yes")
    a(f"Address: {SWITCH_MAC}")

    # --- TCP connections: a switch terminates almost nothing itself --------
    # ~10 ESTABLISHED (ssh, NVUE API session, monitoring), LISTEN 12
    estab = round(gauge("tcp.estab", 10, amp_abs=2.5, phase=0.9, period=700))
    tw    = round(gauge("tcp.timewait", 3, amp_abs=2.0, phase=2.4, period=500))
    a("<<<tcp_conn_stats>>>")
    a(f"01 {max(5, estab)}")         # ESTABLISHED
    a(f"02 {random.randint(0, 1)}")  # SYN_SENT
    a(f"06 {max(0, tw)}")            # TIME_WAIT
    a("0A 12")                       # LISTEN

    # --- SMART ------------------------------------------------------------
    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # --- processes: the Cumulus daemon set + Debian base -------------------
    switchd_rss = int(gauge("ps.switchd.rss", 1_228_800, amp_frac=0.015,
                            phase=1.0, period=1400))
    nvued_rss   = int(gauge("ps.nvued.rss", 96_000, amp_frac=0.03,
                            phase=2.6, period=1600))
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_200, 11_400, "01:12:33", 1, "/sbin/init"),
        ("system.slice/systemd-journald.service", "root", 64_500, 22_100,
         "1-02:11:40", 388, "/usr/lib/systemd/systemd-journald"),
        ("system.slice/systemd-udevd.service", "root", 26_400, 7_900,
         "00:00:41", 421, "/usr/lib/systemd/systemd-udevd"),
        ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_200, 7_600,
         "00:12:26", 498, "/usr/lib/systemd/systemd-timesyncd"),
        ("system.slice/dbus.service", "messagebus", 10_800, 5_600,
         "00:41:08", 505, "@dbus-daemon --system --address=systemd:"),
        ("system.slice/systemd-logind.service", "root", 18_500, 8_200,
         "00:03:12", 512, "/usr/lib/systemd/systemd-logind"),
        ("system.slice/rsyslog.service", "root", 224_000, 7_100,
         "03:10:44", 601, "/usr/sbin/rsyslogd -n -iNONE"),
        ("system.slice/irqbalance.service", "root", 32_800, 4_200,
         "02:55:02", 640, "/usr/sbin/irqbalance --foreground"),
        ("system.slice/polkit.service", "root", 236_000, 8_900,
         "00:01:29", 655, "/usr/lib/polkit-1/polkitd --no-debug"),
        ("system.slice/ssh.service", "root", 15_800, 9_100,
         "00:00:52", 688, "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups"),
        ("system.slice/cron.service", "root", 11_400, 2_600,
         "00:04:11", 702, "/usr/sbin/cron -f"),
        ("system.slice/lldpd.service", "root", 12_100, 5_800,
         "04:12:19", 721, "/usr/sbin/lldpd -x -M 4"),
        ("system.slice/lldpd.service", "_lldpd", 12_100, 4_100,
         "09:31:56", 723, "/usr/sbin/lldpd -x -M 4"),
        # switchd mirrors the ASIC tables in RAM — big VSZ, ~1.2 GB RSS, ~90
        # threads; its cumulative CPU time matches the ~20 %-of-one-core story
        ("system.slice/switchd.service", "root", 2_936_012, switchd_rss,
         "45-03:12:33", 833, "/usr/sbin/switchd -vx --daemon"),
        ("system.slice/mstpd.service", "root", 21_500, 6_400,
         "07:41:02", 845, "/usr/sbin/mstpd -d -v2"),
        ("system.slice/ptmd.service", "root", 88_200, 14_800,
         "1-11:08:51", 852, "/usr/sbin/ptmd -d -l INFO"),
        ("system.slice/ledmgrd.service", "root", 233_400, 27_600,
         "06:12:33", 869, "/usr/bin/python3 /usr/sbin/ledmgrd"),
        ("system.slice/portwd.service", "root", 240_100, 29_800,
         "12:41:20", 874, "/usr/bin/python3 /usr/sbin/portwd"),
        ("system.slice/nvued.service", "root", 780_000, nvued_rss,
         "2-04:33:12", 901, "/usr/bin/python3 /usr/bin/nvued"),
        ("system.slice/frr.service", "root", 25_600, 3_900,
         "00:31:02", 950, "/usr/lib/frr/watchfrr -d -F datacenter zebra mgmtd staticd"),
        ("system.slice/frr.service", "frr", 645_000, 58_200,
         "1-01:22:41", 955, "/usr/lib/frr/zebra -d -F datacenter -A 127.0.0.1 -s 90000000"),
        ("system.slice/frr.service", "frr", 480_000, 22_400,
         "00:12:19", 959, "/usr/lib/frr/mgmtd -d -F datacenter"),
        ("system.slice/frr.service", "frr", 350_000, 12_800,
         "00:05:44", 963, "/usr/lib/frr/staticd -d -F datacenter -A 127.0.0.1"),
        ("system.slice/system-serial\\x2dgetty.slice/serial-getty@ttyS0.service",
         "root", 6_200, 1_800, "00:00:00", 980,
         "/sbin/agetty -o -p -- \\u --keep-baud 115200,57600,38400,9600 ttyS0 vt220"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 199-18:22:41 {pid} {cmd}")

    # --- systemd units (~30, all green; every running unit has a process) --
    a("<<<systemd_units>>>")
    units = [
        ("switchd.service",              "active", "running", "Cumulus Linux switching daemon"),
        ("mstpd.service",                "active", "running", "MSTP/RSTP/STP daemon"),
        ("ptmd.service",                 "active", "running", "Prescriptive Topology Manager"),
        ("ledmgrd.service",              "active", "running", "Cumulus Linux LED manager daemon"),
        ("portwd.service",               "active", "running", "Cumulus Linux port watch daemon"),
        ("nvued.service",                "active", "running", "NVIDIA User Experience daemon (NVUE)"),
        ("frr.service",                  "active", "running", "FRRouting"),
        ("lldpd.service",                "active", "running", "LLDP daemon"),
        ("ssh.service",                  "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service",                 "active", "running", "Regular background program processing daemon"),
        ("dbus.service",                 "active", "running", "D-Bus System Message Bus"),
        ("serial-getty@ttyS0.service",   "active", "running", "Serial Getty on ttyS0"),
        ("irqbalance.service",           "active", "running", "irqbalance daemon"),
        ("polkit.service",               "active", "running", "Authorization Manager"),
        ("rsyslog.service",              "active", "running", "System Logging Service"),
        ("systemd-journald.service",     "active", "running", "Journal Service"),
        ("systemd-logind.service",       "active", "running", "User Login Management"),
        ("systemd-timesyncd.service",    "active", "running", "Network Time Synchronization"),
        ("systemd-udevd.service",        "active", "running", "Rule-based Manager for Device Events and Files"),
        ("networking.service",           "active", "exited",  "Network initialization"),
        ("apparmor.service",             "active", "exited",  "Load AppArmor profiles"),
        ("console-setup.service",        "active", "exited",  "Set console font and keymap"),
        ("keyboard-setup.service",       "active", "exited",  "Set the console keyboard layout"),
        ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
        ("e2scrub_reap.service",         "active", "exited",  "Remove Stale Online ext4 Metadata Check Snapshots"),
        ("systemd-modules-load.service", "active", "exited",  "Load Kernel Modules"),
        ("systemd-sysctl.service",       "active", "exited",  "Apply Kernel Variables"),
        ("systemd-udev-trigger.service", "active", "exited",  "Coldplug All udev Devices"),
        ("systemd-journal-flush.service", "active", "exited", "Flush Journal to Persistent Storage"),
        ("systemd-remount-fs.service",   "active", "exited",  "Remount Root and Kernel File Systems"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # --- scheduled job: nightly NVUE config backup (02:15 UTC) -------------
    day = 86_400
    job_start = now - (now % day) + 8_100    # 02:15 UTC today
    if job_start > now:
        job_start -= day                     # not reached yet — yesterday's run
    a("<<<job>>>")
    a("==> nvue-config-backup <==")
    a(f"start_time {job_start}")
    a("exit_code 0")
    a("real_time 0:07.84")
    a("user_time 5.12")
    a("system_time 1.04")
    a("max_res_kbytes 58400")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# State persistence — counters + uptime survive restarts; graphs keep wobbling
# ---------------------------------------------------------------------------
STATE_FILE = os.environ.get("STATE_FILE",
                            "/var/tmp/cmk-demo-leaf-sw-01.json")


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


def _port_rows() -> str:
    rows = []
    for p in ACCESS_PORTS:
        rows.append(f"<li><b>{p.name}</b> → {p.server} "
                    f"(~{p.rx_bps * 8 / 1e6:.0f}/{p.tx_bps * 8 / 1e6:.0f} Mbit/s)</li>")
    return "\n   ".join(rows)


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
  Role: whitebox ToR access switch (Cumulus Linux 5.9) &nbsp;|&nbsp;
  No incident — steady-green background host</div>

 <div class="info">
  <h2>What this host presents to Checkmk</h2>
  <ul>
   <li><b>Interfaces</b> — 10× 10G server access ports, each with distinct traffic;
     swp49+swp50 = 40G LACP uplinks (bond0, ~160/70 Mbit/s) to core-gw-01;
     swp11–swp14 dark; errors/drops 0 everywhere</li>
   <li><b>CPU load</b> ~0.3 (4 Atom cores) — switchd polling + softirq punt path</li>
   <li><b>Memory</b> ~2 GiB used of 8 GiB (switchd holds ~1.2 GB RSS) — green</li>
   <li><b>Disk</b> 16 GB InnoDisk SATA-DOM, near-idle (logs only), healthy SMART
     (~33 °C, below the 35 °C WARN)</li>
   <li><b>Units/processes</b> switchd, mstpd, ptmd, ledmgrd, portwd, nvued,
     FRR (zebra/mgmtd/staticd/watchfrr), lldpd — all running with processes</li>
   <li><b>nvue-config-backup job</b> exit 0 (nightly, 02:15 UTC)</li>
   <li>Time sync, APT, filesystems (/ ~33 %, /var/log ~40 %) — all green</li>
  </ul>
 </div>

 <div class="info">
  <h2>Access ports (switch view: rx from / tx to the server)</h2>
  <ul>
   {_port_rows()}
  </ul>
 </div>

 <div class="info">
  <h2>Purpose</h2>
  <ul>
   <li>The top-of-rack switch every Meridian Retail server hangs off — the
     parent of the whole rack in the Checkmk topology (its own parent is
     core-gw-01)</li>
   <li>This host has <b>no incident and no toggle</b> — it exists to keep the wall
     of green convincing and give RCA a network path to reason over</li>
   <li>All counters and gauges wobble naturally (no static lines), counters
     survive restarts (state file persists)</li>
  </ul>
 </div>

 <div class="foot">JSON status: <a style="color:#9aa4af" href="/">http://localhost:{HTTP_PORT}/</a></div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "leaf-sw-demo/1.0"

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
                    "tagline": "Steady-green background host — the ToR switch the whole "
                               "rack hangs off. No incident and no toggle.",
                    "effects": [
                        "10 up server access ports (10G) with distinct wobbling traffic, "
                        "5–385 Mbit/s; errors/drops 0",
                        "bond0 = swp49+swp50 40G LACP uplink to core-gw-01, "
                        "~160/70 Mbit/s aggregate",
                        "switchd/mstpd/ptmd/ledmgrd/portwd/nvued/FRR/lldpd all "
                        "active with matching processes",
                        "CPU ~0.3 load, 16 GB SATA-DOM near-idle, SMART healthy "
                        "(~33 °C) — nothing ever alerts",
                    ]}},
            })
        return self._send_json({
            "host": HOSTNAME,
            "role": "whitebox ToR access switch (Cumulus Linux 5.9)",
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
