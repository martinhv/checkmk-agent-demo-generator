#!/usr/bin/env python3
"""Meridian Retail demo host: backup-01 — steady-green restic backup host.

Pulls nightly restic backups of the DB + fileserver to an offsite repository.
Low sustained load; CPU spikes during the nightly backup window (01:00–03:00
UTC) but always finishes successfully. No incident, no toggle. All green,
always.

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env:
  CMK_HOSTNAME        default backup-01
  AGENT_PORT          default 6566 (container internal; published on 6566)
  HTTP_PORT           default 8080 (container internal; published on 8096)
  AGENT_VERSION       default 2.5.0-2026.04.03
  STATE_FILE          default /var/tmp/cmk-demo-backup-state.json
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "backup-01")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6566"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-backup-state.json")

START = time.time()
UPTIME_OFFSET = 12 * 86400  # pretend the host has been up ~12 days

# How many seconds ago the last nightly backup completed (6-8 h ago, varies
# slightly per restart but stays in range once state is loaded).
_LAST_BACKUP_AGE_S: float = 6.5 * 3600  # default; overridden from state file


# --------------------------------------------------------------------------- #
#  Autocorrelated gauges + monotonic counters
#  Verbatim machinery from the dying-disk / app-worker reference.
#  See CLAUDE.md for why a single sine is wrong.
# --------------------------------------------------------------------------- #
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
    return rate_per_s * UPTIME_OFFSET


# /proc/stat jiffies at 100 Hz, 4 CPUs.
# Backup host is mostly idle; small I/O bursts during nightly window.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(20))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(8))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(360))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(4))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(800))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(2))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(0.1))

# Two drives:
#   sda — 500 GiB NVMe system disk (OS + small working set)
#   sdb — 4 TiB SATA SSD backup-storage volume (/srv/backup), moderate I/O
SDA = {
    "rd_ios":  Counter("sda.rd_ios",  phase=0.0, start=_aged(3)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(2)),
    "wr_ios":  Counter("sda.wr_ios",  phase=0.4, start=_aged(8)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(5)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(6)),
}
# sdb carries nightly backup I/O; average across the day is still modest
SDB = {
    "rd_ios":  Counter("sdb.rd_ios",  phase=1.0, start=_aged(12)),
    "rd_ticks": Counter("sdb.rd_ticks", phase=1.2, start=_aged(8)),
    "wr_ios":  Counter("sdb.wr_ios",  phase=1.4, start=_aged(35)),
    "wr_ticks": Counter("sdb.wr_ticks", phase=1.6, start=_aged(28)),
    "io_ticks": Counter("sdb.io_ticks", phase=1.8, amp=0.05, start=_aged(32)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(90_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(70_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(220))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(180))


# --------------------------------------------------------------------------- #
#  SMART helpers — both drives stay green; raw attrs = 0 so discovery baseline
#  is never exceeded.
# --------------------------------------------------------------------------- #
def _smart_json(name: str, model: str, serial: str, hours: int, temp: int) -> str:
    doc = {
        "device": {"name": name, "type": "sat", "protocol": "ATA"},
        "model_name": model,
        "serial_number": serial,
        "smart_status": {"passed": True},
        "power_on_time": {"hours": hours},
        "temperature": {"current": temp},
        "ata_smart_attributes": {"table": [
            {"id": 5,   "name": "Reallocated_Sector_Ct",    "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 9,   "name": "Power_On_Hours",           "value": 96,  "thresh": 0,
             "raw": {"value": hours}},
            {"id": 12,  "name": "Power_Cycle_Count",        "value": 100, "thresh": 0,
             "raw": {"value": 18}},
            {"id": 177, "name": "Wear_Leveling_Count",      "value": 94,  "thresh": 5,
             "raw": {"value": 92}},
            {"id": 179, "name": "Used_Rsvd_Blk_Cnt_Tot",   "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 187, "name": "Reported_Uncorrect",       "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector",   "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count",     "value": 200, "thresh": 0,
             "raw": {"value": 0}},
        ]},
    }
    return json.dumps(doc, separators=(",", ":"))


# --------------------------------------------------------------------------- #
#  Filesystem helpers
#  /         root   ~10 GiB of 40 GiB — slow log creep, daily logrotate trim
#  /srv/backup      ~70 % used (per spec) — slow growth + daily restic GC sawtooth
# --------------------------------------------------------------------------- #
def filesystem_usage(now: float) -> tuple[int, int]:
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: ~10 GiB base + slow log creep (max ~800 MiB before logrotate) + wobble
    root_size = 41_943_040   # 40 GiB in KiB
    root_base = 10_485_760   # ~10 GiB
    root_logs = 819_200 * ((now % day) / day)       # 0..800 MiB daily sawtooth
    root_growth = min(524_288, uptime * 0.02)        # forever creep, capped
    root_used = int(root_base + root_logs + root_growth
                    + gauge("fs.root", 0, amp_abs=40_000, period=1600))

    # /srv/backup: ~70 % of 4 TiB. Restic prune runs ~2 h after the
    # backup window and reclaims ~5 GiB daily; slow repo growth between prunes.
    # 4 TiB in KiB: 4 * 1024 GiB * 1048576 KiB/GiB = 4096 * 1048576
    bkp_size_kib = 4096 * 1048576  # 4 TiB in KiB
    bkp_base = int(0.695 * bkp_size_kib)            # ~69.5 % base
    # Daily ~5 GiB sawtooth: repo grows between backups (slow), prune reclaims
    daily_period = day
    prune_reclaim_kib = 5 * 1048576                 # 5 GiB reclaimed per day by prune
    # Sawtooth: rises from 0 to prune_reclaim over the day, drops at midnight
    bkp_growth_daily = int(prune_reclaim_kib * ((now % daily_period) / daily_period))
    # Long-term forever growth: ~100 MiB/day = ~118 KiB/s ... slow creep
    bkp_forever = min(5 * 1048576, int(uptime * 1.3))
    bkp_used = int(bkp_base + bkp_growth_daily + bkp_forever
                   + gauge("fs.bkp", 0, amp_abs=100_000, period=900))

    return root_used, bkp_used, root_size, bkp_size_kib


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output() -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4

    # ---- load: backup host is mostly idle. 15-min load well under 20 (default
    #      WARN for 4 cores). Spikes during nightly window but that's past. ---  #
    l1 = round(gauge("load1",  0.28, amp_frac=0.30, phase=0.2, period=300), 2)
    l5 = round(gauge("load5",  0.22, amp_frac=0.16, phase=1.0, period=900), 2)
    l15 = round(gauge("load15", 0.18, amp_frac=0.08, phase=2.0, period=2400), 2)
    # Clamp positive
    l1 = max(0.01, l1)
    l5 = max(0.01, l5)
    l15 = max(0.01, l15)
    runnable = 1
    total_procs = 298

    # ---- /proc/stat -------------------------------------------------------- #
    user   = C_USER.sample(20)
    system = C_SYSTEM.sample(8)
    idle   = C_IDLE.sample(360)
    iowait = C_IOWAIT.sample(4)

    # ---- diskstat ---------------------------------------------------------- #
    sda_rd  = SDA["rd_ios"].sample(3)
    sda_rdt = SDA["rd_ticks"].sample(2)
    sda_wr  = SDA["wr_ios"].sample(8)
    sda_wrt = SDA["wr_ticks"].sample(5)
    sda_iot = SDA["io_ticks"].sample(6)

    sdb_rd  = SDB["rd_ios"].sample(12)
    sdb_rdt = SDB["rd_ticks"].sample(8)
    sdb_wr  = SDB["wr_ios"].sample(35)
    sdb_wrt = SDB["wr_ticks"].sample(28)
    sdb_iot = SDB["io_ticks"].sample(32)

    rx_bytes = C_RX_B.sample(90_000)
    tx_bytes = C_TX_B.sample(70_000)
    rx_pkts  = C_RX_P.sample(220)
    tx_pkts  = C_TX_P.sample(180)

    # ---- SMART temps (SSD devices, well under 35°C WARN default) ----------- #
    sda_temp = round(gauge("smart.sda.temp", 28, amp_abs=1.0, phase=2.1, period=1100))
    sdb_temp = round(gauge("smart.sdb.temp", 30, amp_abs=1.2, phase=3.4, period=1300))

    sda_smart = _smart_json("/dev/sda", "SAMSUNG MZNLN512HAJQ-000H1",
                            "S3EVNX0K271481", int(uptime / 3600) + 10200, sda_temp)
    sdb_smart = _smart_json("/dev/sdb", "Samsung SSD 870 EVO 4TB",
                            "S62DNX0T908311", int(uptime / 3600) + 8700, sdb_temp)

    # ---- memory: backup host, ~6 GiB used of 8 GiB. No swap pressure. ----- #
    mem_total   = 8_388_608    # 8 GiB in KiB
    swap_total  = 2_097_152    # 2 GiB swap, empty
    commit_limit = swap_total + mem_total // 2   # kernel default

    cached = int(gauge("mem.cached", 2_200_000, amp_frac=0.02, phase=0.4, period=1500))
    buffers   = 180_000
    sreclaim  = 380_000
    swapcached = 0
    caches    = cached + buffers + swapcached + sreclaim
    mem_free  = max(200_000, mem_total - 3_600_000 - caches)
    swap_free = swap_total   # always empty on a healthy backup host
    committed = int(gauge("mem.committed", 4_100_000, amp_frac=0.01, phase=1.2, period=1700))

    shmem = 32_768
    anon  = max(1_000_000, mem_total - mem_free - caches - 500_000)
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = max(mem_free, mem_free + file_lru + sreclaim)
    a_anon = int(anon_lru * 0.55)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.30)
    i_file = file_lru - a_file
    slab   = sreclaim + 98_304
    threads = 186
    kernel_stack = threads * 16   # 16 KiB per thread
    dirty  = max(4_096, int(gauge("mem.dirty", 12_288, amp_frac=0.12,
                                  phase=2.0, period=800)))

    # ---- filesystem usage -------------------------------------------------- #
    root_used, bkp_used, root_size, bkp_size = filesystem_usage(time.time())
    # Keep bkp at 68-72 % — safely under 80/90 % df defaults
    bkp_used = max(int(0.67 * bkp_size), min(int(0.72 * bkp_size), bkp_used))

    # ---- last backup age (loaded from state; increases monotonically) ------- #
    last_bkp_age_s = time.time() - START + _LAST_BACKUP_AGE_S
    # Clamp to 6-8 h for a believable "ran last night" story
    last_bkp_age_s_display = max(6 * 3600, min(8 * 3600,
                                               _LAST_BACKUP_AGE_S + (time.time() - START) % 3600))

    # ---- cert expiry (dynamically 330 days out) ----------------------------- #
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 330 * 86400))

    # ---- timesyncd dynamic timestamps -------------------------------------- #
    last_sync = now - 612
    sync_str  = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))

    lines: list[str] = []
    a = lines.append

    # ----------------------------------------------------------------------- #
    #  <<<check_mk>>>
    # ----------------------------------------------------------------------- #
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

    # ----------------------------------------------------------------------- #
    #  TLS-pretend controller status
    # ----------------------------------------------------------------------- #
    a("<<<cmk_agent_ctl_status:sep(0)>>>")
    a(json.dumps({
        "version": AGENT_VERSION, "agent_socket_operational": True,
        "ip_allowlist": [], "allow_legacy_pull": False,
        "connections": [{
            "site_id": "monitoring/prod", "receiver_port": 8000,
            "uuid": "c38a2b91-4d7e-11ef-9c12-0a7f3e5b8d04",
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Tue, 03 Jun 2025 09:12:44 +0000",
                "to": cert_to}},
            "remote": "remote_query_disabled"}]}, separators=(",", ":")))

    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"'
      % AGENT_VERSION)
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_job:CMK_VERSION="%s"'
      % AGENT_VERSION)

    # ----------------------------------------------------------------------- #
    #  df_v2
    # ----------------------------------------------------------------------- #
    a("<<<df_v2>>>")
    a(f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/sdb1 ext4 {bkp_size} {bkp_used} {bkp_size - bkp_used} "
      f"{round(bkp_used / bkp_size * 100)}% /srv/backup")
    a("[df_inodes_start]")
    a(f"/dev/sda1 ext4 2621440 312814 {2621440 - 312814} 12% /")
    # Backup volume: large files, very few inodes used (~1 %)
    a(f"/dev/sdb1 ext4 268435456 3142 {268435456 - 3142} 1% /srv/backup")
    a("[df_inodes_end]")

    # ----------------------------------------------------------------------- #
    #  mounts
    # ----------------------------------------------------------------------- #
    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    # noatime on the backup volume: DBA-credible for a write-heavy volume
    a("/dev/sdb1 /srv/backup ext4 rw,noatime,data=ordered 0 0")

    # ----------------------------------------------------------------------- #
    #  mem (full 58-key Ubuntu 24.04 /proc/meminfo)
    # ----------------------------------------------------------------------- #
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
    a("Mapped:         220160 kB")
    a(f"Shmem:          {shmem} kB")
    a(f"KReclaimable:   {sreclaim} kB")
    a(f"Slab:           {slab} kB")
    a(f"SReclaimable:   {sreclaim} kB")
    a("SUnreclaim:     98304 kB")
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
    a("Percpu:         9216 kB")
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
    a("DirectMap4k:    196608 kB")
    a("DirectMap2M:    4980736 kB")
    a("DirectMap1G:    3145728 kB")

    # ----------------------------------------------------------------------- #
    #  cpu + uptime
    # ----------------------------------------------------------------------- #
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {20000 + C_PROC.sample(2) % 4999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.8)}.00")

    # ----------------------------------------------------------------------- #
    #  timesyncd (dynamic timestamps)
    # ----------------------------------------------------------------------- #
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.56 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: A297B12C")
    a("    Precision: 1us (-25)")
    a("Root distance: 10.221ms (max: 5s)")
    offset_us = int(gauge("ntp.offset", 0, amp_abs=1200, phase=0.7, period=2400))
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 18.441ms")
    jitter_ms = round(max(0.1, gauge("ntp.jitter", 2.1, amp_frac=0.30, phase=1.5, period=1800)), 3)
    a(f"       Jitter: {jitter_ms:.3f}ms")
    a(f" Packet count: {420 + int((time.time() - START) / 2048)}")
    a("    Frequency: +6.772ppm")
    a(f"[[[{last_sync}]]]")

    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
      "RootDelay=8.114ms, RootDispersion=1.009ms, Reference=A297B12C, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=42, Jitter=2.118ms }")
    a("Timezone=UTC")

    # ----------------------------------------------------------------------- #
    #  apt — green sentinel
    # ----------------------------------------------------------------------- #
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # ----------------------------------------------------------------------- #
    #  kernel (simplified /proc/vmstat subset + cpu line)
    # ----------------------------------------------------------------------- #
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(800)}")
    a(f"processes {C_PROC.sample(2)}")
    a(f"pgmajfault {C_PGMAJ.sample(0.1)}")

    # ----------------------------------------------------------------------- #
    #  diskstat (/proc/diskstats layout)
    # ----------------------------------------------------------------------- #
    a("<<<diskstat>>>")
    a(str(now))
    a(f"8 0 sda {sda_rd} 0 {sda_rd * 16} {sda_rdt} "
      f"{sda_wr} 0 {sda_wr * 24} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0")
    a(f"8 16 sdb {sdb_rd} 0 {sdb_rd * 32} {sdb_rdt} "
      f"{sdb_wr} 0 {sdb_wr * 48} {sdb_wrt} 0 {sdb_iot} {sdb_iot * 2} 0 0 0 0")

    # ----------------------------------------------------------------------- #
    #  lnx_if (both variants — see CLAUDE.md)
    # ----------------------------------------------------------------------- #
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
      "state UP group default qlen 1000")
    a("    link/ether 02:42:ac:11:00:3b brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:3b")

    # ----------------------------------------------------------------------- #
    #  tcp_conn_stats
    # ----------------------------------------------------------------------- #
    a("<<<tcp_conn_stats>>>")
    a(f"01 {round(gauge('tcp.estab', 8, amp_abs=3, phase=0.9, period=700))}")
    a(f"06 {round(gauge('tcp.timewait', 4, amp_abs=2, phase=2.4, period=500))}")
    a("0A 2")

    # ----------------------------------------------------------------------- #
    #  SMART
    # ----------------------------------------------------------------------- #
    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)
    a(sdb_smart)

    # ----------------------------------------------------------------------- #
    #  ps_lnx — backup host processes
    # ----------------------------------------------------------------------- #
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, elapsed, pid, cmd in (
        ("init.scope",
         "root", 168_000, 12_800, "00:00:14", "12-00:08:21", 1,
         "/sbin/init"),
        ("system.slice/systemd-journald.service",
         "root", 58_300, 19_400, "00:00:52", "12-00:07:54", 401,
         "/usr/lib/systemd/systemd-journald"),
        ("system.slice/systemd-udevd.service",
         "root", 25_900, 7_900, "00:00:01", "12-00:07:53", 438,
         "/usr/lib/systemd/systemd-udevd"),
        ("system.slice/systemd-resolved.service",
         "systemd-resolve", 26_600, 13_100, "00:00:28", "12-00:07:52", 489,
         "/usr/lib/systemd/systemd-resolved"),
        ("system.slice/systemd-timesyncd.service",
         "systemd-timesync", 91_000, 7_600, "00:00:08", "12-00:07:51", 503,
         "/usr/lib/systemd/systemd-timesyncd"),
        ("system.slice/dbus.service",
         "messagebus", 10_200, 5_100, "00:00:11", "12-00:07:51", 515,
         "@dbus-daemon --system --address=systemd:"),
        ("system.slice/rsyslog.service",
         "syslog", 222_400, 6_700, "00:00:22", "12-00:07:50", 612,
         "/usr/sbin/rsyslogd -n -iNONE"),
        ("system.slice/ssh.service",
         "root", 15_400, 9_000, "00:00:01", "12-00:07:48", 690,
         "sshd: /usr/sbin/sshd -D [listener]"),
        ("system.slice/cron.service",
         "root", 11_500, 2_500, "00:00:01", "12-00:07:47", 705,
         "/usr/sbin/cron -f -P"),
        ("system.slice/smartmontools.service",
         "root", 12_012, 5_904, "00:00:00", "12-00:07:45", 820,
         "/usr/sbin/smartd -n"),
        # restic-backup.timer triggers the job (currently idle; last run ~6 h ago)
        # The systemd-timer daemon itself doesn't stay resident — the oneshot service
        # ran and exited. We show the timer process waiting for next trigger.
        ("system.slice/atd.service",
         "daemon", 9_800, 2_200, "00:00:00", "12-00:07:43", 834,
         "/usr/sbin/atd -f"),
        ("system.slice/multipathd.service",
         "root", 41_200, 15_400, "00:00:04", "12-00:07:42", 850,
         "/sbin/multipathd -d -s"),
        ("system.slice/networkd-dispatcher.service",
         "root", 31_500, 11_200, "00:00:00", "12-00:07:41", 890,
         "/usr/bin/python3 /usr/bin/networkd-dispatcher --run-startup-triggers"),
        ("system.slice/polkit.service",
         "root", 24_200, 9_800, "00:00:02", "12-00:07:40", 910,
         "/usr/lib/polkit-1/polkitd --no-debug"),
        ("system.slice/systemd-logind.service",
         "root", 30_800, 9_100, "00:00:03", "12-00:07:40", 930,
         "/usr/lib/systemd/systemd-logind"),
        ("system.slice/systemd-networkd.service",
         "root", 27_400, 9_600, "00:00:12", "12-00:07:39", 960,
         "/usr/lib/systemd/systemd-networkd"),
        ("system.slice/udisks2.service",
         "root", 32_600, 12_800, "00:00:01", "12-00:07:38", 980,
         "/usr/lib/udisks2/udisksd"),
        ("system.slice/unattended-upgrades.service",
         "root", 68_000, 22_400, "00:00:00", "12-00:07:37", 1002,
         "/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal"),
        # two SSH session placeholders (ops checking backup status)
        ("user.slice/user-0.slice/session-1.scope",
         "root", 15_600, 9_200, "00:00:00", "00:02:14", 8210,
         "sshd: root@pts/0"),
        ("user.slice/user-0.slice/session-1.scope",
         "root", 7_800, 4_100, "00:00:00", "00:02:14", 8211,
         "-bash"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} {elapsed} {pid} {cmd}")

    # ----------------------------------------------------------------------- #
    #  systemd_units — ~30 units, all green
    #  restic-backup.service is active/exited (last run succeeded)
    #  restic-prune.service is active/exited (ran after the backup)
    # ----------------------------------------------------------------------- #
    a("<<<systemd_units>>>")
    units = [
        # The two backup oneshots — ran and exited successfully last night
        ("restic-backup.service",    "active",   "exited",  "Restic nightly backup to offsite repo"),
        ("restic-prune.service",     "active",   "exited",  "Restic repository prune and compact"),
        # Timers
        ("restic-backup.timer",      "active",   "waiting", "Timer: nightly restic backup"),
        ("restic-prune.timer",       "active",   "waiting", "Timer: nightly restic prune"),
        # Standard Ubuntu 24.04 services
        ("ssh.service",              "active",   "running", "OpenBSD Secure Shell server"),
        ("cron.service",             "active",   "running", "Regular background program processing daemon"),
        ("atd.service",              "active",   "running", "Deferred execution scheduler"),
        ("dbus.service",             "active",   "running", "D-Bus System Message Bus"),
        ("multipathd.service",       "active",   "running", "Device-Mapper Multipath Device Controller"),
        ("networkd-dispatcher.service", "active", "running", "Dispatcher daemon for systemd-networkd"),
        ("polkit.service",           "active",   "running", "Authorization Manager"),
        ("rsyslog.service",          "active",   "running", "System Logging Service"),
        ("smartmontools.service",    "active",   "running", "Self Monitoring and Reporting Technology (SMART) Daemon"),
        ("systemd-journald.service", "active",   "running", "Journal Service"),
        ("systemd-logind.service",   "active",   "running", "User Login Management"),
        ("systemd-networkd.service", "active",   "running", "Network Configuration"),
        ("systemd-resolved.service", "active",   "running", "Network Name Resolution"),
        ("systemd-timesyncd.service","active",   "running", "Network Time Synchronization"),
        ("systemd-udevd.service",    "active",   "running", "Rule-based Manager for Device Events and Files"),
        ("udisks2.service",          "active",   "running", "Disk Manager"),
        ("unattended-upgrades.service", "active","running", "Unattended Upgrades Shutdown"),
        ("user@0.service",           "active",   "running", "User Manager for UID 0"),
        ("getty@tty1.service",       "active",   "running", "Getty on tty1"),
        # Oneshots / exited
        ("apparmor.service",         "active",   "exited",  "Load AppArmor profiles"),
        ("blk-availability.service", "active",   "exited",  "Availability of block devices"),
        ("console-setup.service",    "active",   "exited",  "Set console font and keymap"),
        ("finalrd.service",          "active",   "exited",  "Create final runtime dir for shutdown pivot root"),
        ("keyboard-setup.service",   "active",   "exited",  "Set the console keyboard layout"),
        ("lvm2-monitor.service",     "active",   "exited",  "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or progress polling"),
        ("setvtrgb.service",         "active",   "exited",  "Set console scheme"),
        ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # ----------------------------------------------------------------------- #
    #  job — two mk_job entries: nightly restic backup + prune, both OK
    #  Format (verified against cmk/plugins/job/agent_based/job.py):
    #    ==> <jobname> <==
    #    start_time <unix_epoch>
    #    exit_code <int>
    #    real_time <M:SS.s>
    #    user_time <float>
    #    system_time <float>
    #    max_res_kbytes <int>
    #    avg_mem_kbytes <int>
    # ----------------------------------------------------------------------- #
    a("<<<job>>>")

    # restic-backup: ran ~6-8 h ago, took ~48 min, exited 0
    bkp_start = now - int(last_bkp_age_s_display)
    a("==> restic-backup <==")
    a(f"start_time {bkp_start}")
    a("exit_code 0")
    a("real_time 48:12.4")
    a("user_time 142.80")
    a("system_time 38.20")
    a("max_res_kbytes 524288")
    a("avg_mem_kbytes 0")

    # restic-prune: ran ~30 min after the backup, took ~8 min, exited 0
    prune_start = bkp_start + 50 * 60   # prune triggered 50 min after backup start
    a("==> restic-prune <==")
    a(f"start_time {prune_start}")
    a("exit_code 0")
    a("real_time 8:03.7")
    a("user_time 24.60")
    a("system_time 8.10")
    a("max_res_kbytes 204800")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence
# --------------------------------------------------------------------------- #
def save_state() -> None:
    if not STATE_FILE:
        return
    data = {
        "version": 1,
        "start": START,
        "last_backup_age_s": _LAST_BACKUP_AGE_S,
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
    global START, _LAST_BACKUP_AGE_S
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[state] load failed ({exc}) — starting fresh")
        return
    START = data["start"]
    _LAST_BACKUP_AGE_S = data.get("last_backup_age_s", _LAST_BACKUP_AGE_S)
    saved = data.get("counters", {})
    restored = 0
    for name, c in _ALL_COUNTERS.items():
        if name in saved:
            c.acc, c.last = saved[name]
            restored += 1
    print(f"[state] restored: {restored}/{len(_ALL_COUNTERS)} counters, uptime continuous")


# --------------------------------------------------------------------------- #
#  TCP agent server
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
#  Admin HTTP UI (status only, no toggles)
# --------------------------------------------------------------------------- #
def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _admin_page() -> str:
    uptime_s = int(time.time() - START) + UPTIME_OFFSET
    last_bkp_h = round(_LAST_BACKUP_AGE_S / 3600, 1)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>{HOSTNAME} — backup host status</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:56rem; padding:0 1rem; }}
 h1   {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }}
 h1 b {{ color:#d8dee4; }}
 .badge {{ display:inline-block; padding:.4rem 1.2rem; border-radius:.4rem;
          color:#fff; font-weight:700; font-size:1.6rem; letter-spacing:.05em;
          background:#2e7d32; }}
 .info  {{ margin:1.2rem 0 0; color:#9aa4af; line-height:1.8; }}
 .info b {{ color:#d8dee4; }}
 .note  {{ margin:1.6rem 0 0; padding:1rem 1.2rem; border:1px solid #333;
          border-radius:.5rem; background:#22262b; font-size:.9rem; color:#9aa4af; }}
 .note b {{ color:#aabbcc; }}
 .foot  {{ margin-top:2rem; color:#666; font-size:.85rem; }}
</style></head><body>
 <h1>demo host — <b>{HOSTNAME}</b> <span style="color:#555">(refreshes every 30 s)</span></h1>
 <div class="badge">HEALTHY</div>
 <div class="info">
  Host uptime: <b>{_fmt_duration(uptime_s)}</b><br>
  Role: <b>Restic backup host — Meridian Retail offsite backups</b><br>
  Last backup: <b>{last_bkp_h:.1f} h ago</b> (exit code 0, real_time 48m 12s)<br>
  Last prune: <b>{last_bkp_h + 0.8:.1f} h ago</b> (exit code 0, real_time 8m 4s)<br>
  Next backup window: <b>~01:00 UTC</b> (nightly timer)
 </div>
 <div class="note">
  <b>Steady-green background host.</b> There is no incident on this host and no
  break/heal toggle. The purpose of this host is to fill out the estate and
  provide realistic <b>Job restic-backup</b> and <b>Job restic-prune</b>
  services in Checkmk (both permanently OK) that corroborate a healthy backup
  posture for "Meridian Retail".
 </div>
 <div class="foot">JSON status: <a href="/" style="color:#9aa4af">/</a></div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "backup-demo-ctl/1.0"

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
                    "tagline": "Steady-green background host — backups always finish OK. "
                               "No incident and no toggle.",
                    "effects": [
                        "restic backup job runs nightly, last run exit 0 — green",
                        "Both drives healthy (SMART raw attrs at 0), calm I/O, "
                        "filesystems well within levels",
                        "CPU / memory / network wobble naturally within green",
                        "No state to change — this host never alerts",
                    ]}},
            })
        return self._send_json({
            "host": HOSTNAME,
            "role": "restic backup host",
            "state": "healthy",
            "uptime_s": int(time.time() - START) + UPTIME_OFFSET,
            "last_backup_age_h": round(_LAST_BACKUP_AGE_S / 3600, 2),
            "last_backup_exit_code": 0,
            "ui": "/admin",
        })


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http  = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    print(f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  http=tcp/{HTTP_PORT}")
    print(f"[boot] status UI: http://localhost:{HTTP_PORT}/admin")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
