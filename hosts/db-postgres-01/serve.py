#!/usr/bin/env python3
"""Conf #12 demo: fake database host for the Theme 2 "Explain with AI" story.

One of Anna's PostgreSQL servers (`db-postgres-01`). The trap from the keynote:
a **CPU load CRITICAL** page that is really a **dying data disk** — compute is
nearly idle, processes are stacked up in I/O wait, the Disk IO read latency is
~200 ms, and a SMART disk-health alarm fired *before* the load alert.
"Explain with AI" fuses those services into the real root cause.

Three states (not two — the timeline is part of the story):

  healthy   everything green. **Discover the host in THIS state** — the SMART
            check snapshots raw attribute values at discovery time and goes
            CRIT only when they later *exceed* that baseline.
  degraded  the data disk (/dev/sdb) starts dying: SMART Stats CRIT
            (pending/reallocated sectors above the discovered baseline, and
            slowly rising), disk temperature WARN. Performance still fine.
            -> trigger this ~20 min before showtime: it puts the disk-health
            event *earlier* in the event history than the load page.
  broken    degraded + the performance impact: CPU load CRIT (15-min load ~44
            on 4 cores; default levels are 5/10 per core => CRIT above 40),
            CPU utilization pinned on I/O wait (~80 %), Disk IO read latency
            ~200 ms on sdb at ~99 % utilization, postgres backends piling up.

Like the payment-api demo, the agent speaks *plaintext* on TCP: the Checkmk
2.5 fetcher reads the first two bytes (`<<` of `<<<check_mk>>>`), recognises
TransportProtocol.PLAIN and accepts it — no TLS, no registration.
(See check_mk:packages/cmk-check-engine/cmk/fetchers/_tcp.py.)

Stdlib only -> the container is plain python:slim, no pip install.

Config via env:
  CMK_HOSTNAME   host name baked into the agent output
                 (default: db-postgres-01.corp.meridian-retail.com)
  AGENT_PORT     TCP port for the agent                  (default: 6556)
  HTTP_PORT      TCP port for the admin/toggle endpoint  (default: 8080)
  START_STATE    healthy | degraded | broken             (default: healthy)
  AGENT_VERSION  version string in the <<<check_mk>>> hdr (default: 2.5.0-2026.04.03)
  AUTO_BREAK_AFTER_MIN  minutes in `degraded` before auto-escalating to
                 `broken` (default: 20; 0 disables the escalation)
  BREAK_RAMP_MIN  minutes for the broken-state impact to ramp to full force
                 (default: 4; 0 = instant spike). The disk goes bad fastest,
                 iowait follows, loadavg lags — the CPU-load CRIT lands at
                 ~90 % of the ramp (~3.6 min with the default).
  SMART_CONFESSION_MIN  minutes into `broken` before the SSD finally admits
                 failure in its SMART attributes (default: 8; the fail-slow
                 story: SMART stays green while latency explodes, the AI
                 diagnoses the disk from cross-signals, SMART confirms later)
  STATE_FILE     persistence file for counters/uptime/incident state, so a
                 restart never resets counters (which would mark Checkmk's
                 rate-based services stale) or the running incident
                 (default: /var/tmp/cmk-demo-dying-disk-state.json; "" = off)
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

HOSTNAME = os.environ.get("CMK_HOSTNAME", "db-postgres-01.corp.meridian-retail.com")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "6556"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
AGENT_VERSION = os.environ.get("AGENT_VERSION", "2.5.0-2026.04.03")
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "20"))
BREAK_RAMP_MIN = float(os.environ.get("BREAK_RAMP_MIN", "4"))
SMART_CONFESSION_MIN = float(os.environ.get("SMART_CONFESSION_MIN", "8"))

START = time.time()
UPTIME_OFFSET = 12 * 86400  # pretend the host has been up ~12 days

STATES = ("healthy", "degraded", "broken")

_state_lock = threading.Lock()
_state = os.environ.get("START_STATE", "healthy")
if _state not in STATES:
    _state = "healthy"
# when the disk started dying (degraded or broken); drives the slowly-rising
# pending-sector count so re-polls during the talk show the disk getting worse
_degraded_since: float | None = None if _state == "healthy" else START
# when the full incident started; drives the live-growing "longest query"
_broken_since: float | None = None if _state != "broken" else START
# when the *current* state was entered (for the /admin screen)
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


def degraded_minutes() -> float:
    with _state_lock:
        return 0.0 if _degraded_since is None else (time.time() - _degraded_since) / 60.0


def broken_seconds() -> float:
    with _state_lock:
        return 0.0 if _broken_since is None else time.time() - _broken_since


def smart_attrs() -> tuple[int, int, int, float]:
    """Fail-slow SMART story — returns (pending, realloc, uncorrect, temp °C).

    The point of the demo is that the root cause must NOT be served on a
    plate: this SSD fails the *fail-slow* way (firmware read-retry/ECC storms
    destroy latency while SMART still reports a clean bill — SMART attributes
    only count completed failures, not retry storms). So:

      * degraded/broken: the attribute counters stay ZERO — the SMART Stats
        service stays green through the entire incident. The only breadcrumb
        is the temperature creeping up (the controller burns power on
        retries): 33 → 38 °C, crossing the 35 °C WARN at ~2.5 min — easy
        for a human to blame on the rack AC.
      * SMART_CONFESSION_MIN minutes into `broken`, the drive finally admits
        it: the attributes cascade causally (pending sectors first,
        uncorrectable from ~5 min, reallocated from ~10 min — remaps even
        consume a few pending), *vindicating* the AI's earlier diagnosis.

    Real drives don't flip every counter in the same minute, hence the
    staggered cascade rather than one synchronized cliff.
    """
    m = degraded_minutes()
    if m <= 0:
        return 0, 0, 0, 30
    # retry storms heat the controller fast: smooth ramp 33 -> 38 over ~6 min,
    # crossing the 35 WARN at ~2.5 min. Capped at 38 so the +/-1.3 gauge wander
    # never reaches the 40 CRIT (fail-slow: temp is a WARN breadcrumb, the
    # SMART attributes are the only thing that ever goes red — and only late).
    temp = 33.0 + min(5.0, m * 0.85)
    c = broken_seconds() / 60.0 - SMART_CONFESSION_MIN  # cascade age (min)
    if c <= 0:
        return 0, 0, 0, temp  # the drive isn't telling
    flagged = 4 + int(c / 3)  # weak sectors admitted so far
    uncorrect = 0 if c < 5 else min(6, 1 + int((c - 5) / 7))
    realloc = 0 if c < 10 else min(12, int((c - 10) * 1.5))
    pending = max(2, min(24, flagged - realloc // 3))  # net: remaps eat a few
    return pending, realloc, uncorrect, temp


def filesystem_usage(now: float) -> tuple[int, int]:
    """Realistic used-space (kB) for / and /var/lib/postgresql over time.

    A static usage line is a dead giveaway. Real volumes show secular GROWTH
    with periodic CLEANUP sawteeth. All terms are pure functions of wall-clock
    `now` (+ persisted START), so the curve is continuous across re-polls and
    restarts:

      * root /: system logs creep up, then journald/logrotate trims them
        daily (a ~1 GiB sawtooth) on a slow ~2 GiB secular base.
      * data /var/lib/postgresql: three superimposed motions a DBA expects —
          - WAL grows between checkpoints and is recycled every ~12 min
            (checkpoint_timeout-ish) -> a ~1.5 GiB sawtooth;
          - base-backup retention purge once/day -> an ~8 GiB sawtooth;
          - the tables themselves grow slowly and forever (~2 kB/s, matching
            the pay_size datsize growth) -> a gentle upward trend.
        It stays ~27-30 % full (well under the 80/90 % df defaults) — green
        corroboration, never an alert.
    """
    uptime = now - START + UPTIME_OFFSET
    day = 86_400.0

    # root: 14.5 GiB base + slow log growth, daily logrotate cleanup
    root_base = 15_204_352  # ~14.5 GiB of 40
    root_logs = 1_258_291 * ((now % day) / day)  # 0..1.2 GiB sawtooth
    root_growth = min(2_097_152, uptime * 0.05)  # capped ~2 GiB
    root_used = int(
        root_base + root_logs + root_growth + gauge("fs.root", 0, amp_abs=120_000, period=1500)
    )

    # data: ~118 GiB base + WAL sawtooth + daily backup sawtooth + DB growth
    data_base = 123_731_968  # ~118 GiB
    wal = 1_572_864 * ((now % 720.0) / 720.0)  # 0..1.5 GiB, 12-min teeth
    backup = 8_388_608 * ((now % day) / day)  # 0..8 GiB daily
    db_growth = min(6_291_456, uptime * 2.0)  # ~2 kB/s, capped ~6 GiB
    data_used = int(
        data_base + wal + backup + db_growth + gauge("fs.data", 0, amp_abs=300_000, period=900)
    )
    return root_used, data_used


def break_ramp(frac: float = 1.0) -> float:
    """0 → 1 since the break started; hits 1 after frac * BREAK_RAMP_MIN minutes.

    The broken state must not be a vertical cliff in every graph — real
    incidents build. Different signals get different time constants via
    `frac`: the disk (the cause) goes bad fastest, iowait follows, and the
    loadavgs lag behind it in 1-min < 5-min < 15-min order, exactly like the
    kernel's exponentially-smoothed averages would. With the default 4-min
    ramp the 15-min load crosses the WARN level (20) at ~1.8 min and the CRIT
    level (40) at ~3.6 min after the break.
    """
    bs = broken_seconds()
    if bs <= 0:
        return 0.0
    if BREAK_RAMP_MIN <= 0:
        return 1.0
    return min(1.0, bs / (BREAK_RAMP_MIN * 60.0 * frac))


def _lerp(healthy: float, broken: float, r: float) -> float:
    return healthy + (broken - healthy) * r


# --------------------------------------------------------------------------- #
#  Monotonic, state-aware counters
# --------------------------------------------------------------------------- #
_ALL_COUNTERS: dict[str, Counter] = {}


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
        harm = (
            0.60 * math.sin(self.omega * now + self.phase)
            + 0.28 * math.sin(self.omega * 2.7 * now + self.phase * 1.7)
            + 0.18 * math.sin(self.omega * 0.41 * now + self.phase * 0.5)
        )
        # mean-reverting, bounded -> irregular but smooth (no white-noise jitter)
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


# /proc/stat is in jiffies: 100 Hz * 4 CPUs = ~400 ticks/s total.
# healthy: mostly idle. broken: idle collapses into iowait — the whole point.
C_USER = Counter("cpu.user", phase=0.3, start=_aged(60))
C_SYSTEM = Counter("cpu.system", phase=1.1, start=_aged(25))
C_IDLE = Counter("cpu.idle", phase=2.4, start=_aged(300))
C_IOWAIT = Counter("cpu.iowait", phase=3.0, start=_aged(12))
C_CTXT = Counter("kernel.ctxt", phase=4.0, start=_aged(2800))
C_PROC = Counter("kernel.processes", phase=4.7, start=_aged(7))
C_PGMAJ = Counter("kernel.pgmajfault", phase=5.4, start=_aged(1))

# diskstat counters per device: rd_ios, rd_ticks(ms), wr_ios, wr_ticks(ms),
# io_ticks(ms, drives the utilization %).
# io_ticks gets a near-flat amplitude: at 990 ms/s (the broken ~99 % util) a
# +/-30 % swing would exceed 1000 ms/s and render as an impossible >100 %.
SDA = {  # system disk: always calm
    "rd_ios": Counter("sda.rd_ios", phase=0.0, start=_aged(4)),
    "rd_ticks": Counter("sda.rd_ticks", phase=0.2, start=_aged(3)),
    "wr_ios": Counter("sda.wr_ios", phase=0.4, start=_aged(8)),
    "wr_ticks": Counter("sda.wr_ticks", phase=0.6, start=_aged(7)),
    "io_ticks": Counter("sda.io_ticks", phase=0.8, amp=0.01, start=_aged(10)),
}
SDB = {  # data SSD (/var/lib/postgresql): the one that dies.
    # Healthy numbers are SATA-datacenter-SSD honest: reads ~0.4 ms, WAL
    # flushes ~120/s at ~0.5 ms (sequential), utilization ~8 %.
    "rd_ios": Counter("sdb.rd_ios", phase=1.0, start=_aged(45)),
    "rd_ticks": Counter("sdb.rd_ticks", phase=1.2, start=_aged(18)),
    "wr_ios": Counter("sdb.wr_ios", phase=1.4, start=_aged(120)),
    "wr_ticks": Counter("sdb.wr_ticks", phase=1.6, start=_aged(60)),
    "io_ticks": Counter("sdb.io_ticks", phase=1.8, amp=0.01, start=_aged(75)),
}

C_RX_B = Counter("net.rx_bytes", phase=1.6, start=_aged(450_000))
C_TX_B = Counter("net.tx_bytes", phase=2.3, start=_aged(380_000))
C_RX_P = Counter("net.rx_pkts", phase=3.0, start=_aged(1400))
C_TX_P = Counter("net.tx_pkts", phase=3.7, start=_aged(1300))

# pg_stat_database counters for the `payments` DB (the workload). When the disk
# dies, throughput collapses — commits/s drop ~10x while the host's *CPU* is
# idle: yet another arrow pointing at I/O, not compute.
PG_PAY = {
    "xact_commit": Counter("pg.payments.xact_commit", phase=0.5, start=_aged(380)),
    "xact_rollback": Counter("pg.payments.xact_rollback", phase=0.9, start=_aged(0.2)),
    "blks_read": Counter("pg.payments.blks_read", phase=1.3, start=_aged(40)),
    "blks_hit": Counter("pg.payments.blks_hit", phase=1.7, start=_aged(9000)),
    "tup_returned": Counter("pg.payments.tup_returned", phase=2.1, start=_aged(14000)),
    "tup_fetched": Counter("pg.payments.tup_fetched", phase=2.5, start=_aged(5000)),
    "tup_inserted": Counter("pg.payments.tup_inserted", phase=2.9, start=_aged(120)),
    "tup_updated": Counter("pg.payments.tup_updated", phase=3.3, start=_aged(90)),
    "tup_deleted": Counter("pg.payments.tup_deleted", phase=3.7, start=_aged(2)),
}
# the near-idle `postgres` maintenance DB (needs xact_commit > 0 to discover)
PG_SYS = {
    "xact_commit": Counter("pg.postgres.xact_commit", phase=0.4, start=_aged(0.5)),
    "blks_hit": Counter("pg.postgres.blks_hit", phase=1.0, start=_aged(25)),
    "tup_returned": Counter("pg.postgres.tup_returned", phase=1.4, start=_aged(30)),
}


# --------------------------------------------------------------------------- #
#  SMART data (smart_posix_all: one `smartctl --all --json` document per line)
# --------------------------------------------------------------------------- #
def _smart_json(
    name: str,
    model: str,
    serial: str,
    hours: int,
    temp: int,
    realloc: int,
    uncorrect: int,
    pending: int,
    crc: int,
    healthy: bool,
) -> str:
    # Only device/model_name/serial_number/ata_smart_attributes/temperature/
    # power_on_time are read by the parser (cmk/plugins/smart/agent_based/
    # smart_posix.py); smart_status is cosmetic but realistic.
    doc = {
        "device": {"name": name, "type": "sat", "protocol": "ATA"},
        "model_name": model,
        "serial_number": serial,
        "smart_status": {"passed": healthy},
        "power_on_time": {"hours": hours},
        "temperature": {"current": temp},
        "ata_smart_attributes": {
            "table": [
                {
                    "id": 5,
                    "name": "Reallocated_Sector_Ct",
                    "value": 100 if realloc == 0 else 81,
                    "thresh": 10,
                    "raw": {"value": realloc},
                },
                {
                    "id": 12,
                    "name": "Power_Cycle_Count",
                    "value": 100,
                    "thresh": 0,
                    "raw": {"value": 41},
                },
                {
                    "id": 187,
                    "name": "Reported_Uncorrect",
                    "value": 100 if uncorrect == 0 else 97,
                    "thresh": 0,
                    "raw": {"value": uncorrect},
                },
                {
                    "id": 197,
                    "name": "Current_Pending_Sector",
                    "value": 100 if pending == 0 else 92,
                    "thresh": 0,
                    "raw": {"value": pending},
                },
                {
                    "id": 199,
                    "name": "UDMA_CRC_Error_Count",
                    "value": 200,
                    "thresh": 0,
                    "raw": {"value": crc},
                },
                # SSD wear attributes (not evaluated by the Checkmk ATA check,
                # but a smartctl-literate viewer expects them on an SSD)
                {
                    "id": 177,
                    "name": "Wear_Leveling_Count",
                    "value": 93 if realloc == 0 else 91,
                    "thresh": 5,
                    "raw": {"value": 142 if realloc == 0 else 178},
                },
                {
                    "id": 179,
                    "name": "Used_Rsvd_Blk_Cnt_Tot",
                    "value": 100 if realloc == 0 else 96,
                    "thresh": 10,
                    "raw": {"value": realloc * 2},
                },
            ]
        },
    }
    return json.dumps(doc, separators=(",", ":"))


# --------------------------------------------------------------------------- #
#  Agent output generation
# --------------------------------------------------------------------------- #
def _kb(mib: float) -> int:
    return int(mib * 1024)


def build_agent_output(state: str) -> bytes:
    now = int(time.time())
    uptime = int(time.time() - START) + UPTIME_OFFSET
    ncpu = 4
    broken = state == "broken"
    disk_dying = state in ("degraded", "broken")

    # NOTE: the design rule from the storyline — ONE coherent root cause, low
    # noise. `degraded` flips ONLY the SMART health of /dev/sdb. `broken` adds
    # the performance impact (load, iowait, disk latency, backend pile-up).
    # Memory, filesystems, network, systemd units, jobs stay green throughout:
    # the trap is that the *service* (postgres) is up and the host "looks fine"
    # — until you read the right two services together.

    # ---- memory: a healthy 16 GiB DB server. The *usage* stays green in
    #      every state (it's not a memory problem!), but the dirty-page
    #      plumbing is honest: while the disk can't drain writes, Dirty piles
    #      up (~1.5 MB/s, live-growing) and Writeback sits nonzero — visible
    #      in the Memory service's dirty/writeback metrics, another arrow
    #      pointing at storage. ---------------------------------------------- #
    mem_total = 16_384_000  # kB
    swap_total = 4_194_300
    # real kernel default: CommitLimit = SwapTotal + 50 % of RAM
    commit_limit = swap_total + mem_total // 2
    committed = 7_028_736
    # MemAvailable must track free + reclaimable file cache + SReclaimable
    # (a kernel person will sum it): ~2.8 G + ~4.4 G + ~0.5 G ≈ 7.5 G.
    r_mem = break_ramp(0.5)  # dirty-page physics follow the disk, fast ramp
    mem_free = int(
        gauge(
            "mem.free", _lerp(2_867_200, 2_457_600, r_mem), amp_frac=0.015, phase=0.4, period=1500
        )
    )
    mem_available = int(
        gauge(
            "mem.avail", _lerp(7_864_320, 7_600_000, r_mem), amp_frac=0.012, phase=1.2, period=1700
        )
    )
    # Dirty grows continuously from the healthy ~20 MB once flushes stall
    # (~1.5 MB/s, capped ~900 MB); Writeback ramps with the disk, no spike.
    # The healthy baseline wanders smoothly; the growth term stays clean.
    dirty = max(
        8_192,
        int(gauge("mem.dirty", 20_480, amp_frac=0.12, phase=2.0, period=800))
        + min(901_120, int(broken_seconds() * 1500)),
    )
    writeback = max(
        0, int(gauge("mem.writeback", 196_608 * r_mem, amp_frac=0.10, phase=3.1, period=600))
    )
    # Cached includes postgres shared_buffers (Shmem). Keep Shmem BELOW 20 % of
    # RAM: the Memory check has default levels on shared memory at 20 %/30 %
    # used — 2.5 GiB of 16 GiB is 15.6 %, comfortably green.
    cached = 6_963_200

    # ---- load: the headline symptom. Default CPU-load levels are 5/10 per
    #      core on the 15-min average => CRIT above 40 on 4 cores. Blocked-on-
    #      I/O (D-state) processes count into loadavg, which is exactly the
    #      story: huge load, idle CPU. --------------------------------------- #
    #      The break is no cliff: load climbs as D-state backends pile up,
    #      1-min ahead of 5-min ahead of 15-min (like the kernel's smoothing).
    #      Each timescale wanders on its own clock: 1-min noisy and fast,
    #      15-min heavily smoothed — like the kernel's real EWMA averages.
    r1, r5, r15 = break_ramp(0.55), break_ramp(0.75), break_ramp(1.0)
    l1 = round(_lerp(0.85, 49.0, r1) * gauge("load1", 1.0, amp_frac=0.22, phase=0.2, period=300), 2)
    l5 = round(_lerp(0.85, 46.5, r5) * gauge("load5", 1.0, amp_frac=0.12, phase=1.0, period=900), 2)
    l15 = round(
        _lerp(0.85, 44.0, r15) * gauge("load15", 1.0, amp_frac=0.06, phase=2.0, period=2400), 2
    )
    runnable = 2 + round(r1)
    total_procs = round(_lerp(396, 612, r1))

    # ---- /proc/stat: where the "CPU is idle, it's all I/O wait" clue lives.
    #      iowait ramps with the disk going bad (slightly behind it). -------- #
    r_cpu = break_ramp(0.6)
    user = C_USER.sample(_lerp(60, 25, r_cpu))
    system = C_SYSTEM.sample(_lerp(25, 18, r_cpu))
    idle = C_IDLE.sample(_lerp(300, 40, r_cpu))
    iowait = C_IOWAIT.sample(_lerp(12, 315, r_cpu))  # -> ~80 % of ~400 ticks/s

    # ---- diskstat: sdb read latency = rd_ticks rate / rd_ios rate.
    #      The data disk is a SATA datacenter SSD, so the states are:
    #      healthy:  ~0.4 ms reads, 120 WAL flushes/s @ ~0.5 ms, ~8 % util
    #      degraded: read-retry onset (~1.2 ms reads, ~13 % util) — foreshadow
    #      broken:   retry storms — 55 reads/s accumulating 11000 ms/s ->
    #                ~200 ms per read, ~99 % util, queue ~12. ----------------- #
    sda_rd = SDA["rd_ios"].sample(4)
    sda_rdt = SDA["rd_ticks"].sample(3)
    sda_wr = SDA["wr_ios"].sample(8)
    sda_wrt = SDA["wr_ticks"].sample(7)
    sda_iot = SDA["io_ticks"].sample(10)
    if disk_dying:
        # the cause ramps fastest; r_disk=0 while merely degraded -> the
        # foreshadowing values, then a steep (but not vertical) climb.
        # Retry onset is BURSTY, not a flat creep: most minutes sit at a mild
        # ~1.2 ms, but every few minutes a read-retry storm pushes reads to
        # ~8 ms (and util to ~40 %) for a minute or two — the classic
        # fail-slow stutter (the average barely moves, the spikes are the
        # tell). Seeded per wall-clock minute so re-polls within the same
        # minute agree; storms come in 1-2 min clumps a few times per 10 min.
        minute = int(now // 60)
        storm = max(random.Random(minute).random(), random.Random(minute - 1).random()) > 0.78
        base_rdt = 360 if storm else 55  # ~8 ms vs ~1.2 ms per read
        base_iot = 420 if storm else 130  # ~42 % vs ~13 % util
        r_disk = break_ramp(0.5)
        sdb_rd = SDB["rd_ios"].sample(_lerp(45, 55, r_disk))
        sdb_rdt = SDB["rd_ticks"].sample(_lerp(base_rdt, 11000, r_disk))
        sdb_wr = SDB["wr_ios"].sample(_lerp(120, 30, r_disk))
        sdb_wrt = SDB["wr_ticks"].sample(_lerp(70, 2200, r_disk))
        sdb_iot = SDB["io_ticks"].sample(_lerp(base_iot, 990, r_disk))
        sdb_queue = random.randint(0, 2) + (3 if storm else 0) + round(10 * r_disk)
    else:
        sdb_rd = SDB["rd_ios"].sample(45)
        sdb_rdt = SDB["rd_ticks"].sample(18)
        sdb_wr = SDB["wr_ios"].sample(120)
        sdb_wrt = SDB["wr_ticks"].sample(60)
        sdb_iot = SDB["io_ticks"].sample(75)
        sdb_queue = random.randint(0, 1)

    # ---- network: a normally busy DB box, state-independent ---------------- #
    rx_bytes = C_RX_B.sample(450_000)
    tx_bytes = C_TX_B.sample(380_000)
    rx_pkts = C_RX_P.sample(1400)
    tx_pkts = C_TX_P.sample(1300)

    # ---- SMART: deliberately NOT the smoking gun (fail-slow story, see
    #      smart_attrs()). Attributes stay zero through the incident — only
    #      the temperature creeps past the 35 °C WARN. The check snapshots raw
    #      values at DISCOVERY and goes CRIT when they later exceed that
    #      baseline, which happens only at the late "confession". The overall
    #      self-assessment stays PASSED throughout — dying drives famously
    #      report PASSED until they're gone (cosmetic field, parser-ignored). - #
    pending, realloc, uncorrect, sdb_temp_base = smart_attrs()
    # SSD temperature wanders ~+/-1.3 °C around the (stepping) baseline — a
    # dead-flat temperature line is the giveaway of a faked agent. Long period
    # so it drifts like a real drive in airflow, not a sawtooth.
    sdb_temp = round(gauge("smart.sdb.temp", sdb_temp_base, amp_abs=1.3, phase=0.7, period=900))
    sdb_smart = _smart_json(
        "/dev/sdb",
        "SAMSUNG MZ7L3480HCHQ-00A07",
        "S6KSNE0T502244",
        hours=int(uptime / 3600) + 29000,
        temp=sdb_temp,
        realloc=realloc,
        uncorrect=uncorrect,
        pending=pending,
        crc=0,
        healthy=True,
    )
    sda_smart = _smart_json(
        "/dev/sda",
        "INTEL SSDSC2KB240G8",
        "PHYF034700AB240A",
        hours=int(uptime / 3600) + 21000,
        temp=round(gauge("smart.sda.temp", 28, amp_abs=1.2, phase=2.1, period=1100)),
        realloc=0,
        uncorrect=0,
        pending=0,
        crc=0,
        healthy=True,
    )

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

    # --- agent controller status + deployed plugins (mk_postgres provenance:
    #     we emit its sections, so the plugin must be visible as installed).
    #     The controller pretends to be REGISTERED (TLS): the Check_MK Agent
    #     service only warns "TLS is not activated" when allow_legacy_pull is
    #     true — with a registered pull connection + cert it reads like a
    #     properly TLS-registered host. The cert expiry is checked against
    #     wall clock (WARN/CRIT below 30/15 days), so `to` is dynamic. ---
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
                        "uuid": "3fcd1c3e-d24a-4d8b-a3c0-58f2a8b7c111",
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
    a(f'/opt/checkmk/agent/default/package/plugins/mk_postgres.py:CMK_VERSION="{AGENT_VERSION}"')
    a(f'/opt/checkmk/agent/default/package/plugins/86400/mk_apt:CMK_VERSION="{AGENT_VERSION}"')

    # --- filesystems: / on the (healthy) sda, the DB volume on the dying sdb.
    #     Usage grows and gets cleaned over time (see filesystem_usage): WAL
    #     recycling + daily backup/log rotation sawteeth on a slow growth
    #     trend — never fills up, stays green (the 'looks fine' corroboration).
    a("<<<df_v2>>>")
    root_size = 41_943_040  # 40 GiB
    data_size = 468_713_472  # ~447 GiB usable of the 480 GB data SSD
    root_used, data_used = filesystem_usage(time.time())
    a(
        f"/dev/sda1 ext4 {root_size} {root_used} {root_size - root_used} "
        f"{round(root_used / root_size * 100)}% /"
    )
    a(
        f"/dev/sdb1 ext4 {data_size} {data_used} {data_size - data_used} "
        f"{round(data_used / data_size * 100)}% /var/lib/postgresql"
    )
    # inode usage (the reference dump carries it): a DB volume holds few, huge
    # files so inode use is tiny; the root fs is ordinary.
    a("[df_inodes_start]")
    root_inodes = 2_621_440
    a(f"/dev/sda1 ext4 {root_inodes} 312844 {root_inodes - 312844} 12% /")
    data_inodes = 29_302_784
    a(f"/dev/sdb1 ext4 {data_inodes} 48213 {data_inodes - 48213} 1% /var/lib/postgresql")
    a("[df_inodes_end]")

    # --- mount options (noatime on the DB volume — standard DBA practice) ---
    a("<<<mounts>>>")
    a("/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0")
    a("/dev/sdb1 /var/lib/postgresql ext4 rw,noatime,errors=remount-ro 0 0")

    # --- memory: full /proc/meminfo so the Memory service yields the whole
    #     metric set (active/inactive, slab, shmem, page tables, ...) ---
    a("<<<mem>>>")
    a(f"MemTotal:       {mem_total} kB")
    a(f"MemFree:        {mem_free} kB")
    a(f"MemAvailable:   {mem_available} kB")
    a(f"Buffers:        {_kb(180)} kB")
    a(f"Cached:         {cached} kB")
    a("SwapCached:     0 kB")
    # LRU accounting must hold up: anon LRU = AnonPages + Shmem (shmem sits on
    # the anon LRU since ~4.8); file LRU = Buffers + Cached - Shmem.
    a("Active:         6187420 kB")
    a("Inactive:       6622410 kB")
    a("Active(anon):   4771840 kB")
    a("Inactive(anon): 3511910 kB")
    a("Active(file):   1415580 kB")
    a("Inactive(file): 3110500 kB")
    a("Unevictable:    0 kB")
    a("Mlocked:        0 kB")
    a(f"SwapTotal:      {swap_total} kB")
    a(f"SwapFree:       {swap_total} kB")
    a("Zswap:          0 kB")
    a("Zswapped:       0 kB")
    a(f"Dirty:          {dirty} kB")
    a(f"Writeback:      {writeback} kB")
    a("AnonPages:      5662310 kB")
    a("Mapped:         491520 kB")
    a("Shmem:          2621440 kB")
    a("KReclaimable:   532480 kB")
    a("Slab:           655360 kB")
    a("SReclaimable:   532480 kB")
    a("SUnreclaim:     122880 kB")
    a("KernelStack:    6144 kB")  # ~384 threads x 16 KiB — matches loadavg total
    a("PageTables:     90112 kB")
    a("SecPageTables:  0 kB")
    a("NFS_Unstable:   0 kB")
    a("Bounce:         0 kB")
    a("WritebackTmp:   0 kB")
    a(f"CommitLimit:    {commit_limit} kB")
    a(f"Committed_AS:   {committed} kB")
    a("VmallocTotal:   34359738367 kB")
    a("VmallocUsed:    61440 kB")
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
    a("DirectMap4k:    333004 kB")
    a("DirectMap2M:    7028736 kB")
    a("DirectMap1G:    9437184 kB")

    # --- load average + nproc ---
    a("<<<cpu>>>")
    a(f"{l1} {l5} {l15} {runnable}/{total_procs} {31000 + C_PROC.sample(7) % 9999} {ncpu}")

    # --- uptime ---
    a("<<<uptime>>>")
    a(f"{uptime}.00 {int(uptime * 3.1)}.00")

    # --- systemd-timesyncd: must exist because the unit is running. The check
    #     compares BOTH the [[[epoch]]] marker and the NTPMessage
    #     ReceiveTimestamp against wall clock (defaults: last sync 7500/10800 s,
    #     last NTP message 3600/7200 s), so both are generated dynamically.
    #     Offset/jitter defaults are 200/500 ms — ours stay in the µs range. ---
    # timesyncd re-syncs every poll interval (2048 s); "time since last sync" is
    # the age of the last real sync event, anchored to boot so it sawtooths
    # 0->34min continuously across agent restarts instead of sitting pinned
    # relative to the (push-lagged) payload timestamp.
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
    a("Root distance: 11.804ms (max: 5s)")
    a(f"       Offset: {offset_us:+d}us")
    a("        Delay: 22.158ms")
    a(f"       Jitter: {random.randint(800, 3200) / 1000:.3f}ms")
    a(f" Packet count: {520 + int((time.time() - START) / 2048)}")
    a("    Frequency: +13.279ppm")
    a(f"[[[{last_sync}]]]")
    a("<<<timesyncd_ntpmessage:sep(10)>>>")
    a(
        "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, Precision=-25, "
        "RootDelay=10.681ms, RootDispersion=1.331ms, Reference=B97D5A38, "
        f"OriginateTimestamp={sync_str}, ReceiveTimestamp={sync_str}, "
        f"TransmitTimestamp={sync_str}, DestinationTimestamp={sync_str}, "
        "Ignored=no, PacketCount=63, Jitter=1.342ms }"
    )
    a("Timezone=UTC")

    # --- apt: defaults WARN on any pending normal update and CRIT on security
    #     updates, so a green box reports the exact sentinel string ---
    a("<<<apt:sep(0)>>>")
    a("No updates pending for installation")

    # --- kernel: /proc/stat cpu line -> "CPU utilization" with the iowait clue
    a("<<<kernel>>>")
    a(str(now))
    a(f"cpu {user} 0 {system} {idle} {iowait} 0 0 0 0 0")
    a(f"ctxt {C_CTXT.sample(2800)}")
    a(f"processes {C_PROC.sample(7)}")
    a(f"pgmajfault {C_PGMAJ.sample(1)}")

    # --- diskstat: sda calm, sdb screaming when broken ---
    a("<<<diskstat>>>")
    a(str(now))
    # major minor name rd_ios rd_merges rd_sect rd_ms wr_ios wr_merges wr_sect wr_ms
    # in_prog io_ms weighted_ms (+discard fields)
    a(
        f"8 0 sda {sda_rd} 0 {sda_rd * 24} {sda_rdt} {sda_wr} 0 "
        f"{sda_wr * 48} {sda_wrt} 0 {sda_iot} {sda_iot * 2} 0 0 0 0"
    )
    a(
        f"8 16 sdb {sdb_rd} 0 {sdb_rd * 64} {sdb_rdt} {sdb_wr} 0 "
        f"{sdb_wr * 96} {sdb_wrt} {sdb_queue} {sdb_iot} {sdb_iot * 3} 0 0 0 0"
    )

    # --- network interface: the real agent emits BOTH lnx_if variants — the
    #     plain ip-link block and the sep(58) counter section ---
    a("<<<lnx_if>>>")
    a("[start_iplink]")
    a("1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000")
    a("    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00")
    a(
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel "
        "state UP group default qlen 1000"
    )
    a("    link/ether 02:42:ac:11:00:1c brd ff:ff:ff:ff:ff:ff")
    a("[end_iplink]")
    a("<<<lnx_if:sep(58)>>>")
    a(f"eth0: {rx_bytes} {rx_pkts} 0 0 0 0 0 0 {tx_bytes} {tx_pkts} 0 0 0 0 0 0")
    a("[eth0]")
    a("\tSpeed: 10000Mb/s")
    a("\tDuplex: Full")
    a("\tAuto-negotiation: on")
    a("\tLink detected: yes")
    a("Address: 02:42:ac:11:00:1c")

    # --- tcp connection stats: a DB server's client connections ---
    a("<<<tcp_conn_stats>>>")
    a(f"01 {round(gauge('tcp.estab', 45, amp_abs=6, phase=0.9, period=700))}")
    a(f"02 {random.randint(0, 1)}")
    a(f"06 {round(gauge('tcp.timewait', 13, amp_abs=4, phase=2.4, period=500))}")
    a("0A 4")

    # --- SMART: the disk-health alarm (root cause) ---
    a("<<<smart_posix_all:sep(0)>>>")
    a(sda_smart)
    a(sdb_smart)

    # --- processes: postmaster + helpers + client backends + system daemons.
    #     Realism rules a DBA will check:
    #       * every postgres process maps shared_buffers (Shmem ~2.5 GiB), so
    #         VSZ must be ~2.9 GB — never smaller than the shared segment;
    #       * idle backends say "idle" in their cmdline, only the running ones
    #         show a query verb — counts match pg_stat_activity (sessions);
    #       * pgbouncer.service is running, so pgbouncer must exist in ps.
    #     Broken => queries pile up waiting on I/O (backends 6+2 -> 2+24),
    #     gradually: each poll during the ramp shows a few more stuck SELECTs.
    r_sess = break_ramp(0.75)
    run_sess = round(_lerp(2, 24, r_sess))
    idle_sess = round(_lerp(6 + random.randint(0, 2), 2, r_sess))
    pg_vsz = 2_950_000  # > shared_buffers, incl. mapped segment
    a("<<<ps_lnx>>>")
    a("[time]")
    a(str(now))
    a("[processes]")
    a("[header] CGROUP USER VSZ RSS TIME ELAPSED PID COMMAND")
    for cgs, user, vsz, rss, cputime, pid, cmd in (
        ("init.scope", "root", 168_000, 13_200, "00:00:39", 1, "/sbin/init"),
        (
            "system.slice/systemd-journald.service",
            "root",
            64_400,
            22_100,
            "00:01:42",
            412,
            "/usr/lib/systemd/systemd-journald",
        ),
        (
            "system.slice/systemd-udevd.service",
            "root",
            26_200,
            8_200,
            "00:00:04",
            450,
            "/usr/lib/systemd/systemd-udevd",
        ),
        (
            "system.slice/systemd-resolved.service",
            "systemd-resolve",
            26_800,
            13_600,
            "00:00:58",
            501,
            "/usr/lib/systemd/systemd-resolved",
        ),
        (
            "system.slice/systemd-timesyncd.service",
            "systemd-timesync",
            91_000,
            7_800,
            "00:00:12",
            520,
            "/usr/lib/systemd/systemd-timesyncd",
        ),
        (
            "system.slice/dbus.service",
            "messagebus",
            10_400,
            5_200,
            "00:00:21",
            530,
            "@dbus-daemon --system --address=systemd:",
        ),
        (
            "system.slice/rsyslog.service",
            "syslog",
            222_400,
            6_900,
            "00:00:47",
            640,
            "/usr/sbin/rsyslogd -n -iNONE",
        ),
        (
            "system.slice/smartmontools.service",
            "root",
            13_100,
            6_300,
            "00:00:09",
            655,
            "/usr/sbin/smartd -n",
        ),
        (
            "system.slice/ssh.service",
            "root",
            15_400,
            9_100,
            "00:00:01",
            710,
            "sshd: /usr/sbin/sshd -D [listener]",
        ),
        (
            "system.slice/cron.service",
            "root",
            11_500,
            2_600,
            "00:00:03",
            720,
            "/usr/sbin/cron -f -P",
        ),
        (
            "system.slice/pgbouncer.service",
            "postgres",
            18_900,
            7_400,
            "00:14:33",
            790,
            "/usr/sbin/pgbouncer -d /etc/pgbouncer/pgbouncer.ini",
        ),
    ):
        a(f"0::/{cgs} {user} {vsz} {rss} {cputime} 12-04:11:40 {pid} {cmd}")
    cg = "0::/system.slice/postgresql.service"
    a(
        f"{cg} postgres {pg_vsz} 156000 01:12:33 12-04:11:08 812 "
        f"/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main"
    )
    for i, (helper, rss) in enumerate(
        (
            ("checkpointer", 2_350_000),  # has touched most shared buffers
            ("background writer", 1_650_000),
            ("walwriter", 112_000),
            ("autovacuum launcher", 92_000),
            ("logical replication launcher", 84_000),
        )
    ):
        a(
            f"{cg} postgres {pg_vsz} {rss} 00:0{i}:1{i} 12-04:11:05 "
            f"{815 + i} postgres: 16/main: {helper}"
        )
    for i in range(idle_sess + run_sess):
        verb = "SELECT" if i < run_sess else "idle"
        rss = 180_000 + (i * 37) % 240 * 1000
        a(
            f"{cg} postgres {pg_vsz} {rss} 00:02:{10 + i % 50:02d} 0-01:{12 + i % 40:02d}:09 "
            f"{2200 + i} postgres: 16/main: payments payments 10.1.2.{40 + i % 8}"
            f"(5{3400 + i}) {verb}"
        )

    # --- systemd units: ALL green in every state. The trap: postgres is up and
    #     answering — the failure is hardware, not a crashed unit. A realistic
    #     Ubuntu 24.04 server runs ~30 services (incl. oneshots in
    #     "active/exited"), not 5 — the Summary shows "Total: 31". ------------
    a("<<<systemd_units>>>")
    units = [
        ("postgresql.service", "active", "running", "PostgreSQL RDBMS"),
        ("pgbouncer.service", "active", "running", "connection pooler for PostgreSQL"),
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
        (
            "smartmontools.service",
            "active",
            "running",
            "Self Monitoring and Reporting Technology (SMART) Daemon",
        ),
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
        # oneshots that already ran — "active/exited" on every real box
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
    a("[status]")  # intentionally empty: parser falls back to [all]
    a("[all]")
    for name, act, sub, descr in units:
        a(f"{name} loaded {act} {sub} {descr}")

    # --- scheduled jobs (mk_job): both green — again, no noise ---
    a("<<<job>>>")
    a("==> pg-basebackup <==")
    a(f"start_time {now - 7 * 3600}")
    a("exit_code 0")
    a("real_time 12:41.3")
    a("user_time 8.40")
    a("system_time 3.10")
    a("max_res_kbytes 312000")
    a("avg_mem_kbytes 0")
    a("==> vacuum-analyze <==")
    a(f"start_time {now - 5 * 3600}")
    a("exit_code 0")
    a("real_time 6:02.1")
    a("user_time 4.20")
    a("system_time 1.10")
    a("max_res_kbytes 98000")
    a("avg_mem_kbytes 0")

    # ------------------------------------------------------------------ #
    # mk_postgres plugin sections (instance `main` -> items "MAIN/...").
    # Instance markers are uppercased by the parsers (cmk/plugins/postgres/
    # lib.py parse_dbs). DB-list sections use [databases_start]/[databases_end]
    # followed by a header row; sep(59) sections are semicolon-separated.
    # All stay GREEN (no default levels) — they corroborate, not alarm:
    # broken => commits/s collapse ~10x, one SELECT stuck since the break
    # (live-growing duration), 24 running sessions, slow connect time.
    # ------------------------------------------------------------------ #
    db_list = "[databases_start]\npostgres\npayments\n[databases_end]"

    # --- instance + version (one service: "PostgreSQL Instance MAIN") ---
    a("<<<postgres_instances>>>")
    a("[[[main]]]")
    a("812 /usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main")

    a("<<<postgres_version:sep(1)>>>")
    a("[[[main]]]")
    a(
        "PostgreSQL 16.3 (Ubuntu 16.3-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, "
        "compiled by gcc (Ubuntu 13.2.0-23ubuntu4) 13.2.0, 64-bit"
    )

    # --- sessions: t = idle, f = running. Broken: queries pile up.
    #     (idle_sess/run_sess defined at the ps section — ps, pg_stat_activity
    #     and numbackends must all tell the same story.) ---
    a("<<<postgres_sessions>>>")
    a("[[[main]]]")
    a(f"t {idle_sess}")
    a(f"f {run_sess}")

    # --- pg_stat_database: per-second rates derived by Checkmk; while broken
    #     the workload starves *gradually* (commits 380/s -> ~35/s over the
    #     ramp — the throughput graph shows a collapse curve, not a step). ---
    r_pg = break_ramp(0.6)
    pay = {
        "xact_commit": PG_PAY["xact_commit"].sample(_lerp(380, 35, r_pg)),
        "xact_rollback": PG_PAY["xact_rollback"].sample(0.2),
        "blks_read": PG_PAY["blks_read"].sample(_lerp(40, 55, r_pg)),
        "blks_hit": PG_PAY["blks_hit"].sample(_lerp(9000, 700, r_pg)),
        "tup_returned": PG_PAY["tup_returned"].sample(_lerp(14000, 1100, r_pg)),
        "tup_fetched": PG_PAY["tup_fetched"].sample(_lerp(5000, 400, r_pg)),
        "tup_inserted": PG_PAY["tup_inserted"].sample(_lerp(120, 8, r_pg)),
        "tup_updated": PG_PAY["tup_updated"].sample(_lerp(90, 6, r_pg)),
        "tup_deleted": PG_PAY["tup_deleted"].sample(_lerp(2, 0.2, r_pg)),
    }
    pay_size = 84_512_345_678 + int((time.time() - START) * 2048)
    sys_commit = PG_SYS["xact_commit"].sample(0.5)
    sys_hit = PG_SYS["blks_hit"].sample(25)
    sys_ret = PG_SYS["tup_returned"].sample(30)
    a("<<<postgres_stat_database:sep(59)>>>")
    a("[[[main]]]")
    a(
        "datid;datname;numbackends;xact_commit;xact_rollback;blks_read;blks_hit;"
        "tup_returned;tup_fetched;tup_inserted;tup_updated;tup_deleted;datsize"
    )
    a(f"5;postgres;1;{sys_commit};0;312;{sys_hit};{sys_ret};{sys_ret // 2};4;1;0;7654321")
    a(
        f"16384;payments;{idle_sess + run_sess};{pay['xact_commit']};{pay['xact_rollback']};"
        f"{pay['blks_read']};{pay['blks_hit']};{pay['tup_returned']};{pay['tup_fetched']};"
        f"{pay['tup_inserted']};{pay['tup_updated']};{pay['tup_deleted']};{pay_size}"
    )

    # --- connections: 24/100 active is far below the 80/90 % defaults ->
    #     stays OK, but the pile-up is visible. ---
    a("<<<postgres_connections:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;mc;idle;active")
    a("postgres;100;1;0")
    a(f"payments;100;{idle_sess};{run_sess}")

    # --- query duration: while broken, the longest query has been stuck
    #     since the break — its duration GROWS live across re-polls. ---
    a("<<<postgres_query_duration:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;datid;usename;client_addr;state;seconds;pid;current_query")
    if broken:
        stuck = 47 + int(broken_seconds())
        a(
            f"payments;16384;payments;10.1.2.44;active;{stuck};2207;"
            "SELECT o.id, o.amount, t.status FROM orders o JOIN transactions t "
            "ON t.order_id = o.id WHERE o.settled = false"
        )
    else:
        a(
            f"payments;16384;payments;10.1.2.41;active;{random.randint(0, 3)};2204;"
            "SELECT id, status FROM orders WHERE created_at > now() - interval '5 minutes'"
        )
    a("postgres;5;postgres;;active;0;812;SELECT 1")

    # --- locks: a few granted shared locks, slightly more while broken ---
    a("<<<postgres_locks:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;granted;mode")
    a("postgres;;")
    for _ in range(3 + round(5 * r_sess)):
        a("payments;t;AccessShareLock")
    for _ in range(round(2 * r_sess)):
        a("payments;t;RowExclusiveLock")

    # --- vacuum/analyze recency (green; pure realism) ---
    a("<<<postgres_stats:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a("datname;sname;tname;vtime;atime")
    a("postgres;pg_catalog;pg_statistic;-1;-1")
    a(f"payments;public;orders;{now - 6 * 3600};{now - 6 * 3600}")
    a(f"payments;public;transactions;{now - 6 * 3600};{now - 6 * 3600}")
    a(f"payments;public;audit_log;{now - 30 * 3600};{now - 30 * 3600}")

    # --- table/index bloat (mk_postgres emits this too; defaults alert at
    #     bloat factor 180/200 % — ours sit at a healthy 1.1-1.6) ---
    a("<<<postgres_bloat:sep(59)>>>")
    a("[[[main]]]")
    a(db_list)
    a(
        "db;schemaname;tablename;tups;pages;otta;tbloat;wastedpages;wastedbytes;"
        "wastedsize;iname;itups;ipages;iotta;ibloat;wastedipages;wastedibytes;"
        "wastedisize;totalwastedbytes"
    )
    a(
        "postgres;pg_catalog;pg_statistic;412;14;11;1.3;3;24576;24 kB;"
        "pg_statistic_relid_att_inh_index;412;6;4;1.5;2;16384;16 kB;40960"
    )
    a(
        "payments;public;orders;18412022;312480;271722;1.2;40758;333930496;318 MB;"
        "orders_pkey;18412022;91220;70169;1.3;21051;172449792;164 MB;506380288"
    )
    a(
        "payments;public;transactions;44820110;780122;709201;1.1;70921;580984832;554 MB;"
        "transactions_pkey;44820110;221080;138175;1.6;82905;679157760;648 MB;1260142592"
    )
    a(
        "payments;public;audit_log;9210441;160233;145666;1.1;14567;119324672;114 MB;"
        "audit_log_pkey;9210441;40110;30852;1.3;9258;75841536;72 MB;195166208"
    )

    # --- connect time: even opening a connection crawls while broken,
    #     creeping up with the backend pile-up rather than jumping ---
    a("<<<postgres_conn_time>>>")
    a("[[[main]]]")
    conn_t = round(
        _lerp(0.012, 1.9, break_ramp(0.75))
        * gauge("pg.conn_time", 1.0, amp_frac=0.15, phase=1.5, period=350),
        3,
    )
    a(str(conn_t))

    return ("\n".join(lines) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
#  State persistence across restarts
#
#  Checkmk's rate-based checks (postgres Statistics, Disk IO, Kernel
#  Performance) abort with IgnoreResults when a counter goes BACKWARDS — and
#  every process restart used to re-seed all counters from scratch, marking
#  exactly those services stale until two fresh samples arrived. Persisting
#  the accumulators (plus START for uptime continuity and the incident state
#  incl. its timestamps, so a redeploy mid-demo doesn't reset the story)
#  makes restarts invisible to the monitoring: the next sample simply
#  integrates the current rate across the downtime gap.
# --------------------------------------------------------------------------- #
STATE_FILE = os.environ.get("STATE_FILE", "/var/tmp/cmk-demo-dying-disk-state.json")


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
        if isinstance(saved, list):
            # v1 file (order-keyed): restore by position if the layout matches
            saved = (
                dict(zip(_ALL_COUNTERS, saved, strict=False))
                if len(saved) == len(_ALL_COUNTERS)
                else {}
            )
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
#  TCP agent server
# --------------------------------------------------------------------------- #
class AgentHandler(StreamRequestHandler):
    def handle(self) -> None:
        try:
            payload = build_agent_output(get_state())
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Checkmk closed early; nothing to do
        save_state()  # cheap (~once a minute); survives restarts/reboots


class AgentServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------- #
#  HTTP admin endpoint (state toggle + control screen — nothing is monitored)
# --------------------------------------------------------------------------- #
STATE_META = {
    "healthy": {
        "color": "#2e7d32",
        "label": "HEALTHY",
        "tagline": "All green. Discover the host in this state (SMART baselines!).",
        "effects": [
            "every service OK — the starting picture",
            "SMART raw values at zero → discovery baselines them",
            "load ~0.9, SSD reads ~0.4 ms, postgres committing ~380 tx/s",
        ],
    },
    "degraded": {
        "color": "#f9a825",
        "label": "DEGRADED",
        "tagline": "The disk starts failing the *fail-slow* way — nothing red points at it. "
        "Trigger ~24 min before you want the load page."
        + (
            f" Auto-escalates to BROKEN after {AUTO_BREAK_AFTER_MIN:g} min."
            if AUTO_BREAK_AFTER_MIN > 0
            else ""
        ),
        "effects": [
            "SMART SAMSUNG … Stats stays GREEN — read-retry storms don't show up in SMART "
            "attributes (they only count completed failures), the drive still says PASSED",
            "Temperature SMART SAMSUNG … → WARN at ~2.5 min (creeps 33 → 38 °C past the 35 °C "
            "default) — the only breadcrumb, and one everybody blames on the rack AC",
            "Disk IO SUMMARY service: 'Read latency' starts to STUTTER — ~1.2 ms baseline with "
            "read-retry storms to ~8 ms (util ~40 %) in 1-2 min clumps every few minutes; the "
            "fail-slow signature, visible in the graph only, the service itself stays OK",
        ],
    },
    "broken": {
        "color": "#c62828",
        "label": "BROKEN",
        "tagline": "The full incident — the CPU-load page Sam gets. "
        + (
            f"Ramps up over ~{BREAK_RAMP_MIN:g} min, no vertical cliffs."
            if BREAK_RAMP_MIN > 0
            else "Instant (no ramp)."
        ),
        "effects": [
            f"CPU load climbs to CRIT (15-min load ~44 on 4 cores; crosses WARN 20 at "
            f"~{BREAK_RAMP_MIN * 0.45:.1f} min, CRIT 40 at ~{BREAK_RAMP_MIN * 0.9:.1f} min — "
            "1-min load leads, 15-min lags, like real loadavg)",
            "CPU utilization: compute idle, I/O wait ramps to ~80 %",
            "Disk IO SUMMARY: 'Read latency' climbs ~1.2 → ~200 ms "
            "(fastest ramp — it's the cause), "
            "'Utilization' → ~99 % (CRIT only with the optional Disk IO "
            "levels rule, otherwise graph-only)",
            "postgres: commits collapse ~10× as a curve, "
            "sessions pile up 2 → 24 running poll by poll, "
            "one SELECT stuck (duration grows live)",
            "memory: Dirty pages pile up (~1.5 MB/s), "
            "Writeback ramps 0 → ~190 MB (flushes can't drain)",
            f"SMART stays GREEN for the first ~{SMART_CONFESSION_MIN:g} min — "
            "no service points at the "
            "disk; the AI has to fuse load + iowait + latency + Dirty/Writeback + postgres itself. "
            "Then the drive finally confesses: pending sectors → CRIT "
            "(cascading: uncorrectable ~5 min, "
            "reallocated ~10 min later), vindicating the diagnosis",
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
    extras = []
    if degraded_minutes() > 0:
        pending, realloc, uncorrect, temp = smart_attrs()
        if pending == 0:
            confession = ""
            if state == "broken" and SMART_CONFESSION_MIN > 0:
                left = max(0.0, SMART_CONFESSION_MIN * 60 - broken_seconds())
                confession = f"; confesses in {_fmt_duration(left)}"
            extras.append(
                f"disk dying for {_fmt_duration(degraded_minutes() * 60)} — "
                f"SMART attrs still clean (fail-slow), only temp {temp} °C"
                f"{confession}"
            )
        else:
            extras.append(
                f"disk dying for {_fmt_duration(degraded_minutes() * 60)} — "
                f"SMART confessed (pending: {pending}, reallocated: {realloc}, "
                f"uncorrectable: {uncorrect}, temp {temp} °C)"
            )
    if broken_seconds() > 0:
        extras.append(f"stuck query running for {_fmt_duration(broken_seconds() + 47)}")
        if break_ramp() < 1.0:
            extras.append(
                f"impact ramping up: {break_ramp() * 100:.0f} % "
                f"(15-min load now ~{_lerp(0.85, 44.0, break_ramp()):.0f}, "
                f"CRIT > 40 at ~{BREAK_RAMP_MIN * 0.9:.1f} min)"
            )
    if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
        left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - state_since_seconds())
        extras.append(f"auto-escalates to BROKEN in {_fmt_duration(left)}")
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
 <h1>demo control — <b>{HOSTNAME}</b>
 <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 <div class="state">{meta["label"]}</div>
 <div class="since">in this state for <b>{_fmt_duration(state_since_seconds())}</b>
  — {meta["tagline"]}</div>
 {extra_html}
 <div class="cards">{"".join(cards)}</div>
 <div class="foot">curl API: /admin/heal · /admin/degrade · /admin/break · / (JSON status)
  — discover the host in Checkmk while HEALTHY, or the SMART baseline is poisoned.</div>
</body></html>"""


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "db-demo-ctl/1.0"

    def log_message(self, format: str, *args) -> None:  # quieter logs
        print(f"[http] {self.address_string()} {format % args}")

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
            if "ui=1" in query:  # button on the /admin screen: bounce back
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
                "disk_dying_for_minutes": round(degraded_minutes(), 1),
                "stuck_query_seconds": round(broken_seconds() + 47) if broken_seconds() else 0,
                "auto_break_in_s": auto_break_in,
                "toggles": ["/admin/degrade", "/admin/break", "/admin/heal"],
                "ui": "/admin",
            },
        )


