#!/usr/bin/env python3
"""Meridian Retail demo host: app-redis-01 — the session + cache store.

A Redis 7 server (`redis-server.service`) holding sessions and the hot cache
for the Meridian platform; payment-api and app-worker-01 read/write it. The
incident is a *maxmemory eviction storm*: a bad deploy ships cache keys with
no / huge TTLs, so used_memory climbs to `maxmemory` (~6 GiB), redis starts
evicting under its `allkeys-lru` policy, the eviction rate spikes, the keyspace
hit-ratio collapses, clients block, and command latency rises. The AI fuses
redis used_memory-at-maxmemory + evicted_keys/s + hit-ratio drop + blocked
clients into "a TTL regression flooded the cache; evictions are thrashing it —
fix the key TTLs, don't just raise maxmemory".

The host's OWN Linux memory stays GREEN: redis enforces `maxmemory` and evicts
rather than OOMing the box, so `/proc/meminfo` is calm. The story lives entirely
in the redis *application* metrics (the `<<<redis_info>>>` section), NOT in the
Linux Memory check — see README and CLAUDE.md.

Three states (the timeline is part of the story):

  healthy   used_memory ~40 % of maxmemory (~2.4 GiB of 6), hit ratio ~99 %,
            0 evictions, no blocked clients. All green.
  degraded  the bad deploy lands: used_memory climbs toward maxmemory,
            evicted_keys/s starts rising, hit ratio slips, RDB saves still ok —
            the breadcrumb (graph-visible). Trigger ~18 min before showtime.
  broken    used_memory pinned at maxmemory: heavy evicted_keys/s, blocked
            clients > 0, hit ratio collapsed, and the background RDB save fork
            fails for want of memory -> `rdb_last_bgsave_status:err` -> the
            "Redis ... Persistence" service goes WARN (its default lever). Live-
            growing eviction counter across re-polls.

Plaintext TCP agent (the Checkmk 2.5 fetcher sees `<<` -> TransportProtocol.
PLAIN and accepts it without TLS/registration). Stdlib only.

Config via env (see also AGENT_PORT/HTTP_PORT/START_STATE/STATE_FILE):
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before the storm auto-fires
                 (default: 18; 0 disables)
  LEAK_FILL_MIN  minutes for used_memory to fill to maxmemory while degraded
                 (default: 15; the redis memory graph climbs over this window)
  BREAK_RAMP_MIN minutes for the broken impact (eviction peg, hit-ratio
                 collapse, bgsave failure) to reach full force (default: 3;
                 0 = instant)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "app-redis-01")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "18"))
LEAK_FILL_MIN = float(os.environ.get("LEAK_FILL_MIN", "15"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "3"))

START = time.time()
UPTIME_OFFSET = 11 * 86400  # pretend the host has been up ~11 days

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the bad deploy landed (degraded or broken) -> drives the rising memory.
_degraded_since: float | None = None if _state == "healthy" else START
# when the eviction storm started -> drives eviction peg + bgsave failure.
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


# The single driver of the whole incident: 0 (healthy) .. 1 (memory pinned at
# maxmemory, eviction storm in full force).
#   * the bad deploy fills used_memory over LEAK_FILL_MIN while degraded, but
#     only up to 0.70 — enough that used_memory climbs visibly, evictions just
#     begin and the hit ratio *slips* (~90 %, graph-visible breadcrumb), but the
#     persistence WARN, blocked clients and hit-ratio collapse stay the
#     *broken*-state headline.
#   * broken pegs used_memory at maxmemory and pushes pressure 0.70 -> 1.0 over
#     the break ramp: eviction storm, blocked clients, RDB bgsave failure.
def pressure() -> float:
    ds = degraded_seconds()
    if ds <= 0:
        deg = 0.0
    elif LEAK_FILL_MIN <= 0:
        deg = 1.0
    else:
        deg = min(1.0, ds / (LEAK_FILL_MIN * 60.0))
    p = 0.70 * deg
    if broken_seconds() > 0:
        p = max(p, 0.70 + 0.30 * break_ramp(1.0))
    return max(0.0, min(1.0, p))


# --------------------------------------------------------------------------- #
#  Autocorrelated gauges + monotonic counters (verbatim machinery from the
#  reference hosts; see CLAUDE.md for why a single sine is wrong).
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


# /proc/stat jiffies: 100 Hz * 4 CPUs = ~400 ticks/s. Redis is single-threaded
# for command execution; the eviction storm burns a little more user/system but
# the box never goes CPU-bound — never a CPU alert.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(46))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(22))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(322))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(4))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(4800))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(4))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(0.4))

SDA = {  # single system SSD; RDB snapshots write here. Calm throughout.
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(3)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(2)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(28)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(20)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.05, start=_aged(24)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(2_400_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(3_100_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(9_400))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(9_600))

# ---- the redis story counters: monotonic, state-aware rates ---------------- #
# total_commands_processed: ~8000 ops/s healthy. Under the storm clients block
# on eviction so throughput dips a little (latency rises), but never reverses.
C_CMDS = Counter("redis.total_commands", phase=2.0, start=_aged(8000))
# total_connections_received: a slow accept rate.
C_CONN = Counter("redis.total_connections", phase=2.6, start=_aged(6))
# expired_keys: normal TTL expiry, modest healthy rate; the bad deploy ships
# keys WITHOUT ttl, so expiry barely rises while used_memory climbs.
C_EXPIRED = Counter("redis.expired_keys", phase=3.3, start=_aged(120))
# keyspace_hits / keyspace_misses: ~99 % hit ratio healthy. Under eviction the
# hot set is thrashed out, so misses spike and the *derived* hit ratio drops.
C_HITS = Counter("redis.keyspace_hits", phase=1.2, start=_aged(7600))
C_MISSES = Counter("redis.keyspace_misses", phase=4.4, start=_aged(80))
# evicted_keys: THE star. 0/s healthy (used_memory < maxmemory, nothing to
# evict). Rises in degraded, storms in broken. Strictly monotonic.
C_EVICTED = Counter("redis.evicted_keys", phase=5.0, amp=0.18, start=0.0)
# instantaneous gauge values come from gauge(); only counters persist.


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
        "ata_smart_attributes": {"table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "thresh": 10,
             "raw": {"value": 0}},
            {"id": 12, "name": "Power_Cycle_Count", "value": 100, "thresh": 0,
             "raw": {"value": 17}},
            {"id": 187, "name": "Reported_Uncorrect", "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "thresh": 0,
             "raw": {"value": 0}},
            {"id": 177, "name": "Wear_Leveling_Count", "value": 97, "thresh": 5,
             "raw": {"value": 71}},
        ]},
    }
    return json.dumps(doc, separators=(",", ":"))


def filesystem_usage(now: float) -> tuple[int, int]:
    """root / and /var/lib/redis — both green, growing + cleaned over time."""
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0
    root_base = 11_534_336                                  # ~11 GiB of 40
    root_logs = 1_048_576 * ((now % day) / day)             # 0..1 GiB daily
    root_growth = min(1_572_864, uptime * 0.04)
    root_used = int(root_base + root_logs + root_growth
                    + gauge("fs.root", 0, amp_abs=80_000, period=1500))
    # /var/lib/redis holds the RDB dump (rewritten on bgsave, ~12-min teeth).
    redis_base = 6_291_456                                  # ~6 GiB of 40
    redis_rdb = 1_572_864 * ((now % 720.0) / 720.0)         # dump.rdb, 12-min teeth
    redis_growth = min(2_097_152, uptime * 0.3)
    redis_used = int(redis_base + redis_rdb + redis_growth
                     + gauge("fs.redis", 0, amp_abs=120_000, period=900))
    return root_used, redis_used


# --------------------------------------------------------------------------- #
#  Redis INFO section. <<<redis_info:sep(58)>>> — the mk_redis plugin emits
#  `[[[name|host|port]]]` then raw `redis-cli info` output (colon-separated
#  key:value lines, "# Section" headers). Parser: cmk/plugins/redis/
#  agent_based/redis_base.py.
# --------------------------------------------------------------------------- #
REDIS_MAXMEMORY = 6_442_450_944  # 6 GiB enforced cap (the whole story)
REDIS_INSTANCE = "MERIDIAN_CACHE"


def _redis_derived() -> dict:
    """All the redis values, so /admin and the agent agree."""
    p = pressure()
    # used_memory: 40 % of maxmemory healthy -> pinned at maxmemory broken. The
    # bad deploy never frees, so it ramps with pressure and pegs in broken.
    used_frac = _lerp(0.40, 1.0, p)
    used_memory = int(min(REDIS_MAXMEMORY,
                          REDIS_MAXMEMORY * used_frac
                          * gauge("redis.used", 1.0, amp_frac=0.01, period=900)))
    # hit ratio: ~99 % healthy; only *slips* while the cache still has headroom
    # (degraded ~90 %), then collapses once eviction thrashes the hot set
    # (broken ~62 %). Convex in p so the degraded band stays a gentle breadcrumb.
    hit_ratio = _lerp(0.992, 0.62, p ** 3.0)
    # evicted_keys/s: ~0 until used_memory nears maxmemory (p>~0.5), modest in
    # degraded, storms in broken. Convex so degraded is a trickle, broken a flood.
    evict_rate = 0.0 if p < 0.5 else _lerp(0.0, 5200.0, ((p - 0.5) / 0.5) ** 2.0)
    # blocked clients: 0 until broken; clients block waiting on the LRU evictor.
    blocked = int(round(_lerp(0, 34, break_ramp(1.0)))) if broken_seconds() > 0 else 0
    connected = int(round(gauge("redis.conn", _lerp(118, 196, p),
                                amp_abs=4, phase=0.9, period=700)))
    mem_frag = round(_lerp(1.18, 1.02, p), 2)  # less headroom -> tighter frag
    return {
        "p": p, "used_memory": used_memory, "hit_ratio": hit_ratio,
        "evict_rate": evict_rate, "blocked": blocked, "connected": connected,
        "mem_frag": mem_frag,
    }


# --------------------------------------------------------------------------- #
#  Agent output
# --------------------------------------------------------------------------- #
def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4
    broken = state == "broken"
    p = pressure()
    r = _redis_derived()

    # ---- Linux memory: STAYS GREEN. redis enforces maxmemory (~6 GiB) and
    #      evicts rather than growing without bound, so the box's RAM is calm
    #      throughout — the story is the redis *application* memory, not the
    #      Memory check. used_memory's growth shows as a modest, bounded bump in
    #      AnonPages (redis RSS) that never approaches MemTotal. ------------- #
    mem_total = 16_384_000  # kB
    swap_total = 4_194_300
    commit_limit = swap_total + mem_total // 2
    # redis RSS tracks used_memory (kB) plus copy-on-write during bgsave; the
    # rest of RAM is page cache. As redis' anon working set grows the kernel
    # reclaims page cache to make room (cache is the elastic buffer), so the box
    # NEVER runs out of RAM — MemFree stays comfortably green, swap untouched.
    redis_rss_kb = r["used_memory"] // 1024 + 180_000
    anon = redis_rss_kb + 1_350_000  # redis + the rest of userspace
    # page cache shrinks from ~6.5 GiB toward ~2.4 GiB as redis fills, keeping
    # ~2.7 GiB free at the worst (maxmemory). Reclaimable, so harmless.
    cached = int(gauge("mem.cached", _lerp(6_500_000, 2_400_000, p),
                       amp_frac=0.02, phase=0.4, period=1500))
    buffers = int(gauge("mem.buffers", 240_000, amp_frac=0.03, phase=1.3, period=1700))
    sreclaim = 412_160
    swapcached = 0
    caches = cached + buffers + swapcached + sreclaim
    shmem = 49_152
    mem_free = max(300_000, mem_total - anon - caches - 700_000)
    swap_used_t = 0  # never swaps — redis is capped
    swap_free = swap_total - swap_used_t
    committed = int(gauge("mem.committed", anon + caches + 1_200_000,
                          amp_frac=0.01, phase=1.2, period=1700))

    # anon LRU = AnonPages + Shmem ; file LRU = Buffers + Cached - Shmem
    anon_lru = anon + shmem
    file_lru = max(0, buffers + cached - shmem)
    mem_available = max(mem_free, mem_free + file_lru + sreclaim)
    a_anon = int(anon_lru * 0.62)
    i_anon = anon_lru - a_anon
    a_file = int(file_lru * 0.34)
    i_file = file_lru - a_file
    slab = sreclaim + 118_784
    threads = 240
    kernel_stack = threads * 16
    dirty = max(4_096, int(gauge("mem.dirty", 9_216, amp_frac=0.18,
                                 phase=2.0, period=800)))

    # ---- load: redis is single-threaded; the box is lightly loaded and stays
    #      GREEN throughout (15-min < 20 WARN). The eviction storm bumps it a
    #      hair (LRU sampling) but nowhere near an alert. ------------------- #
    base_l = _lerp(0.55, 2.6, p)
    l1 = round(base_l * gauge("load1", 1.0, amp_frac=0.25, phase=0.2, period=300), 2)
    l5 = round(base_l * 0.94 * gauge("load5", 1.0, amp_frac=0.14, phase=1.0, period=900), 2)
    l15 = round(base_l * 0.88 * gauge("load15", 1.0, amp_frac=0.07, phase=2.0, period=2400), 2)
    runnable = 1 + round(p)
    total_procs = round(_lerp(286, 320, p))

    # ---- /proc/stat: a little more user/system under the storm; never CPU-bound
    user = C_USER.sample(_lerp(46, 92, p))
    system = C_SYSTEM.sample(_lerp(22, 50, p))
    idle = C_IDLE.sample(_lerp(322, 252, p))
    iowait = C_IOWAIT.sample(_lerp(4, 9, p))
    pgmaj_rate = _lerp(0.4, 2.0, p)  # stays tiny — no swap thrash

    # ---- diskstat: single SSD, calm. bgsave writes the dump file. --------- #
    sda_rd = SDA["rd_ios"].sample(_lerp(3, 8, p))
    sda_rdt = SDA["rd_ticks"].sample(_lerp(2, 6, p))
    sda_wr = SDA["wr_ios"].sample(_lerp(28, 70, p))
    sda_wrt = SDA["wr_ticks"].sample(_lerp(20, 60, p))
    sda_iot = SDA["io_ticks"].sample(_lerp(24, 90, p))

    rx_bytes = C_RX_B.sample(_lerp(2_400_000, 2_900_000, p))
    tx_bytes = C_TX_B.sample(_lerp(3_100_000, 2_200_000, p))  # cache misses -> less served
    rx_pkts = C_RX_P.sample(9_400)
    tx_pkts = C_TX_P.sample(9_600)

    # redis counters
    cmds = C_CMDS.sample(_lerp(8000, 6400, p))      # latency rises -> ops dip
    conns = C_CONN.sample(6)
    expired = C_EXPIRED.sample(_lerp(120, 150, p))  # bad keys lack TTL: barely up
    hits = C_HITS.sample(_lerp(7600, 4100, p))
    misses = C_MISSES.sample(_lerp(80, 2600, p))    # eviction -> misses spike
    evicted = C_EVICTED.sample(r["evict_rate"])
    instantaneous_ops = int(round(gauge("redis.iops", _lerp(8000, 6400, p),
                                        amp_frac=0.08, phase=2.2, period=600)))

    sda_temp = round(gauge("smart.sda.temp", 30, amp_abs=1.2, phase=2.1, period=1100))
    sda_smart = _smart_json("/dev/sda", "SAMSUNG MZ7L3480HCHQ-00A07",
                            "S6KSNX0T901244", int(uptime / 3600) + 21000, sda_temp)

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
    cert_to = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                            time.gmtime(now + 331 * 86400))
    a(json.dumps({
        "version": AGENT_VERSION, "agent_socket_operational": True,
        "ip_allowlist": [], "allow_legacy_pull": False,
        "connections": [{
            "site_id": "monitoring/prod", "receiver_port": 8000,
            "uuid": "c47a9e21-3b58-4d10-8e6f-1a2b3c4d5e6f",
            "local": {"connection_mode": "pull-agent", "cert_info": {
                "issuer": "Site 'prod' local CA",
                "from": "Mon, 02 Jun 2025 07:41:09 +0000", "to": cert_to}},
            "remote": "remote_query_disabled"}]}, separators=(",", ":")))
    a("<<<checkmk_agent_plugins_lnx:sep(0)>>>")
    a("pluginsdir /opt/checkmk/agent/default/package/plugins")
    a("localdir /opt/checkmk/agent/default/package/local")
    a('/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="%s"'
      % AGENT_VERSION)
    a('/opt/checkmk/agent/default/package/plugins/0/mk_redis:CMK_VERSION="%s"'
      % AGENT_VERSION)

    a("<<<df_v2>>>")
    root_size = 41_943_040
    redis_size = 41_943_040
    root_used, redis_used = filesystem_usage(time.time())
    a(f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
      f"{round(root_used / root_size * 100)}% /")
    a(f"/dev/sda2 ext4 {redis_size} {redis_used} {redis_size - redis_used} "
      f"{round(redis_used / redis_size * 100)}% /var/lib/redis")
    a("[df_inodes_start]")
    a(f"/dev/sda1 ext4 2621440 268914 {2621440 - 268914} 11% /")
    a(f"/dev/sda2 ext4 2621440 38 {2621440 - 38} 1% /var/lib/redis")
    a("[df_inodes_end]")

    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sda2 /var/lib/redis ext4 rw,noatime 0 0")

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
    a("SUnreclaim:     118784 kB")
    a(f"KernelStack:    {kernel_stack} kB")
    a("PageTables:     46080 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    48128 kB")
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
    a("DirectMap4k:    253900 kB")
    a("DirectMap2M:    6035456 kB")
    a("DirectMap1G:    11534336 kB")

    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {31000 + C_PROC.sample(4) % 9999} {ncpu}")

    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.6)}.00")

    last_sync = now - 488
    sync_str = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(last_sync))
    offset_us = random.randint(-1600, 1600)
    a("<<<timesyncd>>>")
    a("       Server: 185.125.190.57 (ntp.ubuntu.com)")
    a("Poll interval: 34min 8s (min: 32s; max 34min 8s)")
    a("         Leap: normal")
    a("      Version: 4")
    a("      Stratum: 2")
    a("    Reference: C2A85B11")
    a("    Precision: 1us (-25)")
    a("Root distance: 11.482ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 17.221ms")
    a(f"       Jitter: {random.randint(700, 2900) / 1000:.3f}ms")
    a(f" Packet count: {712 + int((time.time() - START) / 2048)}")
    a("    Frequency: -4.227ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a("NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
      "RootDelay=8.118ms, RootDispersion=1.007ms, Reference=C2A85B11, "
      f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
      f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
      "Ignored=no, PacketCount=61, Jitter=0.984ms }")
    a("Timezone=UTC")

    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(4800)}")
    a(f"processes {C_PROC.sample(4)}")
    a(f"pgmajfault {C_PGMAJ.sample(pgmaj_rate)}")

    a("<<<diskstat>>>")
    a(str(now))
    a(f"8 0 sda {sda_rd} 0 {sda_rd * 24} {sda_rdt} {sda_wr} 0 "
      f"{sda_wr * 40} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0")

    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN "
      "group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
      "state UP group default qlen 1000")
    a("    link/ether 02:42:ac:11:00:2c brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:2c")

    a("<<<tcp_conn_stats>>>")
    a(f"01 {round(gauge('tcp.estab', r['connected'], amp_abs=4, phase=0.9, period=700))}")
    a(f"02 {random.randint(0, 1)}")
    a(f"06 {round(gauge('tcp.timewait', 12, amp_abs=3, phase=2.4, period=500))}")
    a("0A 4")

    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)

    # ---- the redis INFO section: the whole story ------------------------- #
    bgsave_ok = not (broken and break_ramp(1.0) > 0.5)
    rdb_last_save = now - (now % 720) - (0 if bgsave_ok else 720)
    rdb_changes = int(gauge("redis.rdbchanges", _lerp(2400, 18000, p),
                            amp_frac=0.1, phase=3.1, period=600))
    a("<<<redis_info:sep(58)>>>")
    a(f"[[[{REDIS_INSTANCE}|127.0.0.1|6379]]]")
    a("# Server")
    a("redis_version:7.0.15")
    a("redis_git_sha1:00000000")
    a("redis_git_dirty:0")
    a("redis_build_id:b3f8c2a9d4e15067")
    a("redis_mode:standalone")
    a("os:Linux 6.8.0-48-generic x86_64")
    a("arch_bits:64")
    a("multiplexing_api:epoll")
    a("atomicvar_api:c11-builtin")
    a("gcc_version:13.2.0")
    a("process_id:1142")
    a("run_id:9f3c1d7b2a8e6405f1c9b3a7d2e84f60c5b1a9d3")
    a("tcp_port:6379")
    a(f"uptime_in_seconds:{uptime}")
    a(f"uptime_in_days:{uptime // 86400}")
    a("hz:10")
    a(f"lru_clock:{now % 16777216}")
    a("executable:/usr/bin/redis-server")
    a("config_file:/etc/redis/redis.conf")
    a("# Clients")
    a(f"connected_clients:{r['connected']}")
    a("cluster_connections:0")
    a("maxclients:10000")
    a("client_recent_max_input_buffer:20480")
    a("client_recent_max_output_buffer:0")
    a(f"blocked_clients:{r['blocked']}")
    a("tracking_clients:0")
    a("clients_in_timeout_table:0")
    a("# Memory")
    a(f"used_memory:{r['used_memory']}")
    a(f"used_memory_human:{r['used_memory'] / 1024 / 1024 / 1024:.2f}G")
    a(f"used_memory_rss:{int(r['used_memory'] * 1.04)}")
    a(f"used_memory_peak:{max(r['used_memory'], int(REDIS_MAXMEMORY * 0.42))}")
    a(f"used_memory_lua:{37888}")
    a(f"maxmemory:{REDIS_MAXMEMORY}")
    a(f"maxmemory_human:{REDIS_MAXMEMORY / 1024 / 1024 / 1024:.2f}G")
    a("maxmemory_policy:allkeys-lru")
    a(f"mem_fragmentation_ratio:{r['mem_frag']}")
    a("mem_allocator:jemalloc-5.3.0")
    a("# Persistence")
    a("loading:0")
    a(f"rdb_changes_since_last_save:{rdb_changes}")
    a("rdb_bgsave_in_progress:0")
    a(f"rdb_last_save_time:{rdb_last_save}")
    a(f"rdb_last_bgsave_status:{'ok' if bgsave_ok else 'err'}")
    a(f"rdb_last_bgsave_time_sec:{1 if bgsave_ok else -1}")
    a("rdb_current_bgsave_time_sec:-1")
    a("rdb_last_cow_size:8388608")
    a("aof_enabled:0")
    a("aof_rewrite_in_progress:0")
    a("aof_rewrite_scheduled:0")
    a("aof_last_rewrite_time_sec:-1")
    a("aof_current_rewrite_time_sec:-1")
    a("aof_last_bgrewrite_status:ok")
    a("aof_last_write_status:ok")
    a("aof_last_cow_size:0")
    a("# Stats")
    a(f"total_connections_received:{conns}")
    a(f"total_commands_processed:{cmds}")
    a(f"instantaneous_ops_per_sec:{instantaneous_ops}")
    a(f"total_net_input_bytes:{rx_bytes}")
    a(f"total_net_output_bytes:{tx_bytes}")
    a(f"expired_keys:{expired}")
    a(f"evicted_keys:{evicted}")
    a(f"keyspace_hits:{hits}")
    a(f"keyspace_misses:{misses}")
    a("pubsub_channels:6")
    a("pubsub_patterns:0")
    a(f"instantaneous_input_kbps:{gauge('redis.inkbps', 480, amp_frac=0.12, period=600):.2f}")
    a(f"instantaneous_output_kbps:{gauge('redis.outkbps', 1240, amp_frac=0.12, period=600):.2f}")
    a("rejected_connections:0")
    a("# Replication")
    a("role:master")
    a("connected_slaves:1")
    a("master_failover_state:no-failover")
    a("# CPU")
    a(f"used_cpu_sys:{system / 100:.2f}")
    a(f"used_cpu_user:{user / 100:.2f}")
    a("# Keyspace")
    a(f"db0:keys={int(_lerp(420000, 1180000, p))},expires={int(_lerp(390000, 410000, p))},avg_ttl={int(_lerp(540000, 90, p))}")

    # ---- processes: the redis server + daemons ---------------------------- #
    redis_vsz = redis_rss_kb + 220_000
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, usr, vsz, rss, cputime, pid, cmd in (
            ("init.scope", "root", 168_000, 12_400, "00:00:28", 1, "/sbin/init"),
            ("system.slice/systemd-journald.service", "root", 56_700, 18_900,
             "00:01:11", 398, "/usr/lib/systemd/systemd-journald"),
            ("system.slice/systemd-udevd.service", "root", 25_400, 7_700,
             "00:00:02", 431, "/usr/lib/systemd/systemd-udevd"),
            ("system.slice/systemd-resolved.service", "systemd-resolve", 26_300, 12_900,
             "00:00:39", 482, "/usr/lib/systemd/systemd-resolved"),
            ("system.slice/systemd-timesyncd.service", "systemd-timesync", 91_000, 7_500,
             "00:00:09", 497, "/usr/lib/systemd/systemd-timesyncd"),
            ("system.slice/dbus.service", "messagebus", 10_100, 5_000,
             "00:00:15", 509, "@dbus-daemon --system --address=systemd:"),
            ("system.slice/rsyslog.service", "syslog", 222_400, 6_500,
             "00:00:33", 604, "/usr/sbin/rsyslogd -n -iNONE"),
            ("system.slice/ssh.service", "root", 15_400, 9_000,
             "00:00:01", 681, "sshd: /usr/sbin/sshd -D [listener]"),
            ("system.slice/cron.service", "root", 11_500, 2_400,
             "00:00:02", 695, "/usr/sbin/cron -f -P"),
            ("system.slice/containerd.service", "root", 1_810_000, 39_800,
             "01:31:02", 742, "/usr/bin/containerd"),
    ):
        a(f"0::/{cgs} {usr} {vsz} {rss} {cputime} 11-04:18:22 {pid} {cmd}")
    # the redis server (RSS tracks used_memory)
    a(f"0::/system.slice/redis-server.service redis {redis_vsz} {redis_rss_kb} "
      "13:42:09 11-04:17:55 1142 "
      "/usr/bin/redis-server 127.0.0.1:6379")

    # ---- systemd units: ~30, all green (redis-server stays running — the
    #      eviction storm does NOT kill the process; the story is application
    #      memory, not OOM). The persistence WARN is a redis check, not systemd.
    a("<<<systemd_units>>>")
    units = [
        ("redis-server.service", "active", "running",
         "Advanced key-value store"),
        ("ssh.service", "active", "running", "OpenBSD Secure Shell server"),
        ("cron.service", "active", "running",
         "Regular background program processing daemon"),
        ("containerd.service", "active", "running", "containerd container runtime"),
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
    a("[status]")
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # ---- scheduled job: the nightly RDB-to-S3 offload (green) ------------- #
    a("<<<job>>>")
    a("==> rdb-offload <==")
    a(f"start_time {now - 8 * 3600}")
    a("exit_code 0")
    a("real_time 0:22.4")
    a("user_time 0.40")
    a("system_time 0.90")
    a("max_res_kbytes 41000")
    a("avg_mem_kbytes 0")

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence (counters/uptime/incident — see CLAUDE.md)
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-app-redis-state.json")


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
        "tagline": "All green. used_memory ~40 % of the 6 GiB maxmemory, hit ratio ~99 %, "
                   "0 evictions, no blocked clients.",
        "effects": [
            "every service OK — the starting picture",
            "Redis Memory graph: used_memory ~2.4 GiB of 6 GiB maxmemory (allkeys-lru)",
            "keyspace hit ratio ~99 %, evicted_keys/s = 0, blocked_clients = 0",
            "Redis Persistence OK (last RDB bgsave successful)",
        ],
    },
    "degraded": {
        "color": "#f9a825", "label": "DEGRADED",
        "tagline": "A bad deploy ships cache keys with no/huge TTL. used_memory climbs toward "
                   "maxmemory, evictions begin, hit ratio slips — the breadcrumb (graph-visible)."
                   + (f" Auto-escalates after {AUTO_BREAK_AFTER_MIN:g} min."
                      if AUTO_BREAK_AFTER_MIN > 0 else ""),
        "effects": [
            "Redis Memory graph: used_memory ramps toward the 6 GiB maxmemory cap",
            "evicted_keys/s rises off zero once used_memory nears the cap; hit ratio slips "
            "from ~99 % — graph-visible breadcrumb (no default level → set a rule to alert)",
            "RDB saves still OK, no blocked clients; redis-server still active/running",
            "the host's Linux Memory stays GREEN — redis evicts, it doesn't OOM the box",
        ],
    },
    "broken": {
        "color": "#c62828", "label": "BROKEN",
        "tagline": "used_memory pinned at maxmemory — eviction storm. "
                   + (f"Ramps over ~{BREAK_RAMP_MIN:g} min."
                      if BREAK_RAMP_MIN > 0 else "Instant."),
        "effects": [
            "Redis Persistence WARN: the bgsave fork fails for want of memory → "
            "rdb_last_bgsave_status:err (the redis check's DEFAULT lever) — the alert",
            "evicted_keys storms (thousands/s, counter climbs live); hit ratio collapses "
            "to ~60 %; blocked_clients > 0 — graph-visible, the AI fuses them",
            "used_memory == maxmemory (6 GiB); command latency up, ops/s dip",
            "Linux Memory / Swap / CPU all GREEN — the box is fine; the AI concludes "
            "'a TTL regression flooded the cache; evictions are thrashing it — fix the "
            "key TTLs, don't just raise maxmemory'",
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
    r = _redis_derived()
    extras = []
    if degraded_seconds() > 0:
        extras.append(f"bad deploy live for {_fmt_duration(degraded_seconds())} — "
                      f"used_memory {r['used_memory'] / 1024 / 1024 / 1024:.2f} / "
                      f"{REDIS_MAXMEMORY / 1024 / 1024 / 1024:.0f} GiB "
                      f"({r['p'] * 100:.0f} % of maxmemory)")
        extras.append(f"hit ratio {r['hit_ratio'] * 100:.1f} %, "
                      f"evicting ~{r['evict_rate']:.0f} keys/s, "
                      f"blocked clients {r['blocked']}")
    if broken_seconds() > 0:
        extras.append(f"eviction storm for {_fmt_duration(broken_seconds())} — "
                      "RDB bgsave failing (Persistence WARN)")
        if break_ramp() < 1.0:
            extras.append(f"storm ramping: {break_ramp() * 100:.0f} %")
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"eviction storm auto-fires in {_fmt_duration(left)}")
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
 <h1>demo control — <b>{HOSTNAME}</b> <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta['label']}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta['tagline']}</div>
 {extra_html}
 <div class="cards">{''.join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "redis-demo-ctl/1.0"

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
        state = get_state()
        r = _redis_derived()
        auto_break_in = (
            round(max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds()))
            if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0 else None)
        return self._send(200, {
            "state": state,
            "in_state_for_s": round(state_since_seconds(), 1),
            "used_memory_pct_of_maxmemory": round(r["p"] * 100, 1),
            "used_memory_bytes": r["used_memory"],
            "maxmemory_bytes": REDIS_MAXMEMORY,
            "hit_ratio_pct": round(r["hit_ratio"] * 100, 1),
            "evicted_keys_per_s": round(r["evict_rate"], 1),
            "blocked_clients": r["blocked"],
            "deploy_live_for_s": round(degraded_seconds(), 1),
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
            print(f"[ctl] -> BROKEN (auto: eviction storm after {AUTO_BREAK_AFTER_MIN:g} min)")


def main() -> None:
    load_state()
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(f"[boot] auto-escalation: degraded -> broken (eviction storm) after "
              f"{AUTO_BREAK_AFTER_MIN:g} min")
    print(f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
          f"start_state={get_state()}")
    print(f"[boot] control UI:   http://localhost:{HTTP_PORT}/admin")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
