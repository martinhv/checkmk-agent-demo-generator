#!/usr/bin/env python3
"""Meridian Retail demo host: db-postgres-02 — the PostgreSQL read replica.

The hot-standby sibling of `db-postgres-01` (the primary). It streams WAL from
the primary and serves the *analytics / reporting* read traffic — the BI tool,
nightly dashboards, ad-hoc SQL. Its hardware is **healthy** (this is a
different failure class from db-postgres-01's dying disk: disk, SMART and
memory all stay green). The incident is **connection-pool exhaustion**:

A runaway BI/reporting client (a connection leak in the analytics service)
opens PostgreSQL connections and never closes them — they accumulate as
idle / idle-in-transaction backends. The connection count climbs toward
`max_connections` (200). `postgres_connections` is the ONE postgres check that
alerts by default (80 %/90 % of max_connections), so:

  "PostgreSQL Connections MAIN/analytics" goes WARN at 160 (80 %) and CRIT at
  180 (90 %). Corroboration (all graph-only, nothing else red): the idle
  backends pile up in "PostgreSQL Daemon Sessions" (t/f), connect time creeps
  (every new client waits longer for a free slot), numbackends in
  pg_stat_database tracks the leak, and ps shows the leaked backends.

The AI fuses: connections% near max + the idle-in-transaction backend pile-up +
slow connect time -> "a client is leaking connections toward max_connections;
new clients can't connect — kill the leaking BI session / add pooling".

Three states (the timeline is part of the story):

  healthy   ~30/200 connections, all green. Replica streaming fine.
  degraded  the BI job starts leaking: connections climb 90 -> ~150/200,
            approaching but UNDER the 80 % WARN (160); idle backends grow.
            The breadcrumb. Trigger ~15-20 min before showtime; auto-escalates.
  broken    the leak runs away: connections > 180/200 -> CRIT (90 %); many
            idle-in-transaction backends; connect time elevated. The connection
            count GROWS live across re-polls (capped just under max_connections,
            the leak can't open the 200th).

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env (see also AGENT_PORT/HTTP_PORT/START_STATE/STATE_FILE):
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before the leak runs away to CRIT
                 (default: 18; 0 disables)
  LEAK_FILL_MIN  minutes for the leak to climb 90 -> ~150 conns while degraded
                 (default: 16; the connections graph climbs over this window)
  BREAK_RAMP_MIN minutes for the broken impact (CRIT conns / connect-time creep)
                 to reach full force (default: 4; 0 = instant)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "db-postgres-02.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "18"))
LEAK_FILL_MIN = float(os.environ.get("LEAK_FILL_MIN", "16"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "4"))

# max_connections on the replica — the denominator for the 80/90 % connection
# levels. The leak's whole drama is the climb toward this number.
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "200"))

START = time.time()
UPTIME_OFFSET = 12 * 86400  # pretend the host has been up ~12 days (sibling of -01)

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the BI leak started (degraded or broken) -> drives the rising conn count
_degraded_since: float | None = None if _state == "healthy" else START
# when the runaway/incident started -> drives the CRIT conn count + connect time
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
    save_state()  # toggles must survive a restart mid-demo


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
#  The single driver of the incident: how many connections the leak holds open.
#
#  Healthy: ~30 connections (a handful of dashboards + the streaming/system
#  backends). Degraded: the BI leak climbs the count from ~90 to ~150 over
#  LEAK_FILL_MIN — approaching but staying UNDER the 80 % WARN (160 of 200) so
#  nothing is red yet (the breadcrumb). Broken: the leak runs away past the
#  90 % CRIT (180) and keeps creeping, capped just under max_connections (the
#  leak literally can't open the last slot). The count GROWS live across
#  re-polls in the broken state, exactly like the dying-disk stuck-query grew.
#
#  These connections are mostly idle / idle-in-transaction (a leak holds slots,
#  it doesn't run queries) — so they pile onto the *idle* connection count,
#  which has the same 80/90 % default levels as active. ps, postgres_sessions
#  (t/f), postgres_connections and pg_stat_database.numbackends are ALL derived
#  from these same numbers so they can never disagree.
# --------------------------------------------------------------------------- #
HEALTHY_CONNS = 30
DEGRADED_PEAK = 150  # idle ~146 -> 73 %, safely under the 80 % WARN (160) even with wobble
# leave a couple of slots so the leak never quite reaches max (realistic: the
# last connections fail with "too many clients already" — count plateaus high)
BROKEN_CAP = MAX_CONNECTIONS - 6  # 194 of 200


def connection_counts() -> tuple[int, int]:
    """Return (idle_connections, active_connections) on the busiest DB.

    Deterministic functions of wall-clock time-in-state (+ a small smooth
    wobble), so re-polls within a poll interval agree and the count grows
    monotonically while the incident worsens.
    """
    ds = degraded_seconds()
    bs = broken_seconds()

    # active (genuinely-running) queries stay modest the whole time — analytics
    # reads, a handful in flight. The leak is about *held* connections, not load.
    active = round(gauge("pg.active", 4, amp_abs=1.5, phase=0.7, period=400))
    active = max(1, active)

    if ds <= 0:
        # healthy: ~30 total, almost all idle (pooled dashboards holding slots)
        idle = round(gauge("pg.idle", HEALTHY_CONNS - 4, amp_abs=3,
                           phase=1.3, period=600))
        return max(1, idle), active

    # degraded: leak climbs 90 -> DEGRADED_PEAK over LEAK_FILL_MIN
    if LEAK_FILL_MIN <= 0:
        deg_frac = 1.0
    else:
        deg_frac = min(1.0, ds / (LEAK_FILL_MIN * 60.0))
    idle_total = _lerp(90, DEGRADED_PEAK, deg_frac)

    # broken: runaway climb DEGRADED_PEAK -> BROKEN_CAP, then plateau just under
    # max_connections. Grows live (broken_seconds keeps rising).
    if bs > 0:
        runaway = min(1.0, bs / (BREAK_RAMP_MIN * 60.0)) if BREAK_RAMP_MIN > 0 else 1.0
        # the runaway crosses the 90 % CRIT during the ramp, then an extra slow
        # creep keeps it growing visibly poll-by-poll, asymptoting to BROKEN_CAP
        # (the leak can't open the last few slots). Time constant ~4 min so the
        # count still ticks up between 1-min polls long after the ramp.
        creep = (BROKEN_CAP - 184) * (1.0 - math.exp(-bs / 240.0))
        idle_total = max(idle_total,
                         _lerp(DEGRADED_PEAK, 184, runaway) + creep)

    idle = round(idle_total + gauge("pg.idle", 0, amp_abs=1.2,
                                    phase=1.3, period=600))
    idle = max(HEALTHY_CONNS, min(BROKEN_CAP - active, idle))
    return idle, active


# --------------------------------------------------------------------------- #
#  Autocorrelated gauges + monotonic counters (verbatim machinery from the
#  dying-disk reference; see CLAUDE.md for why a single sine is wrong).
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


# /proc/stat jiffies: 100 Hz * 4 CPUs = ~400 ticks/s. A replica doing analytics
# reads: moderate user, mostly idle. Stays GREEN in every state (CPU is never
# the problem — the connection slots run out, the machine is fine).
C_USER = Counter("cpu.user", phase=0.3, start=_aged(48))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(20))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(320))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(8))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(2400))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(6))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(1))

SDA = {  # system disk: root + WAL replay. Always calm (healthy hardware).
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(5)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(3)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(10)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(8)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(14)),
}
SDB = {  # data SSD (/var/lib/postgresql): healthy, calm reads. WAL apply only.
    "rd_ios": Counter("sdb.rd_ios", phase=1.0, start=_aged(60)),
    "rd_ticks": Counter("sdb.rd_ticks", phase=1.2, start=_aged(25)),
    "wr_ios": Counter("sdb.wr_ios", phase=1.4, start=_aged(95)),
    "wr_ticks": Counter("sdb.wr_ticks", phase=1.6, start=_aged(48)),
    "io_ticks": Counter("sdb.io_ticks", phase=1.8, amp=0.05, start=_aged(70)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(520_000))  # WAL stream in
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(410_000))  # query results out
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(1600))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(1500))

# pg_stat_database counters for the `analytics` DB (the reporting workload). On
# a read replica there are no commits to user tables, but read-only transactions
# still COMMIT, and the leak's idle-in-transaction backends keep BEGIN/SELECTs
# alive. Throughput stays roughly flat (the machine is fine) — corroboration,
# not an alarm.
PG_ANA = {
    "xact_commit": Counter("pg.analytics.xact_commit", phase=0.5, start=_aged(85)),
    "xact_rollback": Counter("pg.analytics.xact_rollback", phase=0.9, start=_aged(0.3)),
    "blks_read": Counter("pg.analytics.blks_read", phase=1.3, start=_aged(120)),
    "blks_hit": Counter("pg.analytics.blks_hit", phase=1.7, start=_aged(16000)),
    "tup_returned": Counter("pg.analytics.tup_returned", phase=2.1, start=_aged(48000)),
    "tup_fetched": Counter("pg.analytics.tup_fetched", phase=2.5, start=_aged(12000)),
    "tup_inserted": Counter("pg.analytics.tup_inserted", phase=2.9, start=_aged(0)),
    "tup_updated": Counter("pg.analytics.tup_updated", phase=3.3, start=_aged(0)),
    "tup_deleted": Counter("pg.analytics.tup_deleted", phase=3.7, start=_aged(0)),
}
# the near-idle `postgres` maintenance DB (needs xact_commit > 0 to discover)
PG_SYS = {
    "xact_commit": Counter("pg.postgres.xact_commit", phase=0.4, start=_aged(0.5)),
    "blks_hit": Counter("pg.postgres.blks_hit", phase=1.0, start=_aged(22)),
    "tup_returned": Counter("pg.postgres.tup_returned", phase=1.4, start=_aged(28)),
}


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
        "ata_smart_attributes": {"table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 12, "name": "Power_Cycle_Count", "value": 100, "thresh": 0,
             "raw": {"value": 37}},
            {"id": 187, "name": "Reported_Uncorrect", "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 177, "name": "Wear_Leveling_Count", "value": 94, "thresh": 5,
             "raw": {"value": 118}},
            {"id": 179, "name": "Used_Rsvd_Blk_Cnt_Tot", "value": 100, "thresh": 10,
             "raw": {"value": 0}},
        ]},
    }
    return json.dumps(doc, separators=(",", ":"))


def _kb(mib: float) -> int:
    return int(mib * 1024)


def filesystem_usage(now: float) -> tuple[int, int]:
    """root / and /var/lib/postgresql — both green, growing + cleaned over time.

    A replica's data volume mirrors the primary's size (it replays the same WAL)
    so it shows the same WAL recycle sawtooth + slow table growth, well under the
    df defaults. The leak does NOT touch disk — these stay calm in every state.
    """
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0
    root_base = 14_680_064                                  # ~14 GiB of 40
    root_logs = 1_048_576 * ((now % day) / day)             # 0..1 GiB daily
    root_growth = min(1_572_864, uptime * 0.04)
    root_used = int(root_base + root_logs + root_growth
                    + gauge("fs.root", 0, amp_abs=90_000, period=1500))
    data_base = 121_634_816                                 # ~116 GiB (mirrors -01)
    wal = 1_572_864 * ((now % 720.0) / 720.0)               # 0..1.5 GiB, 12-min teeth
    db_growth = min(6_291_456, uptime * 2.0)                # ~2 kB/s, capped ~6 GiB
    data_used = int(data_base + wal + db_growth
                    + gauge("fs.data", 0, amp_abs=300_000, period=900))
    return root_used, data_used


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4
    broken = state == "broken"

    # The connection leak is the ONLY thing that moves. Everything else stays
    # green and corroborates: low noise, one root cause (the storyline rule).
    idle_sess, run_sess = connection_counts()
    total_conns = idle_sess + run_sess

    # ---- memory: healthy 16 GiB replica. Usage stays green in EVERY state —
    #      a connection leak costs a little RAM per backend (~each backend maps
    #      shared_buffers but only touches a few MB of private memory), so RAM
    #      ticks up slightly with the backend count, never near an alert. ---- #
    mem_total = 16_384_000  # kB
    swap_total = 4_194_300
    commit_limit = swap_total + mem_total // 2  # kernel default
    # ~2.5 KiB private RSS bump per leaked backend — a few hundred MB at worst.
    backend_mem = total_conns * 2560
    mem_free = int(gauge("mem.free", 3_100_000, amp_frac=0.015,
                         phase=0.4, period=1500)) - backend_mem
    mem_free = max(1_800_000, mem_free)
    mem_available = int(gauge("mem.avail", 8_200_000, amp_frac=0.012,
                              phase=1.2, period=1700)) - backend_mem
    mem_available = max(2_500_000, mem_available)
    cached = 6_700_000
    shmem = 2_621_440          # 2.5 GiB shared_buffers (< 20 % of RAM -> green)
    dirty = max(8_192, int(gauge("mem.dirty", 18_432, amp_frac=0.12,
                                 phase=2.0, period=800)))
    committed = int(gauge("mem.committed", 6_900_000 + backend_mem,
                          amp_frac=0.01, phase=1.2, period=1700))

    # ---- load: modest analytics load; nudged up slightly by the extra
    #      backends but stays GREEN (15-min well under the 20 WARN; the CPU is
    #      fine — slots run out, not cores). ------------------------------- #
    load_bump = min(2.5, total_conns / 90.0)  # connections add a little runqueue
    l1 = round(gauge("load1", 1.6 + load_bump, amp_frac=0.22, phase=0.2, period=300), 2)
    l5 = round(gauge("load5", 1.5 + load_bump * 0.9, amp_frac=0.12, phase=1.0, period=900), 2)
    l15 = round(gauge("load15", 1.4 + load_bump * 0.8, amp_frac=0.06, phase=2.0, period=2400), 2)
    runnable = 2 + round(run_sess / 4)
    total_procs = 300 + total_conns

    # ---- /proc/stat: moderate user (analytics scans), mostly idle. Green. -- #
    user = C_USER.sample(48 + total_conns * 0.05)
    system = C_SYSTEM.sample(20)
    idle = C_IDLE.sample(320)
    iowait = C_IOWAIT.sample(8)

    # ---- diskstat: healthy SSDs, calm in every state (the leak is not I/O). - #
    sda_rd = SDA["rd_ios"].sample(5)
    sda_rdt = SDA["rd_ticks"].sample(3)
    sda_wr = SDA["wr_ios"].sample(10)
    sda_wrt = SDA["wr_ticks"].sample(8)
    sda_iot = SDA["io_ticks"].sample(14)
    sdb_rd = SDB["rd_ios"].sample(60)
    sdb_rdt = SDB["rd_ticks"].sample(25)
    sdb_wr = SDB["wr_ios"].sample(95)
    sdb_wrt = SDB["wr_ticks"].sample(48)
    sdb_iot = SDB["io_ticks"].sample(70)
    sdb_queue = random.randint(0, 1)

    rx_bytes = C_RX_B.sample(520_000)
    tx_bytes = C_TX_B.sample(410_000)
    rx_pkts = C_RX_P.sample(1600)
    tx_pkts = C_TX_P.sample(1500)

    sda_temp = round(gauge("smart.sda.temp", 29, amp_abs=1.2, phase=2.1, period=1100))
    sdb_temp = round(gauge("smart.sdb.temp", 30, amp_abs=1.3, phase=0.7, period=900))
    sda_smart = _smart_json("/dev/sda", "INTEL SSDSC2KB240G8", "PHYF108200KL240A",
                            int(uptime / 3600) + 22000, sda_temp)
    sdb_smart = _smart_json("/dev/sdb", "SAMSUNG MZ7L3480HCHQ-00A07", "S6KSNG0T618907",
                            int(uptime / 3600) + 26000, sdb_temp)

    lines: list[str] = []
    a = lines.append

    # Header mirrors a real 2.5 agent install.
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
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 324 * 86400))
    a(json.dumps({
        "version": AGENT_VERSION, "agent_socket_operational": True,
        "ip_allowlist": [], "allow_legacy_pull": False,
        "connections": [{
            "site_id": "monitoring/prod", "receiver_port": 8000,
            "uuid": "7c9a1f02-3b8e-4d51-9a6c-1e4d2f8b7a55",
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Tue, 03 Jun 2025 09:12:44 +0000", "to": cert_to}},
            "remote": "remote_query_disabled"}]}, separators=(",", ":")))
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/mk_postgres.py:CMK_VERSION="%s"'
      % AGENT_VERSION)
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"'
      % AGENT_VERSION)

    # --- filesystems: / on sda, the DB volume on sdb. Both green, growing +
    #     cleaned over time (WAL recycle teeth + slow growth). ---
    a("<<<df_v2>>>")
    root_size = 41_943_040    # 40 GiB
    data_size = 468_713_472   # ~447 GiB usable of the 480 GB data SSD
    root_used, data_used = filesystem_usage(time.time())
    a(f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/sdb1 ext4 {data_size} {data_used} {data_size - data_used} "
      f"{round(data_used / data_size * 100)}% /var/lib/postgresql")
    a("[df_inodes_start]")
    root_inodes = 2_621_440
    a(f"/dev/sda1 ext4 {root_inodes} 301722 {root_inodes - 301722} 12% /")
    data_inodes = 29_302_784
    a(f"/dev/sdb1 ext4 {data_inodes} 46118 {data_inodes - 46118} 1% "
      "/var/lib/postgresql")
    a("[df_inodes_end]")

    # --- mount options (noatime on the DB volume — standard DBA practice) ---
    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sdb1 /var/lib/postgresql ext4 rw,noatime,errors=remount-ro 0 0")

    # --- memory: full /proc/meminfo so the Memory service yields the whole set
    a("<<<mem>>>")
    a(f"MemTotal:       {mem_total} kB")
    a(f"MemFree:        {mem_free} kB")
    a(f"MemAvailable:   {mem_available} kB")
    a(f"Buffers:        {_kb(190)} kB")
    a(f"Cached:         {cached} kB")
    a("SwapCached:     0 kB")
    a("Active:         6024110 kB")
    a("Inactive:       6390220 kB")
    a("Active(anon):   4612300 kB")
    a("Inactive(anon): 3402880 kB")
    a("Active(file):   1411810 kB")
    a("Inactive(file): 2987340 kB")
    a("Unevictable:    0 kB")
    a("Mlocked:        0 kB")
    a(f"SwapTotal:      {swap_total} kB")
    a(f"SwapFree:       {swap_total} kB")
    a("Zswap:          0 kB")
    a("Zswapped:       0 kB")
    a(f"Dirty:          {dirty} kB")
    a("Writeback:      0 kB")
    a("AnonPages:      5388420 kB")
    a("Mapped:         478208 kB")
    a(f"Shmem:          {shmem} kB")
    a("KReclaimable:   511360 kB")
    a("Slab:           632800 kB")
    a("SReclaimable:   511360 kB")
    a("SUnreclaim:     121440 kB")
    a(f"KernelStack:    {6000 + total_conns * 16} kB")  # ~16 KiB per backend thread
    a("PageTables:     94208 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    59392 kB")
    a("VmallocChunk:   0 kB")
    a("Percpu:         16384 kB")
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
    a("DirectMap4k:    321024 kB")
    a("DirectMap2M:    6814720 kB")
    a("DirectMap1G:    9437184 kB")

    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {31000 + C_PROC.sample(6) % 9999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.1)}.00")

    # --- systemd-timesyncd: dynamic timestamps (both checked against wall clock)
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
    a("Root distance: 10.942ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 20.114ms")
    a(f"       Jitter: {random.randint(800, 3200) / 1000:.3f}ms")
    a(f" Packet count: {540 + int((time.time() - START) / 2048)}")
    a("    Frequency: +11.602ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
      "RootDelay=10.118ms, RootDispersion=1.287ms, Reference=B97D5A38, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=61, Jitter=1.204ms }")
    a("Timezone=UTC")

    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(2400)}")
    a(f"processes {C_PROC.sample(6)}")
    a(f"pgmajfault {C_PGMAJ.sample(1)}")

    a("<<<diskstat>>>")
    a(str(now))
    a(f"8 0 sda {sda_rd} 0 {sda_rd * 24} {sda_rdt} {sda_wr} 0 "
      f"{sda_wr * 48} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0")
    a(f"8 16 sdb {sdb_rd} 0 {sdb_rd * 64} {sdb_rdt} {sdb_wr} 0 "
      f"{sdb_wr * 96} {sdb_wrt} {sdb_queue} {sdb_iot} {sdb_iot * 3} 0 0 0 0")

    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
      "state UP group default qlen 1000")
    a("    link/ether 02:42:ac:11:00:41 brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:41")

    # --- tcp connection stats: client connections to the DB. The leak shows up
    #     here too (established climbs with the backend count) — corroboration. -
    a("<<<tcp_conn_stats>>>")
    a(f"01 {total_conns + round(gauge('tcp.estab', 6, amp_abs=4, phase=0.9, period=700))}")
    a(f"02 {random.randint(0, 1)}")
    a(f"06 {round(gauge('tcp.timewait', 11, amp_abs=4, phase=2.4, period=500))}")
    a("0A 5")

    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)
    a(sdb_smart)

    # --- processes: postmaster (+ a WAL receiver, since this is a standby) +
    #     helpers + client backends + system daemons. ps MUST agree with
    #     pg_stat_activity / postgres_sessions / numbackends: total_conns
    #     backends, run_sess of them running a query, the rest idle /
    #     idle-in-transaction (the leaked ones). Every backend maps
    #     shared_buffers, so VSZ > Shmem (2.5 GiB). ---
    pg_vsz = 2_950_000  # > shared_buffers, incl. mapped segment
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
            ("init.scope", "root", 168_000, 13_000, "00:00:36", 1, "/sbin/init"),
            ("system.slice/systemd-journald.service", "root", 63_800, 21_400,
             "00:01:33", 410, "/usr/lib/systemd/systemd-journald"),
            ("system.slice/systemd-udevd.service", "root", 26_100, 8_100,
             "00:00:04", 447, "/usr/lib/systemd/systemd-udevd"),
            ("system.slice/systemd-resolved.service", "systemd-resolve", 26_700, 13_400,
             "00:00:52", 498, "/usr/lib/systemd/systemd-resolved"),
            ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_000, 7_700,
             "00:00:11", 516, "/usr/lib/systemd/systemd-timesyncd"),
            ("system.slice/dbus.service", "messagebus", 10_300, 5_100,
             "00:00:19", 528, "@dbus-daemon --system --address=systemd:"),
            ("system.slice/rsyslog.service", "syslog", 222_400, 6_800,
             "00:00:43", 636, "/usr/sbin/rsyslogd -n -iNONE"),
            ("system.slice/smartmontools.service", "root", 13_100, 6_200,
             "00:00:08", 650, "/usr/sbin/smartd -n"),
            ("system.slice/ssh.service", "root", 15_400, 9_000,
             "00:00:01", 705, "sshd: /usr/sbin/sshd -D [listener]"),
            ("system.slice/cron.service", "root", 11_500, 2_500,
             "00:00:03", 716, "/usr/sbin/cron -f -P"),
            ("system.slice/pgbouncer.service", "postgres", 18_900, 7_300,
             "00:11:48", 781, "/usr/sbin/pgbouncer -d /etc/pgbouncer/pgbouncer.ini"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 12-01:52:40 {pid} {cmd}")
    cg = "0::/system.slice/postgresql.service"
    a(f"{cg} postgres {pg_vsz} 158000 00:52:11 12-01:52:08 802 "
      f"/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main")
    # standby-specific + standard helpers (a hot standby runs a startup/recovery
    # process + a walreceiver streaming from the primary)
    for i, (helper, rss) in enumerate((
            ("startup recovering 0000000100000A2F0000003C", 1_980_000),
            ("walreceiver streaming A2F/3C418800", 142_000),
            ("checkpointer", 2_350_000),
            ("background writer", 1_650_000),
            ("autovacuum launcher", 92_000),
            ("logical replication launcher", 84_000))):
        a(f"{cg} postgres {pg_vsz} {rss} 00:0{i}:1{i} 12-01:52:05 "
          f"{805 + i} postgres: 16/main: {helper}")
    # client backends: run_sess running a SELECT, the rest idle / idle in
    # transaction (the leaked BI connections). Counts match the sessions /
    # connections / numbackends sections exactly.
    for i in range(total_conns):
        if i < run_sess:
            verb = "SELECT"
        elif i < run_sess + max(0, idle_sess - (idle_sess * 2 // 3)):
            verb = "idle"
        else:
            verb = "idle in transaction"   # the leaked BI connections
        rss = 168_000 + (i * 41) % 220 * 1000
        a(f"{cg} postgres {pg_vsz} {rss} 00:0{i % 9}:{10 + i % 50:02d} "
          f"0-00:{10 + i % 48:02d}:0{i % 9} {2200 + i} "
          f"postgres: 16/main: analytics bi_reporter 10.1.4.{60 + i % 12}"
          f"(5{3200 + i}) {verb}")

    # --- systemd units: ALL green in every state — the DB and replica are UP,
    #     the failure is "no free connection slots", not a crashed unit. ---
    a("<<<systemd_units>>>")
    units = [
        ("postgresql.service", "active", "running", "PostgreSQL RDBMS"),
        ("pgbouncer.service", "active", "running", "connection pooler for PostgreSQL"),
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
        ("smartmontools.service", "active", "running",
         "Self Monitoring and Reporting Technology (SMART) Daemon"),
        ("snapd.service", "active", "running", "Snap Daemon"),
        ("systemd-journald.service", "active", "running", "Journal Service"),
        ("systemd-logind.service", "active", "running", "User Login Management"),
        ("systemd-networkd.service", "active", "running", "Network Configuration"),
        ("systemd-resolved.service", "active", "running", "Network Name Resolution"),
        ("systemd-timesyncd.service", "active", "running",
         "Network Time Synchronization"),
        ("systemd-udevd.service", "active", "running",
         "Rule-based Manager for Device Events and Files"),
        ("udisks2.service", "active", "running", "Disk Manager"),
        ("unattended-upgrades.service", "active", "running",
         "Unattended Upgrades Shutdown"),
        ("user@1000.service", "active", "running", "User Manager for UID 1000"),
        ("apparmor.service", "active", "exited", "Load AppArmor profiles"),
        ("blk-availability.service", "active", "exited",
         "Availability of block devices"),
        ("console-setup.service", "active", "exited", "Set console font and keymap"),
        ("finalrd.service", "active", "exited",
         "Create final runtime dir for shutdown pivot root"),
        ("keyboard-setup.service", "active", "exited", "Set the console keyboard layout"),
        ("lvm2-monitor.service", "active", "exited",
         "Monitoring of LVM2 mirrors, snapshots etc. using dmeventd or progress polling"),
        ("setvtrgb.service", "active", "exited", "Set console scheme"),
        ("snapd.seeded.service", "active", "exited", "Wait until snapd is fully seeded"),
        ("systemd-user-sessions.service", "active", "exited", "Permit User Sessions"),
    ]
    a("[list-unit-files]")
    for name, _act, _sub, _descr in units:
        a(f"{name} enabled enabled")
    a("[status]")  # intentionally empty: parser falls back to [all]
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # --- scheduled jobs (mk_job): both green — no noise ---
    a("<<<job>>>")
    a("==> nightly-report-export <==")
    a(f"start_time {now - 8 * 3600}")
    a("exit_code 0")
    a("real_time 9:14.7")
    a("user_time 6.10")
    a("system_time 2.40")
    a("max_res_kbytes 184000")
    a("avg_mem_kbytes 0")
    a("==> vacuum-analyze-stats <==")
    a(f"start_time {now - 4 * 3600}")
    a("exit_code 0")
    a("real_time 2:31.5")
    a("user_time 1.80")
    a("system_time 0.50")
    a("max_res_kbytes 72000")
    a("avg_mem_kbytes 0")

    # ------------------------------------------------------------------ #
    # mk_postgres plugin sections (instance `main` -> items "MAIN/...").
    # Instance markers are uppercased into service items (lib.py parse_dbs).
    # DB-list sections use [databases_start]/[databases_end] + a header row;
    # sep(59) sections are semicolon-separated. The ONLY check here that
    # alerts by default is postgres_connections (80/90 % of max_connections) —
    # that is the incident lever. Everything else corroborates, never alarms.
    # ------------------------------------------------------------------ #
    db_list = "[databases_start]\npostgres\nanalytics\n[databases_end]"

    # --- instance + version (one service: "PostgreSQL Instance MAIN") ---
    a("<<<postgres_instances>>>")
    a("[[[main]]]")
    a("802 /usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main")

    a("<<<postgres_version:sep(1)>>>")
    a("[[[main]]]")
    a("PostgreSQL 16.3 (Ubuntu 16.3-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, "
      "compiled by gcc (Ubuntu 13.2.0-23ubuntu4) 13.2.0, 64-bit")

    # --- sessions: t = idle, f = running. The leaked backends are idle, so the
    #     idle (t) count climbs with the leak; running (f) stays modest. ps,
    #     pg_stat_activity, numbackends and postgres_connections all agree. ---
    a("<<<postgres_sessions>>>")
    a("[[[main]]]")
    a(f"t {idle_sess}")
    a(f"f {run_sess}")

    # --- pg_stat_database: read replica -> read-only commits, no user writes.
    #     numbackends on `analytics` == total_conns (matches sessions/ps). The
    #     throughput stays roughly flat (the machine is fine) — corroboration. -
    ana = {
        "xact_commit": PG_ANA["xact_commit"].sample(85),
        "xact_rollback": PG_ANA["xact_rollback"].sample(0.3),
        "blks_read": PG_ANA["blks_read"].sample(120),
        "blks_hit": PG_ANA["blks_hit"].sample(16000),
        "tup_returned": PG_ANA["tup_returned"].sample(48000),
        "tup_fetched": PG_ANA["tup_fetched"].sample(12000),
        "tup_inserted": PG_ANA["tup_inserted"].sample(0),
        "tup_updated": PG_ANA["tup_updated"].sample(0),
        "tup_deleted": PG_ANA["tup_deleted"].sample(0),
    }
    ana_size = 96_512_345_678 + int((time.time() - START) * 2048)
    sys_commit = PG_SYS["xact_commit"].sample(0.5)
    sys_hit = PG_SYS["blks_hit"].sample(22)
    sys_ret = PG_SYS["tup_returned"].sample(28)
    a("<<<postgres_stat_database:sep(59)>>>")
    a("[[[main]]]")
    a("datid;datname;numbackends;xact_commit;xact_rollback;blks_read;blks_hit;"
      "tup_returned;tup_fetched;tup_inserted;tup_updated;tup_deleted;datsize")
    a(f"5;postgres;1;{sys_commit};0;208;{sys_hit};{sys_ret};{sys_ret // 2};0;0;0;7421056")
    a(f"16401;analytics;{total_conns};{ana['xact_commit']};{ana['xact_rollback']};"
      f"{ana['blks_read']};{ana['blks_hit']};{ana['tup_returned']};{ana['tup_fetched']};"
      f"{ana['tup_inserted']};{ana['tup_updated']};{ana['tup_deleted']};{ana_size}")

    # --- connections: THE incident lever. mc = MAX_CONNECTIONS (200). The leak
    #     drives `idle` toward mc; the check computes used % per connection
    #     type against mc -> idle % crosses the 80 %/90 % default levels.
    #     header: datname;mc;idle;active (parse_dbs drops the leading datname). -
    a("<<<postgres_connections:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;mc;idle;active")
    a(f"postgres;{MAX_CONNECTIONS};1;0")
    a(f"analytics;{MAX_CONNECTIONS};{idle_sess};{run_sess}")

    # --- query duration: a handful of real analytics queries; the leaked
    #     backends are idle-in-transaction so they don't show a long query. ---
    a("<<<postgres_query_duration:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;datid;usename;client_addr;state;seconds;pid;current_query")
    a(f"analytics;16401;bi_reporter;10.1.4.60;active;{random.randint(2, 14)};2207;"
      "SELECT date_trunc('day', o.created_at) d, sum(o.amount) "
      "FROM orders o GROUP BY 1 ORDER BY 1 DESC LIMIT 90")
    if broken:
        # the leaking BI client's oldest idle-in-transaction backend has been
        # holding its transaction open since the runaway started — grows live
        idle_age = 60 + int(broken_seconds())
        a(f"analytics;16401;bi_reporter;10.1.4.61;idle in transaction;{idle_age};2261;"
          "BEGIN; SELECT * FROM orders WHERE settled = false")
    a("postgres;5;postgres;;active;0;802;SELECT 1")

    # --- locks: read replica -> mostly AccessShareLocks held by the readers ---
    a("<<<postgres_locks:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;granted;mode")
    a("postgres;;")
    for _ in range(2 + run_sess + idle_sess // 8):
        a("analytics;t;AccessShareLock")

    # --- vacuum/analyze recency (green; pure realism) ---
    a("<<<postgres_stats:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;sname;tname;vtime;atime")
    a("postgres;pg_catalog;pg_statistic;-1;-1")
    a(f"analytics;public;orders;{now - 5 * 3600};{now - 5 * 3600}")
    a(f"analytics;public;transactions;{now - 5 * 3600};{now - 5 * 3600}")
    a(f"analytics;public;daily_rollup;{now - 26 * 3600};{now - 26 * 3600}")

    # --- table/index bloat (defaults alert at bloat factor 180/200 %; ours sit
    #     at a healthy 1.1-1.6) ---
    a("<<<postgres_bloat:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("db;schemaname;tablename;tups;pages;otta;tbloat;wastedpages;wastedbytes;"
      "wastedsize;iname;itups;ipages;iotta;ibloat;wastedipages;wastedibytes;"
      "wastedisize;totalwastedbytes")
    a("postgres;pg_catalog;pg_statistic;398;13;10;1.3;3;24576;24 kB;"
      "pg_statistic_relid_att_inh_index;398;6;4;1.5;2;16384;16 kB;40960")
    a("analytics;public;orders;18412022;312480;271722;1.2;40758;333930496;318 MB;"
      "orders_pkey;18412022;91220;70169;1.3;21051;172449792;164 MB;506380288")
    a("analytics;public;transactions;44820110;780122;709201;1.1;70921;580984832;554 MB;"
      "transactions_pkey;44820110;221080;138175;1.6;82905;679157760;648 MB;1260142592")
    a("analytics;public;daily_rollup;1240882;28140;25102;1.1;3038;24887296;24 MB;"
      "daily_rollup_pkey;1240882;7012;5388;1.3;1624;13303808;13 MB;38191104")

    # --- connect time: every new client has to find a free slot — as the leak
    #     hogs them, opening a connection crawls. Creeps up with the pile-up
    #     (corroboration: "even connecting is slow now"). ---
    a("<<<postgres_conn_time>>>")
    a("[[[main]]]")
    # baseline ~12 ms; climbs to ~1.6 s as the leak saturates the pool (backends
    # spend longer competing for the connection-establishment lock / proc slots)
    sat = max(0.0, min(1.0, (total_conns - 90) / float(BROKEN_CAP - 90)))
    conn_t = round(_lerp(0.012, 1.6, sat)
                   * gauge("pg.conn_time", 1.0, amp_frac=0.15,
                           phase=1.5, period=350), 3)
    a(str(conn_t))

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence across restarts (counters/uptime/incident state — see
#  CLAUDE.md: a reset counter goes backwards and stales the rate-based checks).
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-db-postgres-02-state.json")


def save_state() -> None:
    if not STATE_FILE:
        return
    with _state_lock:
        data = {
            "version": 1, "start": START, "state": _state,
            "degraded_since": _degraded_since, "broken_since": _broken_since,
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
    print(f"[state] restored: state={_state!r}, "
          f"{restored}/{len(_ALL_COUNTERS)} counters, uptime continuous")


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
        "color": "#2e7d32", "label": "HEALTHY",
        "tagline": f"All green. ~{HEALTHY_CONNS}/{MAX_CONNECTIONS} connections, "
                   "the replica streaming WAL and serving analytics reads.",
        "effects": [
            "every service OK — the starting picture",
            f"PostgreSQL Connections MAIN/analytics ~{HEALTHY_CONNS}/{MAX_CONNECTIONS} "
            "(well under the 80 % WARN)",
            "Daemon Sessions handful idle, connect time ~12 ms, replica caught up",
        ],
    },
    "degraded": {
        "color": "#f9a825", "label": "DEGRADED",
        "tagline": "A BI/reporting client starts leaking connections. The count climbs "
                   f"90 -> ~{DEGRADED_PEAK}/{MAX_CONNECTIONS} — approaching, but still under "
                   "the 80 % WARN. The breadcrumb. Trigger ~15-20 min before showtime."
                   + (f" Auto-escalates after {AUTO_BREAK_AFTER_MIN:g} min."
                      if AUTO_BREAK_AFTER_MIN > 0 else ""),
        "effects": [
            f"PostgreSQL Connections MAIN/analytics climbs to ~{DEGRADED_PEAK}/"
            f"{MAX_CONNECTIONS} — still OK (under 80 % = {int(MAX_CONNECTIONS * 0.8)})",
            "Daemon Sessions: idle (leaked) backends pile up; connect time starts to creep",
            "numbackends / ps / tcp established all rise together — the breadcrumb trail",
        ],
    },
    "broken": {
        "color": "#c62828", "label": "BROKEN",
        "tagline": "The leak runs away — connections cross 90 % of max_connections and the "
                   "count grows live, capped just under max (new clients can't connect)."
                   + (f" Ramps over ~{BREAK_RAMP_MIN:g} min."
                      if BREAK_RAMP_MIN > 0 else " Instant."),
        "effects": [
            f"PostgreSQL Connections MAIN/analytics CRIT: idle % > 90 % "
            f"(> {int(MAX_CONNECTIONS * 0.9)} of {MAX_CONNECTIONS}; WARN 80 % / CRIT 90 %) "
            "— the headline, and the connection count keeps growing live",
            "Daemon Sessions: many idle-in-transaction backends (the leaked BI sessions)",
            "Connection Time elevated (~1.5 s — new clients wait for a free slot)",
            "disk / SMART / memory / CPU / systemd ALL GREEN — the machine is fine, "
            "the AI fuses connections% near max + idle pile-up + slow connect into "
            "'a client is leaking connections; kill the BI session / add pooling'",
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
    idle_sess, run_sess = connection_counts()
    total_conns = idle_sess + run_sess
    pct = total_conns / MAX_CONNECTIONS * 100
    extras = []
    if degraded_seconds() > 0:
        extras.append(f"connection leak running for {_fmt_duration(degraded_seconds())} — "
                      f"{total_conns}/{MAX_CONNECTIONS} connections "
                      f"({pct:.0f} %; {idle_sess} idle, {run_sess} active)")
    if broken_seconds() > 0:
        crit_at = int(MAX_CONNECTIONS * 0.9)
        if total_conns >= crit_at:
            extras.append(f"Connections MAIN/analytics CRIT — idle {idle_sess} > 90 % "
                          f"({crit_at}); still climbing toward max")
        if break_ramp() < 1.0:
            extras.append(f"runaway ramping: {break_ramp() * 100:.0f} %")
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"leak runs away (auto -> BROKEN) in {_fmt_duration(left)}")
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
        margin:2rem auto; max-width:72rem; padding:0 1rem; }}
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
 <h1>demo control — <b>{HOSTNAME}</b> <span style="color:#555">(PostgreSQL 16 read replica · auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta['label']}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta['tagline']}</div>
 {extra_html}
 <div class="cards">{''.join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "db-replica-demo-ctl/1.0"

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
            return self._send(200, {"state": get_state(),
                                    "in_state_for_s": round(state_since_seconds(), 1),
                                    "action_to_state": ACTION_TO_STATE,
                                    "states": STATE_META})
        if path.startswith("/admin/") and (action := path[len("/admin/"):]) in ACTION_TO_STATE:
            target = ACTION_TO_STATE[action]
            set_state(target)
            print(f"[ctl] -> {target.upper()}")
            if "ui=1" in query:
                self.send_response(303)
                self.send_header("Location", "/admin")
                self.end_headers()
                return None
            return self._send(200, {"state": target})
        idle_sess, run_sess = connection_counts()
        state = get_state()
        auto_break_in = (
            round(max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds()))
            if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0 else None)
        return self._send(200, {
            "state": state,
            "in_state_for_s": round(state_since_seconds(), 1),
            "connections": idle_sess + run_sess,
            "max_connections": MAX_CONNECTIONS,
            "connections_pct": round((idle_sess + run_sess) / MAX_CONNECTIONS * 100, 1),
            "idle_connections": idle_sess,
            "active_connections": run_sess,
            "leaking_for_s": round(degraded_seconds(), 1),
            "auto_break_in_s": auto_break_in,
            "toggles": ["/admin/degrade", "/admin/break", "/admin/heal"],
            "ui": "/admin",
        })


def _auto_break_watchdog() -> None:
    while True:
        time.sleep(5)
        if (get_state() == "degraded"
                and state_since_seconds() >= AUTO_BREAK_AFTER_MIN * 60):
            set_state("broken")
            print(f"[ctl] -> BROKEN (auto: leak ran away after "
                  f"{AUTO_BREAK_AFTER_MIN:g} min)")


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(f"[boot] auto-escalation: degraded -> broken after "
              f"{AUTO_BREAK_AFTER_MIN:g} min in degraded")
    print(f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
          f"start_state={get_state()}  max_connections={MAX_CONNECTIONS}")
    print(f"[boot] control UI:   http://localhost:{HTTP_PORT}/admin")
    print(f"[boot] curl API:     curl localhost:{HTTP_PORT}/admin/degrade|/admin/break|/admin/heal")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