def _auto_break_watchdog() -> None:
    """Escalate degraded -> broken after AUTO_BREAK_AFTER_MIN minutes.

    A dying disk doesn't politely stay in 'a few SMART counters' forever —
    after a while the retry storms hit performance. Uses time-in-state (not
    _degraded_since), so manually toggling broken -> degraded restarts the
    clock instead of escalating right back.
    """
    while True:
        time.sleep(5)
        if get_state() == "degraded" and state_since_seconds() >= AUTO_BREAK_AFTER_MIN * 60:
            set_state("broken")
            print(f"[ctl] -> BROKEN (auto: degraded for {AUTO_BREAK_AFTER_MIN:g} min)")


def main() -> None:
    load_state()  # restart-proof counters/uptime/incident state
    agent = AgentServer(("0.0.0.0", AGENT_PORT), AgentHandler)  # nosec B104
    http = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)  # nosec B104

    threading.Thread(target=agent.serve_forever, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=_auto_break_watchdog, daemon=True).start()
        print(
            f"[boot] auto-escalation: degraded -> broken after "
            f"{AUTO_BREAK_AFTER_MIN:g} min in degraded"
        )
    print(
        f"[boot] host={HOSTNAME!r}  agent=tcp/{AGENT_PORT}  ctl=tcp/{HTTP_PORT}  "
        f"start_state={get_state()}"
    )
    print(f"[boot] control UI:   http://localhost:{HTTP_PORT}/admin")
    print(f"[boot] curl API:     curl localhost:{HTTP_PORT}/admin/degrade|/admin/break|/admin/heal")
    print("[boot] IMPORTANT: run service discovery in Checkmk while *healthy* —")
    print("[boot] the SMART check baselines raw values at discovery time.")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        agent.shutdown()
        http.shutdown()


if __name__ == "__main__":
    main()
