#!/usr/bin/env python3
"""Meridian Retail network estate — fake SNMP devices, answered live.

Where the server hosts fake a Checkmk *agent* over TCP, network gear is
monitored via SNMP. Two transports (--transport, default snmp):

  snmp  — answer SNMP v2c LIVE on ONE UDP port (snmp/snmpserver.py), routing
          to a device by its UNIQUE community string (= the device short name);
          Checkmk polls 127.0.0.1:<port> with a per-host community. NO site
          filesystem, NO sudo — and one port means netsim ports-maps into a
          normal container like the gateway (no --network host). The default,
          and what estate.py uses.
  walk  — legacy: write a stored-walk file per device into the site's
          `var/check_mk/snmpwalks/<host>` (the `usewalk_hosts` ruleset /
          StoredWalk backend). Needs to run AS THE SITE USER (-> sudo), which
          is exactly what the live transport avoids.

Either way this daemon is the SNMP twin of the estate's serve.py servers: it
renders each device's OID space on demand with monotonically advancing
counters (live traffic graphs, not flat lines), autocorrelated gauges (CPU,
temperature) and a break/heal control plane.

Devices (see FLEET.md):

  sw-core-01    Catalyst 9300 DC core switch       steady green
  sw-access-01  Catalyst 9200 server-access switch incident: CRC error storm
                                                   on uplink Te1/1/1 (WARN),
                                                   then the link dies (CRIT)
  rt-wan-01     ISR 2921 DC WAN head-end router    incident: WAN saturation ->
                                                   CPU climbs past 80/90
  ups-01        APC Smart-UPS 3000                 steady green (behind
                                                   sw-access-01)

This IS the estate's network layer: the DC core switch sw-core-01 tops the
parent topology; servers and the UPS hang off sw-access-01, which uplinks to
the core (applied by deploy/cmk_setup.py when the SNMP layer is deployed).

Usual invocation (no privileges needed):

    python3 netsim.py                          # live SNMP on 127.0.0.1:1161
    python3 netsim.py --transport walk --site heute   # legacy stored walks

Then bootstrap Checkmk once with the estate tool (`../estate.py up`) or
`deploy/cmk_setup.py` directly (folder + hosts with per-host community + one
port rule + discovery + activation, stdlib-only). Normally you don't run this by
hand at all — estate.py starts and stops it.

Control plane: http://localhost:8101/admin (combined panel for all devices),
curl API: /admin/<device>/degrade|break|heal, JSON status on /.

Walk file format (verified against the parser in stored_walk.py and the
writer in `cmk --snmpwalk`): one `.<oid> <value>` line, numerically sorted;
printable-ASCII values raw and unquoted; binary values (MACs) quoted
uppercase hex WITH a trailing space before the closing quote
(`"B2 E0 7D 2C 4D 15 "`) — without that trailing space the parser keeps the
literal ASCII text instead of decoding bytes.
"""

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DOMAIN = os.environ.get("ESTATE_DOMAIN", "corp.meridian-retail.com")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8101"))
RENDER_INTERVAL = float(os.environ.get("RENDER_INTERVAL", "30"))
AUTO_BREAK_AFTER_MIN = float(os.environ.get("AUTO_BREAK_AFTER_MIN", "20"))
# per-uid so a run as one user never collides with a state file another user
# left in sticky /var/tmp (the live SNMP transport runs as the caller, not the
# site user — a shared path would EPERM on os.replace every save)
STATE_FILE = os.environ.get("STATE_FILE", f"/var/tmp/cmk-demo-netsim-state-{os.getuid()}.json")

START = time.time()

STATES = ("healthy", "degraded", "broken")
ACTION_TO_STATE = {"heal": "healthy", "degrade": "degraded", "break": "broken"}


# --------------------------------------------------------------------------- #
#  Wobble / gauges / counters — same physics as the agent hosts (see
#  hosts/db-postgres-01/serve.py, the reference implementation): incommensurate
#  long-period harmonics + AR(1) noise, clamped to [-1, 1]; counters integrate
#  the current rate so a state flip changes the slope, never the accumulated
#  value (a backwards counter would wreck the if64 rates).
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


def gauge(
    name: str,
    base: float,
    *,
    amp_abs: float | None = None,
    amp_frac: float | None = None,
    phase: float = 0.0,
    period: float = 1200.0,
) -> float:
    w = _GAUGES.get(name)
    if w is None:
        w = _GAUGES[name] = _Wobble(phase, period)
    d = w.step(time.time())
    if amp_abs is not None:
        return base + amp_abs * d
    return base * (1.0 + (amp_frac or 0.0) * d)


_ALL_COUNTERS: dict[str, "Counter"] = {}


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
        _ALL_COUNTERS[name] = self  # stable name -> restart-proof persistence

    def sample(self, rate_per_s: float) -> int:
        now = time.time()
        dt = max(0.0, now - self.last)
        inst = rate_per_s * (1.0 + self.amp * self.wob.step(now))
        self.acc += inst * dt
        self.last = now
        return int(self.acc)


# --------------------------------------------------------------------------- #
#  Per-device incident state machine (same semantics as the agent hosts)
# --------------------------------------------------------------------------- #
class DeviceState:
    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._state = "healthy"
        self._state_since = START
        self._degraded_since: float | None = None
        self._broken_since: float | None = None

    def get(self) -> str:
        with self._lock:
            return self._state

    def set(self, value: str) -> None:
        with self._lock:
            if value != self._state:
                self._state_since = time.time()
            self._state = value
            if value == "healthy":
                self._degraded_since = None
            elif self._degraded_since is None:
                self._degraded_since = time.time()
            if value == "broken":
                if self._broken_since is None:
                    self._broken_since = time.time()
            else:
                self._broken_since = None

    def since_seconds(self) -> float:
        with self._lock:
            return time.time() - self._state_since

    def degraded_minutes(self) -> float:
        with self._lock:
            return (
                0.0 if self._degraded_since is None else (time.time() - self._degraded_since) / 60.0
            )

    def broken_seconds(self) -> float:
        with self._lock:
            return 0.0 if self._broken_since is None else time.time() - self._broken_since

    def ramp(self, minutes: float = 3.0) -> float:
        """0 -> 1 over `minutes` since the break — incidents build, no cliffs."""
        bs = self.broken_seconds()
        return 1.0 if minutes <= 0 else min(1.0, bs / (minutes * 60.0))

    def dump(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "state_since": self._state_since,
                "degraded_since": self._degraded_since,
                "broken_since": self._broken_since,
            }

    def restore(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._state = data.get("state", "healthy")
            self._state_since = data.get("state_since", time.time())
            self._degraded_since = data.get("degraded_since")
            self._broken_since = data.get("broken_since")


def _lerp(a: float, b: float, r: float) -> float:
    return a + (b - a) * r


# --------------------------------------------------------------------------- #
#  Walk rendering helpers
# --------------------------------------------------------------------------- #
def _oid_key(oid: str) -> tuple[int, ...]:
    return tuple(int(p) for p in oid.strip(".").split("."))


def hex_bytes(raw: bytes) -> str:
    """Binary value in walk encoding: quoted uppercase hex pairs, each
    followed by a space — INCLUDING the last one before the closing quote
    (the parser's _is_hex_string requires the trailing space)."""
    return '"' + "".join(f"{b:02X} " for b in raw) + '"'


def mac(dev_seed: int, index: int) -> str:
    return hex_bytes(bytes([0x00, 0x1B, 0x2C, dev_seed & 0xFF, (index >> 8) & 0xFF, index & 0xFF]))


def render_walk(rows: list[tuple[str, str]]) -> str:
    """Numerically sorted `.oid value` lines — the binary search in the
    StoredWalk backend requires full numeric sort order."""
    out = []
    for oid, value in sorted(rows, key=lambda r: _oid_key(r[0])):
        out.append(f"{oid} {value}\n")
    return "".join(out)


U32 = 2**32


# --------------------------------------------------------------------------- #
#  Interfaces (if64): all 21 columns the section fetches + ifName for the
#  if_names section. ifType 6 + ifOperStatus 1 => discovered (item = padded
#  ifIndex). Error levels default WARN 0.01 % / CRIT 0.1 % of packets — the
#  lever for the CRC-storm incident. Bandwidth has NO default levels (graphs
#  only). Verified in cmk/plugins/network/agent_based/if64.py +
#  packages/cmk-plugins/cmk/plugins/lib/interfaces.py.
# --------------------------------------------------------------------------- #
class Iface:
    def __init__(
        self,
        dev: str,
        index: int,
        name: str,
        descr: str,
        alias: str,
        mbit: int,
        in_bps: float,
        out_bps: float,
        seed: int,
    ) -> None:
        self.index = index
        self.name = name
        self.descr = descr
        self.alias = alias
        self.mbit = mbit  # ifHighSpeed (Mbit/s)
        self.in_bps = in_bps  # healthy octets/s in
        self.out_bps = out_bps
        self.oper = 1
        self.seed = seed
        key = f"{dev}.if{index}"
        ph = (index * 0.73) % 6.28
        aged = 87 * 86400  # pretend counters aged ~87 days
        self.c = {
            "in_oct": Counter(f"{key}.in_oct", phase=ph, start=in_bps * aged),
            "out_oct": Counter(f"{key}.out_oct", phase=ph + 0.5, start=out_bps * aged),
            "in_ucast": Counter(f"{key}.in_ucast", phase=ph + 1.0, start=in_bps / 700 * aged),
            "out_ucast": Counter(f"{key}.out_ucast", phase=ph + 1.5, start=out_bps / 700 * aged),
            "in_mcast": Counter(f"{key}.in_mcast", phase=ph + 2.0, start=2 * aged),
            "in_bcast": Counter(f"{key}.in_bcast", phase=ph + 2.5, start=0.5 * aged),
            "out_mcast": Counter(f"{key}.out_mcast", phase=ph + 3.0, start=1 * aged),
            "out_bcast": Counter(f"{key}.out_bcast", phase=ph + 3.5, start=0.2 * aged),
            "in_disc": Counter(f"{key}.in_disc", phase=ph + 4.0, start=0),
            "in_err": Counter(f"{key}.in_err", phase=ph + 4.2, amp=0.15, start=0),
            "out_disc": Counter(f"{key}.out_disc", phase=ph + 4.4, start=0),
            "out_err": Counter(f"{key}.out_err", phase=ph + 4.6, start=0),
        }
        # current modifiers, set by the owning device before sampling
        self.rate_factor = 1.0  # scales traffic
        self.err_rate = 0.0  # ifInErrors per second
        self.disc_rate = 0.0  # ifOutDiscards per second

    def rows(self) -> list[tuple[str, str]]:
        i = self.index
        up = self.oper == 1
        f = self.rate_factor if up else 0.0
        in_bps, out_bps = self.in_bps * f, self.out_bps * f
        c = self.c
        vals = {
            "in_oct": c["in_oct"].sample(in_bps),
            "out_oct": c["out_oct"].sample(out_bps),
            "in_ucast": c["in_ucast"].sample(in_bps / 700),
            "out_ucast": c["out_ucast"].sample(out_bps / 700),
            "in_mcast": c["in_mcast"].sample(2 * f),
            "in_bcast": c["in_bcast"].sample(0.5 * f),
            "out_mcast": c["out_mcast"].sample(1 * f),
            "out_bcast": c["out_bcast"].sample(0.2 * f),
            "in_disc": c["in_disc"].sample(0.0),
            "in_err": c["in_err"].sample(self.err_rate if up else 0.0),
            "out_disc": c["out_disc"].sample(self.disc_rate if up else 0.0),
            "out_err": c["out_err"].sample(0.0),
        }
        if_speed = min(self.mbit * 1_000_000, U32 - 1)  # ifSpeed caps at ~4.3G
        return [
            (f".1.3.6.1.2.1.2.2.1.1.{i}", str(i)),
            (f".1.3.6.1.2.1.2.2.1.2.{i}", self.descr),
            (f".1.3.6.1.2.1.2.2.1.3.{i}", "6"),  # ethernetCsmacd
            (f".1.3.6.1.2.1.2.2.1.5.{i}", str(if_speed)),
            (f".1.3.6.1.2.1.2.2.1.6.{i}", mac(self.seed, i)),
            (f".1.3.6.1.2.1.2.2.1.8.{i}", str(self.oper)),
            (f".1.3.6.1.2.1.2.2.1.13.{i}", str(vals["in_disc"] % U32)),
            (f".1.3.6.1.2.1.2.2.1.14.{i}", str(vals["in_err"] % U32)),
            (f".1.3.6.1.2.1.2.2.1.19.{i}", str(vals["out_disc"] % U32)),
            (f".1.3.6.1.2.1.2.2.1.20.{i}", str(vals["out_err"] % U32)),
            (f".1.3.6.1.2.1.2.2.1.21.{i}", "0"),  # ifOutQLen
            (f".1.3.6.1.2.1.31.1.1.1.1.{i}", self.name),
            (f".1.3.6.1.2.1.31.1.1.1.6.{i}", str(vals["in_oct"])),
            (f".1.3.6.1.2.1.31.1.1.1.7.{i}", str(vals["in_ucast"])),
            (f".1.3.6.1.2.1.31.1.1.1.8.{i}", str(vals["in_mcast"])),
            (f".1.3.6.1.2.1.31.1.1.1.9.{i}", str(vals["in_bcast"])),
            (f".1.3.6.1.2.1.31.1.1.1.10.{i}", str(vals["out_oct"])),
            (f".1.3.6.1.2.1.31.1.1.1.11.{i}", str(vals["out_ucast"])),
            (f".1.3.6.1.2.1.31.1.1.1.12.{i}", str(vals["out_mcast"])),
            (f".1.3.6.1.2.1.31.1.1.1.13.{i}", str(vals["out_bcast"])),
            (f".1.3.6.1.2.1.31.1.1.1.15.{i}", str(self.mbit)),
            (f".1.3.6.1.2.1.31.1.1.1.18.{i}", self.alias),
        ]


# --------------------------------------------------------------------------- #
#  Devices
# --------------------------------------------------------------------------- #
class Device:
    short: str = ""
    uptime_offset: float = 87 * 86400
    sys_descr: str = ""
    sys_objectid: str = ""
    location: str = ""
    incident = False
    role: str = "network"  # folder-taxonomy key (deploy/cmk_setup.py)
    parent: str | None = "sw-core-01"  # short name of the upstream device
    tagline_effects: dict[str, list[str]] = {}

    def __init__(self) -> None:
        self.state = DeviceState(self.short)

    @property
    def fqdn(self) -> str:
        return f"{self.short}.{DOMAIN}"

    def system_rows(self, now: float) -> list[tuple[str, str]]:
        ticks = int((now - START + self.uptime_offset) * 100)
        return [
            (".1.3.6.1.2.1.1.1.0", self.sys_descr),
            (".1.3.6.1.2.1.1.2.0", self.sys_objectid),
            (".1.3.6.1.2.1.1.3.0", str(ticks)),
            (".1.3.6.1.2.1.1.4.0", "netops@meridian-retail.com"),
            (".1.3.6.1.2.1.1.5.0", self.fqdn),
            (".1.3.6.1.2.1.1.6.0", self.location),
        ]

    def rows(self, now: float) -> list[tuple[str, str]]:
        raise NotImplementedError

    def walk(self, now: float) -> str:
        return render_walk(self.rows(now))

    def dynamic_rows(self, now: float) -> list[tuple[str, str]]:
        """OIDs to refresh in a cached SNMP table. The synthetic devices
        re-render everything and are small (<=~600 OIDs), so refresh them
        fully; ReplayDevice overrides this to return only its changing OIDs."""
        return self.rows(now)

    def status_extras(self) -> list[str]:
        return []


def envmon_temp_rows(descr: str, celsius: float, threshold: int) -> list[tuple[str, str]]:
    """CISCO-ENVMON-MIB temperature row (classic IOS): value + device
    threshold + state 1 (normal)."""
    return [
        (".1.3.6.1.4.1.9.9.13.1.3.1.2.1", descr),
        (".1.3.6.1.4.1.9.9.13.1.3.1.3.1", str(int(round(celsius)))),
        (".1.3.6.1.4.1.9.9.13.1.3.1.4.1", str(threshold)),
        (".1.3.6.1.4.1.9.9.13.1.3.1.6.1", "1"),
    ]


def envmon_fan_psu_rows(fans: list[str], psus: list[str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for n, name in enumerate(fans, start=1):
        rows += [
            (f".1.3.6.1.4.1.9.9.13.1.4.1.2.{n}", name),
            (f".1.3.6.1.4.1.9.9.13.1.4.1.3.{n}", "1"),
        ]  # normal
    for n, name in enumerate(psus, start=1):
        rows += [
            (f".1.3.6.1.4.1.9.9.13.1.5.1.2.{n}", name),
            (f".1.3.6.1.4.1.9.9.13.1.5.1.3.{n}", "1"),  # normal
            (f".1.3.6.1.4.1.9.9.13.1.5.1.4.{n}", "2"),
        ]  # source: ac
    return rows


def catalyst_platform_rows(
    dev: str, cpu_pct: float, temp_c: float, mem_used: int, mem_free: int
) -> list[tuple[str, str]]:
    """Modern IOS-XE Catalyst: cisco_cpu_multiitem (cpmCPU row 7 -> ENTITY
    1001), enhanced-64 cisco_mem pool, CISCO-ENTITY-SENSOR temperature with
    device thresholds WARN 65 / CRIT 75, ENVMON fans + PSUs."""
    rows = [
        # ENTITY-MIB skeleton: chassis 1, CPU 1001, temp sensor 1010
        (".1.3.6.1.2.1.47.1.1.1.1.4.1", "0"),
        (".1.3.6.1.2.1.47.1.1.1.1.4.1001", "1"),
        (".1.3.6.1.2.1.47.1.1.1.1.4.1010", "1"),
        (".1.3.6.1.2.1.47.1.1.1.1.5.1", "3"),  # chassis
        (".1.3.6.1.2.1.47.1.1.1.1.5.1001", "12"),  # cpu
        (".1.3.6.1.2.1.47.1.1.1.1.5.1010", "8"),  # sensor
        (".1.3.6.1.2.1.47.1.1.1.1.7.1", "Switch 1 Chassis"),
        (".1.3.6.1.2.1.47.1.1.1.1.7.1001", "Switch 1 CPU"),
        (".1.3.6.1.2.1.47.1.1.1.1.7.1010", "Switch 1 - Temp Sensor 0"),
        # cisco_cpu_multiitem: physical index ref + 5-min utilization %
        (".1.3.6.1.4.1.9.9.109.1.1.1.1.2.7", "1001"),
        (".1.3.6.1.4.1.9.9.109.1.1.1.1.8.7", str(int(round(cpu_pct)))),
        # cisco_mem enhanced 64-bit pool "Processor"
        (".1.3.6.1.4.1.9.9.221.1.1.1.1.3.2.1", "Processor"),
        (".1.3.6.1.4.1.9.9.221.1.1.1.1.18.2.1", str(mem_used)),
        (".1.3.6.1.4.1.9.9.221.1.1.1.1.20.2.1", str(mem_free)),
        # CISCO-ENTITY-SENSOR temp sensor 1010: celsius, scale units, prec 0
        (".1.3.6.1.4.1.9.9.91.1.1.1.1.1.1010", "8"),
        (".1.3.6.1.4.1.9.9.91.1.1.1.1.2.1010", "9"),
        (".1.3.6.1.4.1.9.9.91.1.1.1.1.3.1010", "0"),
        (".1.3.6.1.4.1.9.9.91.1.1.1.1.4.1010", str(int(round(temp_c)))),
        (".1.3.6.1.4.1.9.9.91.1.1.1.1.5.1010", "1"),
        # device thresholds: minor(10) >= 65 -> WARN, major(20) >= 75 -> CRIT
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.2.1010.1", "10"),
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.3.1010.1", "4"),
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.4.1010.1", "65"),
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.2.1010.2", "20"),
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.3.1010.2", "4"),
        (".1.3.6.1.4.1.9.9.91.1.2.1.1.4.1010.2", "75"),
    ]
    rows += envmon_fan_psu_rows(
        fans=["Switch 1 - FAN 1", "Switch 1 - FAN 2"],
        psus=["Switch 1 - Power Supply A, Normal", "Switch 1 - Power Supply B, Normal"],
    )
    return rows


class SwCore(Device):
    """Core switch — steady-green background (12 x 10G)."""

    short = "sw-core-01"
    uptime_offset = 214 * 86400
    sys_descr = (
        "Cisco IOS Software [Amsterdam], Catalyst L3 Switch Software "
        "(CAT9K_IOSXE), Version 17.3.4, RELEASE SOFTWARE (fc2), "
        "Copyright (c) 1986-2021 by Cisco Systems, Inc."
    )
    sys_objectid = ".1.3.6.1.4.1.9.1.2494"
    location = "DC comms room, network row"
    incident = False
    role = "net_switches"
    parent = None

    def __init__(self) -> None:
        super().__init__()
        # The 12 x 10G ports are ALL uplink trunks to the layer below — the two
        # DC distribution switches, the HQ distribution switch, the edge
        # firewalls and the WAN/internet routers (endpoints live on the access
        # /ToR switches, never here). Aliases name the peer so a specialist can
        # match the interface table against the parent/child topology:
        #  - Te1/0/4 <-> rt-wan-01 Gi0/0 (its alias names this port back);
        #  - Te1/0/9+10 <-> sw-access-01's two uplinks (in <-> out swapped,
        #    ~half the access-port sums each — see SwAccess, whose broken state
        #    fails Te1/1/1 over to Te1/1/2);
        #  - fw-02 is the PASSIVE HA member: heartbeat/sync only, near-idle.
        self.ifaces = []
        peers: list[tuple[str, float, float]] = [
            ("link sw-dc-dist-01 (DC distribution)", 210e6, 240e6),
            ("link sw-dc-dist-02 (DC distribution)", 205e6, 232e6),
            ("link sw-hq-dist-01 (HQ distribution)", 120e6, 90e6),
            ("link rt-wan-01 Gi0/0 (WAN head-end)", 21e6, 24e6),
            ("link rt-inet-01 (internet edge)", 34e6, 28e6),
            ("link fw-01 (edge firewall HA, active)", 30e6, 26e6),
            ("link fw-02 (edge firewall HA, passive)", 0.6e6, 0.5e6),
            ("link fw-dmz-01 (DMZ)", 6e6, 5e6),
            ("link sw-access-01 Te1/1/1", 17e6, 11e6),
            ("link sw-access-01 Te1/1/2", 16e6, 11e6),
            ("(spare)", 0.5e6, 0.4e6),
            ("(spare)", 0.4e6, 0.3e6),
        ]
        for n, (alias, base_in, base_out) in enumerate(peers, start=1):
            self.ifaces.append(
                Iface(
                    "sw-core-01",
                    n,
                    f"Te1/0/{n}",
                    f"TenGigabitEthernet1/0/{n}",
                    alias,
                    10000,
                    base_in,
                    base_out,
                    seed=1,
                )
            )

    def rows(self, now: float) -> list[tuple[str, str]]:
        rows = self.system_rows(now)
        rows.append((".1.3.6.1.2.1.2.1.0", str(len(self.ifaces))))
        for it in self.ifaces:
            rows += it.rows()
        rows += catalyst_platform_rows(
            self.short,
            cpu_pct=gauge("core.cpu", 14, amp_abs=4, period=900),
            temp_c=gauge("core.temp", 39, amp_abs=1.5, period=1800),
            mem_used=1_912_602_624,
            mem_free=6_275_072_000,
        )
        return rows


class SwAccess(Device):
    """DC server-access switch — carries the classic-roster servers and the
    delivery shell; the CRC-error-storm incident (on the FIRST instance;
    replicas — see --access-switches — are steady-green extra server rows).

    healthy:  48 x 1G access ports + 2 x 10G uplinks, all green.
    degraded: uplink Te1/1/1 develops CRC errors (bad SFP/patch cable):
              ~0.04 % of inbound packets -> Interface WARN on error rate
              (defaults 0.01/0.1 %). Traffic still flows.
    broken:   the link dies: Te1/1/1 ifOperStatus -> down(2) => CRIT
              (discovered state was up); traffic fails over to Te1/1/2,
              whose load roughly doubles. Discover while HEALTHY — down
              ports are never discovered, and the target state is the one
              recorded at discovery.
    """

    uptime_offset = 87 * 86400
    sys_descr = (
        "Cisco IOS Software [Amsterdam], Catalyst L3 Switch Software "
        "(CAT9K_LITE_IOSXE), Version 17.3.4, RELEASE SOFTWARE (fc2), "
        "Copyright (c) 1986-2021 by Cisco Systems, Inc."
    )
    sys_objectid = ".1.3.6.1.4.1.9.1.2695"
    role = "net_switches"

    def __init__(self, num: int = 1) -> None:
        self.short = f"sw-access-{num:02d}"
        self.incident = num == 1  # one story; replicas stay green
        self.location = f"DC row {num}, server access"
        self.uptime_offset = (87 - 3 * num) * 86400
        super().__init__()
        rnd = random.Random(42 + num)  # distinct port mix per switch
        mac_seed = 2 if num == 1 else 16 + num  # unique MACs across replicas
        self.ifaces: list[Iface] = []
        acc_in = acc_out = 0.0
        for n in range(1, 49):  # 48 x 1G access ports
            if rnd.random() < 0.70:  # most ports lightly used
                base_in = rnd.uniform(0.1e6, 3e6) / 8
            else:  # a few noisy ones (backups, repl)
                base_in = rnd.uniform(5e6, 25e6) / 8
            base_out = base_in * rnd.uniform(0.3, 1.0)
            acc_in += base_in
            acc_out += base_out
            self.ifaces.append(
                Iface(
                    self.short,
                    n,
                    f"Gi1/0/{n}",
                    f"GigabitEthernet1/0/{n}",
                    "",
                    1000,
                    base_in,
                    base_out,
                    seed=mac_seed,
                )
            )
        # 2 x 10G uplinks to the core, ~balanced. Traffic conservation (a
        # network person WILL sum this): what the access ports receive from
        # the devices leaves via the uplinks and vice versa — so each
        # uplink's OUT ~= half the access-IN sum, and its IN ~= half the
        # access-OUT sum.
        self.up1 = Iface(
            self.short,
            49,
            "Te1/1/1",
            "TenGigabitEthernet1/1/1",
            "uplink sw-core-01",
            10000,
            acc_out * 0.51,
            acc_in * 0.50,
            seed=mac_seed,
        )
        self.up2 = Iface(
            self.short,
            50,
            "Te1/1/2",
            "TenGigabitEthernet1/1/2",
            "uplink sw-core-01",
            10000,
            acc_out * 0.49,
            acc_in * 0.50,
            seed=mac_seed,
        )
        self.ifaces += [self.up1, self.up2]

    def rows(self, now: float) -> list[tuple[str, str]]:
        state = self.state.get()
        # uplink 1: healthy -> clean; degraded -> CRC storm (~0.04 % of
        # ~130k pps inbound ~= 50 err/s, comfortably over the 0.01 % WARN,
        # under the 0.1 % CRIT); broken -> link down, failover to uplink 2
        pps_in = self.up1.in_bps / 700.0
        if state == "healthy":
            self.up1.oper, self.up1.err_rate, self.up1.rate_factor = 1, 0.0, 1.0
            self.up2.rate_factor = 1.0
        elif state == "degraded":
            m = min(1.0, self.state.degraded_minutes() / 2.0)  # storm builds ~2 min
            self.up1.oper = 1
            self.up1.err_rate = 0.0004 * pps_in * m
            self.up1.rate_factor = 1.0
            self.up2.rate_factor = 1.0
        else:  # broken: link down
            self.up1.oper = 2
            self.up1.err_rate = 0.0
            self.up2.rate_factor = 1.0 + 0.95 * self.state.ramp(2.0)
        rows = self.system_rows(now)
        rows.append((".1.3.6.1.2.1.2.1.0", str(len(self.ifaces))))
        for it in self.ifaces:
            rows += it.rows()
        rows += catalyst_platform_rows(
            self.short,
            cpu_pct=gauge(
                f"{self.short}.cpu", 9, amp_abs=3, period=1100, phase=sum(map(ord, self.short)) % 6
            ),
            temp_c=gauge(
                f"{self.short}.temp",
                43,
                amp_abs=1.5,
                period=2000,
                phase=sum(map(ord, self.short)) % 5,
            ),
            mem_used=1_204_570_112,
            mem_free=2_890_137_600,
        )
        return rows

    def status_extras(self) -> list[str]:
        state = self.state.get()
        if state == "degraded":
            pct = 0.04 * min(1.0, self.state.degraded_minutes() / 2.0)
            return [
                f"Te1/1/1 CRC storm: ~{pct:.3f} % of inbound packets in error "
                f"(WARN at 0.01 %, CRIT at 0.1 %)"
            ]
        if state == "broken":
            return [
                "Te1/1/1 DOWN (Interface 49 goes CRIT) — "
                f"Te1/1/2 carrying failover load (+{self.state.ramp(2.0) * 95:.0f} %)"
            ]
        return []


class RtWan(Device):
    """DC WAN head-end router (DC rack A2) — the saturation incident. Aggregates
    the leased lines to the fulfillment warehouses; each warehouse's own CPE
    router (rt-wh1-01 / rt-wh2-01) sits BEHIND this box, so the path is
    core -> rt-wan-01 -> rt-wh{1,2}-01 -> warehouse switch. (Not the internet
    edge.)

    healthy:  Gi0/1 (WAN uplink to the warehouse leased lines) runs at ~180 Mbit/s.
    degraded: a bulk transfer (inventory replication gone wrong) pushes it
              to ~600 Mbit/s; CPU follows to ~70 % — visible in every
              graph, nothing red yet.
    broken:   the link saturates ~940 Mbit/s; the CPU (process switching,
              QoS drops) climbs past the cisco_cpu defaults (WARN 80/CRIT 90)
              and output discards appear on the WAN port. One story: the
              red CPU points at the saturated WAN graph next to it.
    """

    short = "rt-wan-01"
    uptime_offset = 391 * 86400
    sys_descr = (
        "Cisco IOS Software, C2900 Software (C2900-UNIVERSALK9-M), "
        "Version 15.7(3)M5, RELEASE SOFTWARE (fc1), "
        "Copyright (c) 1986-2019 by Cisco Systems, Inc."
    )
    sys_objectid = ".1.3.6.1.4.1.9.1.1639"
    location = "DC rack A2"
    incident = True
    role = "net_routers"

    def __init__(self) -> None:
        super().__init__()
        self.lan = Iface(
            "rt-wan-01",
            1,
            "Gi0/0",
            "GigabitEthernet0/0",
            "LAN to sw-core-01 Te1/0/4",
            1000,
            24e6,
            21e6,
            seed=3,
        )
        self.wan = Iface(
            "rt-wan-01",
            2,
            "Gi0/1",
            "GigabitEthernet0/1",
            "WAN leased lines to warehouses (rt-wh1/rt-wh2)",
            1000,
            22.5e6,
            19e6,
            seed=3,
        )
        self.ifaces = [self.lan, self.wan]

    def _factor_cpu(self) -> tuple[float, float]:
        """(traffic factor vs healthy ~180 Mbit, cpu %) per state."""
        state = self.state.get()
        if state == "healthy":
            return 1.0, gauge("wan.cpu", 22, amp_abs=5, period=1000)
        if state == "degraded":
            m = min(1.0, self.state.degraded_minutes() / 3.0)
            return _lerp(1.0, 3.3, m), gauge("wan.cpu", _lerp(25, 68, m), amp_abs=4, period=800)
        r = self.state.ramp(3.0)
        return _lerp(3.3, 5.2, r), gauge("wan.cpu", _lerp(68, 93, r), amp_abs=2, period=700)

    def rows(self, now: float) -> list[tuple[str, str]]:
        factor, cpu = self._factor_cpu()
        self.wan.rate_factor = factor
        self.lan.rate_factor = _lerp(1.0, 1.15, min(1.0, factor / 5.0))
        # a saturated egress drops packets: output discards on the WAN port
        self.wan.disc_rate = 0.0 if factor < 4.5 else (factor - 4.5) * 120.0
        rows = self.system_rows(now)
        rows.append((".1.3.6.1.2.1.2.1.0", str(len(self.ifaces))))
        for it in self.ifaces:
            rows += it.rows()
        rows += [
            # classic cisco_cpu: cpmCPUTotal5minRev only, NO .2.* row (that
            # would flip detection to cisco_cpu_multiitem)
            (".1.3.6.1.4.1.9.9.109.1.1.1.1.8.1", str(int(round(cpu)))),
            # legacy cisco_mem pools (bytes used / free)
            (".1.3.6.1.4.1.9.9.48.1.1.1.2.1", "Processor"),
            (".1.3.6.1.4.1.9.9.48.1.1.1.5.1", "231972864"),
            (".1.3.6.1.4.1.9.9.48.1.1.1.6.1", "289406976"),
            (".1.3.6.1.4.1.9.9.48.1.1.1.2.2", "I/O"),
            (".1.3.6.1.4.1.9.9.48.1.1.1.5.2", "35651584"),
            (".1.3.6.1.4.1.9.9.48.1.1.1.6.2", "31457280"),
        ]
        rows += envmon_temp_rows(
            "chassis",
            gauge("wan.temp", 46 + (cpu - 22) * 0.12, amp_abs=1.2, period=1600),
            threshold=65,
        )
        rows += envmon_fan_psu_rows(fans=["Fan 1"], psus=["PS1 Normal"])
        return rows

    def status_extras(self) -> list[str]:
        factor, cpu = self._factor_cpu()
        mbit = 180 * factor
        out = [f"WAN Gi0/1 at ~{mbit:.0f} Mbit/s of 1000, CPU ~{cpu:.0f} % (WARN 80 / CRIT 90)"]
        if self.wan.disc_rate > 0:
            out.append(f"output discards on Gi0/1: ~{self.wan.disc_rate:.0f}/s")
        return out


class Ups(Device):
    """APC Smart-UPS — steady-green infrastructure corroboration. Its
    management card plugs into the server-access switch (a UPS mgmt NIC on a
    10G core port would be odd)."""

    short = "ups-01"
    parent = "sw-access-01"
    uptime_offset = 402 * 86400
    sys_descr = (
        "APC Web/SNMP Management Card (MB:v4.1.0 PF:v6.9.6 "
        "PN:apc_hw05_aos_696.bin AF1:v6.9.6 "
        "AN1:apc_hw05_sumx_696.bin MN:AP9631 HR:05) "
        "(Embedded PowerNet SNMP Agent SW v2.2 compatible)"
    )
    sys_objectid = ".1.3.6.1.4.1.318.1.3.27"
    location = "DC rack A2, bottom"
    incident = False
    role = "net_ups"

    def rows(self, now: float) -> list[tuple[str, str]]:
        # apc_symmetra family (detect: sysObjectID startswith .1.3.6.1.4.1.318;
        # the RFC1628 ups_* plugins key on sysObjectID too and never fire for
        # .318). Values wander smoothly and stay clear of every default level:
        # capacity lower-levels 95/80 -> keep ~100; battery temp 50/60 -> ~25;
        # output voltage LOWER levels (220, 220) -> keep >= 226; battery
        # status 2=normal, output status 2=onLine, replace indicator
        # 1=noReplace, diag results 1=ok. The last-diagnostics date must be a
        # PARSEABLE MM/DD/YYYY older than a day (a fresher one arms the
        # post-calibration capacity check) -> rendered dynamically ~5 weeks
        # back. upsBasicStateOutputState is the documented healthy bitmask
        # (self-test bit 1<<35 clear).
        cap = min(100, int(round(gauge("ups.cap", 99.4, amp_abs=0.7, period=2400))))
        temp = int(round(gauge("ups.temp", 25, amp_abs=1.5, period=1800)))
        load = int(round(gauge("ups.load", 37, amp_abs=4, period=1300)))
        int(round(gauge("ups.vout", 231, amp_abs=2, period=1600)))
        volt_in = int(round(gauge("ups.vin", 230, amp_abs=2, period=1900)))
        runtime_ticks = (
            int(gauge("ups.runtime", 7200, amp_abs=350, period=2100)) * 100
        )  # TimeTicks (1/100 s)
        amps = max(1, int(round(load / 9)))
        diag = time.strftime("%m/%d/%Y", time.localtime(now - 37 * 86400))
        state_bits = "0001010000000000001000000000000000000000000000000000000000000000"
        rows = self.system_rows(now)
        rows += [
            (".1.3.6.1.4.1.318.1.1.1.2.1.1.0", "2"),  # battery: normal
            (".1.3.6.1.4.1.318.1.1.1.2.2.1.0", str(cap)),
            (".1.3.6.1.4.1.318.1.1.1.2.2.2.0", str(temp)),
            (".1.3.6.1.4.1.318.1.1.1.2.2.3.0", str(runtime_ticks)),
            (".1.3.6.1.4.1.318.1.1.1.2.2.4.0", "1"),  # no replace
            (".1.3.6.1.4.1.318.1.1.1.2.2.6.0", "1"),  # battery packs
            (".1.3.6.1.4.1.318.1.1.1.2.2.9.0", str(amps)),
            (".1.3.6.1.4.1.318.1.1.1.3.2.1.0", str(volt_in)),
            (".1.3.6.1.4.1.318.1.1.1.4.1.1.0", "2"),  # output: onLine
            # output-phase rows (.4.2.1/.4.2.3/.4.2.4) intentionally absent:
            # parse_apc_symmetra_output crashes on the 3.0.0 dev branch
            # (ElPhase.from_dict re-parses ReadingWithState -> TypeError,
            # introduced in c1802b42504) — restore once fixed upstream
            (".1.3.6.1.4.1.318.1.1.1.7.2.3.0", "1"),  # self test: ok
            (".1.3.6.1.4.1.318.1.1.1.7.2.4.0", diag),
            (".1.3.6.1.4.1.318.1.1.1.7.2.6.0", "1"),  # calibration: ok
            (".1.3.6.1.4.1.318.1.1.1.8.1.0", "1"),  # comm status ok
            (".1.3.6.1.4.1.318.1.1.1.11.1.1.0", state_bits),
        ]
        return rows


# --------------------------------------------------------------------------- #
#  Replay layer: anonymized REAL walks (snmp/walklib/, made by curate_walks.py)
#  replayed as additional estate devices. A walk file is loaded ONCE per model
#  into a segment template (static text chunks + sparse dynamic slots), then
#  each instance renders with its own identity (sysName/Location), advancing
#  uptime, and live interface counters whose base rate is derived from the
#  RECORDED counter value over the RECORDED uptime — so a busy recorded port
#  stays busy and a dead one stays dead. Error/discard counters stay frozen
#  (rate 0 -> the interface checks stay green). Everything else is served
#  exactly as recorded: real device values are the realism.
# --------------------------------------------------------------------------- #
_UPTIME_OID = ".1.3.6.1.2.1.1.3.0"
_HRUPTIME_OID = ".1.3.6.1.2.1.25.1.1.0"
# ifTable 32-bit + ifXTable 64-bit counter columns -> logical counter key
_IF32 = {"10": "ifin", "11": "ifinu", "16": "ifout", "17": "ifoutu"}
_IFHC = {"6": "ifin", "7": "ifinu", "10": "ifout", "11": "ifoutu"}
_HRCPU_PREFIX = ".1.3.6.1.2.1.25.3.3.1.2."
_PAGES_PREFIX = ".1.3.6.1.2.1.43.10.2.1.4."
_HP_CPU_OID = ".1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0"  # hpSwitchCpuStat (%)
_APC_DIAG_DATE_OID = ".1.3.6.1.4.1.318.1.1.1.7.2.4.0"  # last self test (M/D/Y)


def _ticks(value: str) -> int:
    """TimeTicks in either raw-int or human 'd:hh:mm:ss.cc' walk format."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    if len(parts) == 4:
        d, h, m = (int(p) for p in parts[:3])
        s = float(parts[3])
        return int((((d * 24 + h) * 60 + m) * 60 + s) * 100)
    return 0


class ReplayModel:
    """One parsed walk file, shared by all its instances."""

    _cache: dict[str, "ReplayModel"] = {}

    @classmethod
    def load(cls, name: str, walklib: str) -> "ReplayModel":
        if name not in cls._cache:
            cls._cache[name] = cls(name, os.path.join(walklib, f"{name}.walk"))
        return cls._cache[name]

    @classmethod
    def loaded_count(cls) -> int:
        return len(cls._cache)

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        # segments: static text blocks interleaved with dynamic slots
        # slot = (kind, oid, key, recorded_int)
        self.segments: list[str | tuple[Any, ...]] = []
        self.rates: dict[str, float] = {}  # counter key -> base rate/s
        self.uptime_rec = 90 * 86400  # seconds, fallback

        rows: list[tuple[str, str]] = []
        with open(path) as f:
            for line in f:
                oid, _, value = line.rstrip("\n").partition(" ")
                rows.append((oid, value))
        for oid, value in rows:
            if oid == _UPTIME_OID:
                self.uptime_rec = max(7 * 86400, _ticks(value) / 100.0)

        static: list[str] = []

        def flush() -> None:
            if static:
                self.segments.append("".join(static))
                static.clear()

        for oid, value in rows:
            slot = self._classify(oid, value)
            if slot is None:
                static.append(f"{oid} {value}\n")
            else:
                flush()
                self.segments.append(slot)
        flush()

    def _classify(self, oid: str, value: str) -> tuple[Any, ...] | None:
        if oid == ".1.3.6.1.2.1.1.5.0":
            return ("sysname", oid, None, 0)
        if oid == ".1.3.6.1.2.1.1.6.0":
            return ("syslocation", oid, None, 0)
        if oid in (_UPTIME_OID, _HRUPTIME_OID):
            return ("uptime", oid, None, _ticks(value))
        if oid == _HP_CPU_OID and value.isdigit():
            return ("hrcpu", oid, "hp", int(value))
        # UCD ssCpuRaw*/ssIORaw* jiffie counters: if these stay static, the
        # ucd_cpu_util check raises IgnoreResults on the zero jiffie delta
        # EVERY cycle and the service pends forever — advance them at the
        # recorded average so the recorded utilization ratio is preserved
        if oid.startswith(".1.3.6.1.4.1.2021.11.") and value.isdigit():
            col = oid.split(".")[-2]
            if col.isdigit() and 50 <= int(col) <= 66:
                key = f"ucd.{col}"
                self._note_rate(key, int(value))
                return ("ctr32", oid, key, int(value))
        if oid == _APC_DIAG_DATE_OID:
            # a static date would age into the self-test levels eventually —
            # render "~5 weeks ago" dynamically like the synthetic UPS does
            return ("apcdate", oid, None, 0)
        parts = oid.rsplit(".", 1)
        if oid.startswith(".1.3.6.1.2.1.2.2.1."):
            col, index = oid.split(".")[-2], parts[1]
            if col in _IF32 and value.isdigit():
                key = f"{_IF32[col]}.{index}"
                self._note_rate(key, int(value))
                return ("ctr32", oid, key, int(value))
        if oid.startswith(".1.3.6.1.2.1.31.1.1.1."):
            col, index = oid.split(".")[-2], parts[1]
            if col in _IFHC and value.isdigit():
                key = f"{_IFHC[col]}.{index}"
                self._note_rate(key, int(value))
                return ("ctr64", oid, key, int(value))
        if oid.startswith(_HRCPU_PREFIX) and value.isdigit():
            return ("hrcpu", oid, oid[len(_HRCPU_PREFIX) :], int(value))
        if oid.startswith(_PAGES_PREFIX) and value.isdigit():
            key = f"pages.{oid[len(_PAGES_PREFIX) :]}"
            self._note_rate(key, int(value))
            return ("ctr32", oid, key, int(value))
        return None

    def _note_rate(self, key: str, recorded: int) -> None:
        # base rate: the average the REAL device sustained over its recorded
        # uptime (32-bit wraps make this an underestimate — fine, stays green)
        cap = 25e6 if "if" in key and key.split(".")[0].endswith("in") else 25e6
        rate = min(cap, recorded / self.uptime_rec)
        prev = self.rates.get(key)
        self.rates[key] = max(prev, rate) if prev is not None else rate


class ReplayDevice(Device):
    """One estate instance of a replayed walk model."""

    incident = False

    def __init__(
        self, short: str, model: ReplayModel, role: str, location: str, parent: str | None
    ) -> None:
        self.short = short
        self.role = role
        self.location = location
        self.parent = parent
        self.model = model
        super().__init__()
        rnd = random.Random(f"{short}:replay-v1")
        self.rate_jit = rnd.uniform(0.7, 1.3)
        self.uptime_extra = rnd.uniform(0, 120) * 86400  # instances differ
        self.phase = rnd.uniform(0, 6.28)
        self._counters: dict[str, Counter] = {}

    def _counter(self, key: str, recorded: int) -> Counter:
        c = self._counters.get(key)
        if c is None:
            c = Counter(
                f"{self.short}.{key}",
                phase=self.phase + (hash(key) % 63) / 10.0,
                amp=0.30,
                start=float(recorded),
            )
            self._counters[key] = c
        return c

    # kinds whose value moves between polls — everything else (incl. sysname/
    # syslocation and all the recorded static lines) is fixed for an instance
    _DYNAMIC_KINDS = frozenset({"uptime", "hrcpu", "apcdate", "ctr32", "ctr64"})

    def _slot_value(
        self, seg: tuple[Any, ...], elapsed: float, now: float, sampled: dict[str, int]
    ) -> str:
        kind, _oid, key, rec = seg
        if kind == "sysname":
            return self.fqdn
        if kind == "syslocation":
            return self.location
        if kind == "uptime":
            return str(int(rec + elapsed * 100) % U32)
        if kind == "hrcpu":
            v = gauge(f"{self.short}.hrcpu.{key}", rec, amp_abs=3.0, phase=self.phase, period=900)
            return str(max(1, min(97, int(round(v)))))
        if kind == "apcdate":
            return time.strftime("%m/%d/%Y", time.localtime(now - 37 * 86400))
        # ctr32 / ctr64
        if key not in sampled:
            rate = self.model.rates.get(key, 0.0) * self.rate_jit
            sampled[key] = self._counter(key, rec).sample(rate)
        return str(sampled[key] % U32 if kind == "ctr32" else sampled[key])

    def walk(self, now: float) -> str:
        elapsed = now - START + self.uptime_extra
        sampled: dict[str, int] = {}
        parts: list[str] = []
        for seg in self.model.segments:
            if isinstance(seg, str):
                parts.append(seg)
            else:
                parts.append(f"{seg[1]} {self._slot_value(seg, elapsed, now, sampled)}\n")
        return "".join(parts)

    def dynamic_rows(self, now: float) -> list[tuple[str, str]]:
        """Only the OIDs whose value changes between polls (counters, uptime,
        cpu, apc date) — ~2-4 % of the walk. Lets the responder refresh a
        cached table in place instead of rebuilding all ~17k OIDs each poll."""
        elapsed = now - START + self.uptime_extra
        sampled: dict[str, int] = {}
        return [
            (seg[1], self._slot_value(seg, elapsed, now, sampled))
            for seg in self.model.segments
            if not isinstance(seg, str) and seg[0] in self._DYNAMIC_KINDS
        ]

    def rows(self, now: float) -> list[tuple[str, str]]:  # pragma: no cover
        raise NotImplementedError("ReplayDevice renders via walk()")


# --------------------------------------------------------------------------- #
#  Replay roster — the SNMP bulk of the 300-host estate (see FLEET.md).
#  (count, name pattern, model, role, location pattern, parent)
#  Roles are folder-taxonomy keys consumed by deploy/cmk_setup.py.
# --------------------------------------------------------------------------- #
REPLAY_ROSTER: list[tuple[int, str, str, str, str, str | None]] = [
    # --- data center fabric: core -> distribution pair -> ToR + leaves --------
    # Only the distribution switches uplink to the core; everything else hangs
    # off a distribution switch (ToR carry the servers; storage/mgmt/power/env
    # aggregate on the pair). Keeps the 12-port core's fan-out realistic.
    (
        2,
        "sw-dc-dist-{n:02d}",
        "hp-5406r",
        "net_switches",
        "DC row {n}, end of row (distribution)",
        "sw-core-01",
    ),
    (
        6,
        "sw-dc-tor-{n:02d}",
        "aruba-6200f",
        "net_switches",
        "DC rack A{n}, top of rack",
        "sw-dc-dist-01",
    ),
    (
        2,
        "sw-dc-tor-{nn:02d}",
        "huawei-s6730",
        "net_switches",
        "DC rack B{n}, top of rack",
        "sw-dc-dist-02",
    ),
    # dedicated OOB/management switch: iDRACs, rack PDUs and environment
    # sensors live on the management network, not on the production fabric
    (
        1,
        "sw-dc-oob-{n:02d}",
        "hp-2530",
        "net_switches",
        "DC comms room, OOB management",
        "sw-dc-dist-01",
    ),
    (2, "fw-{n:02d}", "fortigate", "net_firewalls", "DC rack A1 (HA pair, unit {n})", "sw-core-01"),
    (1, "fw-dmz-{n:02d}", "cisco-asa", "net_firewalls", "DC rack A1, DMZ tier", "sw-core-01"),
    (
        2,
        "lb-{n:02d}",
        "kemp-lb",
        "net_loadbalancers",
        "DC rack A2 (HA pair, unit {n})",
        "sw-dc-dist-01",
    ),
    (2, "nas-{n:02d}", "synology-nas", "storage", "DC rack B1", "sw-dc-dist-02"),
    (2, "san-fc-{n:02d}", "brocade-fc", "storage", "DC rack B2, SAN fabric {n}", "sw-dc-dist-02"),
    (16, "oob-idrac-{n:02d}", "idrac", "mgmt", "DC rack A{n} (iDRAC)", "sw-dc-oob-01"),
    (
        1,
        "ntp-gps-{n:02d}",
        "meinberg-ntp",
        "infrastructure",
        "DC comms room, GPS antenna on roof",
        "sw-dc-dist-02",
    ),
    (3, "ups-dc-{n:02d}", "apc-symmetra", "net_ups", "DC power room, feed {n}", "sw-dc-oob-01"),
    (4, "pdu-dc-{n:02d}", "apc-pdu", "net_ups", "DC rack A{n}, rack PDU", "sw-dc-oob-01"),
    (2, "pdu-dc-{nn:02d}", "raritan-pdu", "net_ups", "DC rack B{n}, rack PDU", "sw-dc-oob-01"),
    (2, "pdu-dc-{nnn:02d}", "gude-pdu", "net_ups", "DC rack C{n}, rack PDU", "sw-dc-oob-01"),
    (6, "env-dc-{n:02d}", "akcp-sensor", "net_ups", "DC row {n}, hot aisle", "sw-dc-oob-01"),
    (2, "env-dc-{nn:02d}", "avtech-ra3s", "net_ups", "DC comms room {n}", "sw-dc-oob-01"),
    # --- HQ office: core -> HQ distribution -> floor switches + leaves --------
    (
        1,
        "sw-hq-dist-{n:02d}",
        "hp-5406r",
        "net_switches",
        "HQ comms room, distribution",
        "sw-core-01",
    ),
    (7, "sw-hq-f{n:02d}", "aruba-2930f", "net_switches", "HQ floor {n}, IDF {n}A", "sw-hq-dist-01"),
    (7, "sw-hq-f{nn:02d}", "hp-2530", "net_switches", "HQ floor {n}, IDF {n}B", "sw-hq-dist-01"),
    (
        2,
        "wlc-{n:02d}",
        "extreme-wlc",
        "net_wifi",
        "HQ comms room (controller {n})",
        "sw-hq-dist-01",
    ),
    # a floor's printer plugs into that floor's switch (index matches 1:1)
    (
        6,
        "prt-hq-{n:02d}",
        "printer-ricoh",
        "printers",
        "HQ floor {n}, print room",
        "sw-hq-f{n:02d}",
    ),
    (
        6,
        "prt-hq-{nn:02d}",
        "printer-canon",
        "printers",
        "HQ floor {n}, print room",
        "sw-hq-f{n:02d}",
    ),
    (1, "ups-hq-{n:02d}", "apc-symmetra", "net_ups", "HQ comms room", "sw-hq-dist-01"),
    (2, "pdu-hq-{n:02d}", "gude-pdu", "net_ups", "HQ comms room, rack {n}", "sw-hq-dist-01"),
    (1, "env-hq-{n:02d}", "avtech-ra3s", "net_ups", "HQ comms room", "sw-hq-dist-01"),
    # --- warehouse 1 (CPE router rt-wh1-01, behind the DC head-end rt-wan-01) ---
    # Path: device -> hall switch -> rt-wh1-01 (local CPE) -> rt-wan-01 -> core.
    # Only the three hall switches plug into the CPE (a small Lancom has 4-5
    # LAN ports); the mezzanine switches daisy-chain off hall 1, the packing-
    # line printers plug into their line's switch, and the comms-room power/
    # env gear shares hall 1's switch.
    (1, "rt-wh1-{n:02d}", "lancom-router", "net_routers", "warehouse 1, comms room", "rt-wan-01"),
    (3, "wh1-sw-{n:02d}", "procurve-2510", "net_switches", "warehouse 1, hall {n}", "rt-wh1-01"),
    (2, "wh1-sw-{nn:02d}", "hp-2530", "net_switches", "warehouse 1, mezzanine {n}", "wh1-sw-01"),
    (
        4,
        "wh1-prt-{n:02d}",
        "printer-zebra",
        "printers",
        "warehouse 1, packing line {n}",
        "wh1-sw-{n:02d}",
    ),
    (1, "wh1-ups-{n:02d}", "apc-symmetra", "net_ups", "warehouse 1, comms room", "wh1-sw-01"),
    (1, "wh1-pdu-{n:02d}", "raritan-pdu", "net_ups", "warehouse 1, comms room", "wh1-sw-01"),
    (1, "wh1-env-{n:02d}", "akcp-sensor", "net_ups", "warehouse 1, comms room", "wh1-sw-01"),
    # --- warehouse 2 (CPE router rt-wh2-01, behind the DC head-end rt-wan-01) ---
    (1, "rt-wh2-{n:02d}", "lancom-router", "net_routers", "warehouse 2, comms room", "rt-wan-01"),
    (3, "wh2-sw-{n:02d}", "procurve-2510", "net_switches", "warehouse 2, hall {n}", "rt-wh2-01"),
    (2, "wh2-sw-{nn:02d}", "hp-2530", "net_switches", "warehouse 2, mezzanine {n}", "wh2-sw-01"),
    (
        4,
        "wh2-prt-{n:02d}",
        "printer-zebra",
        "printers",
        "warehouse 2, packing line {n}",
        "wh2-sw-{n:02d}",
    ),
    (1, "wh2-ups-{n:02d}", "apc-symmetra", "net_ups", "warehouse 2, comms room", "wh2-sw-01"),
    (1, "wh2-pdu-{n:02d}", "raritan-pdu", "net_ups", "warehouse 2, comms room", "wh2-sw-01"),
    (1, "wh2-env-{n:02d}", "akcp-sensor", "net_ups", "warehouse 2, comms room", "wh2-sw-01"),
    # --- internet edge ----------------------------------------------------------
    (
        1,
        "rt-inet-{n:02d}",
        "lancom-router",
        "net_routers",
        "DC rack A1, internet backup line",
        "sw-core-01",
    ),
]


def build_replay_devices(walklib: str) -> list[ReplayDevice]:
    """Expand the roster. Name patterns share a prefix across models
    ({n} starts at 1, {nn} continues after the previous entry with the same
    prefix, {nnn} after that) so e.g. pdu-dc-01..08 mixes three models.
    The parent column may also carry a "{n:02d}" pattern — substituted with
    the SAME running index as the device — for per-instance parents (e.g.
    floor printer prt-hq-07 -> its floor switch sw-hq-f07)."""
    devices: list[ReplayDevice] = []
    next_index: dict[str, int] = {}
    for count, pattern, model_name, role, loc_pattern, parent in REPLAY_ROSTER:
        prefix = pattern.split("{")[0]
        start = next_index.get(prefix, 1)
        try:
            model = ReplayModel.load(model_name, walklib)
        except OSError as exc:
            print(
                f"[replay] WARN: model {model_name} unavailable ({exc}) — "
                f"skipping {count}x {prefix}*"
            )
            continue
        for i in range(count):
            n = start + i
            short = (
                pattern.replace("{n:02d}", f"{n:02d}")
                .replace("{nn:02d}", f"{n:02d}")
                .replace("{nnn:02d}", f"{n:02d}")
            )
            location = loc_pattern.replace("{n}", str(n))
            parent_n = parent.replace("{n:02d}", f"{n:02d}") if parent else parent
            devices.append(ReplayDevice(short, model, role, location, parent_n))
        next_index[prefix] = start + count
    return devices


DEVICES: list[Device] = []


# --------------------------------------------------------------------------- #
#  Persistence (counters + per-device incident state; restart-invisible)
# --------------------------------------------------------------------------- #
def save_state() -> None:
    if not STATE_FILE:
        return
    data = {
        "version": 1,
        "start": START,
        "devices": {d.short: d.state.dump() for d in DEVICES},
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
    global START
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[state] load failed ({exc}) — starting fresh")
        return
    START = data.get("start", START)
    for d in DEVICES:
        if d.short in data.get("devices", {}):
            d.state.restore(data["devices"][d.short])
    saved = data.get("counters", {})
    restored = 0
    for name, c in _ALL_COUNTERS.items():
        if name in saved:
            c.acc, c.last = saved[name]
            restored += 1
    print(f"[state] restored {restored}/{len(_ALL_COUNTERS)} counters, uptime continuous")


# --------------------------------------------------------------------------- #
#  Walk writer loop
# --------------------------------------------------------------------------- #
WALKS_DIR = ""
TRANSPORT = "walk"  # set to "snmp" by _main_snmp
SNMP_PORT: int | None = None


def write_walks() -> None:
    now = time.time()
    for dev in DEVICES:
        path = os.path.join(WALKS_DIR, dev.fqdn)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(dev.walk(now))
        os.replace(tmp, path)  # atomic: a poll never sees a torn file


def render_loop() -> None:
    while True:
        try:
            write_walks()
            save_state()
        except OSError as exc:
            print(f"[render] write failed: {exc}")
        time.sleep(RENDER_INTERVAL)


def auto_break_watchdog() -> None:
    while True:
        time.sleep(5)
        for dev in DEVICES:
            if (
                dev.incident
                and dev.state.get() == "degraded"
                and dev.state.since_seconds() >= AUTO_BREAK_AFTER_MIN * 60
            ):
                dev.state.set("broken")
                write_walks()
                print(
                    f"[ctl] {dev.short} -> BROKEN (auto: degraded for {AUTO_BREAK_AFTER_MIN:g} min)"
                )


# --------------------------------------------------------------------------- #
#  Control plane: combined /admin panel + curl API
# --------------------------------------------------------------------------- #
STATE_COLORS = {"healthy": "#2e7d32", "degraded": "#f9a825", "broken": "#c62828"}

DEVICE_EFFECTS = {
    "sw-access-01": {
        "healthy": [
            "all 50 interfaces green, uplinks Te1/1/1 + Te1/1/2 balanced",
            "discover the host in THIS state (down ports are never "
            "discovered; target state = state at discovery)",
        ],
        "degraded": [
            "Te1/1/1 (Interface 49): CRC error storm ~0.04 % of packets "
            "-> WARN (defaults 0.01/0.1 %)",
            "traffic still flows — the classic dying-SFP picture",
        ],
        "broken": [
            "Te1/1/1 goes DOWN -> Interface 49 CRIT (state != discovered)",
            "Te1/1/2 load roughly doubles (failover) — visible in graphs",
        ],
    },
    "rt-wan-01": {
        "healthy": ["warehouse WAN Gi0/1 ~180 Mbit/s, CPU ~22 %, all green"],
        "degraded": [
            "WAN ramps to ~600 Mbit/s (runaway inventory "
            "replication), CPU ~70 % — graphs move, nothing red yet"
        ],
        "broken": [
            "WAN saturates ~940 Mbit/s of 1G",
            "CPU utilization climbs ~93 % -> CRIT (defaults 80/90)",
            "output discards appear on Gi0/1 (graph corroboration)",
        ],
    },
}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _admin_page() -> str:
    cards = []
    steady = [d for d in DEVICES if not d.incident]
    for dev in DEVICES:
        if not dev.incident:
            continue  # rendered compactly below
        state = dev.state.get()
        color = STATE_COLORS[state]
        extras = "".join(f"<div class='extra'>{e}</div>" for e in dev.status_extras())
        effects = DEVICE_EFFECTS.get(dev.short, {})
        state_cards = []
        for action, target in ACTION_TO_STATE.items():
            tcolor = STATE_COLORS[target]
            lis = "".join(f"<li>{e}</li>" for e in effects.get(target, []))
            btn = (
                "<span class='btn current'>current</span>"
                if target == state
                else f"<a class='btn' style='background:{tcolor}' "
                f"href='/admin/{dev.short}/{action}?ui=1'>&rarr; {action}</a>"
            )
            state_cards.append(
                f"<div class='card{' active' if target == state else ''}' "
                f"style='border-color:{tcolor}'>"
                f"<h3 style='color:{tcolor}'>{target.upper()}</h3>"
                f"<ul>{lis}</ul>{btn}</div>"
            )
        auto = ""
        if state == "degraded" and AUTO_BREAK_AFTER_MIN > 0:
            left = max(0.0, AUTO_BREAK_AFTER_MIN * 60 - dev.state.since_seconds())
            auto = f"<div class='extra'>auto-escalates to BROKEN in {_fmt_duration(left)}</div>"
        cards.append(
            f"<div class='dev' style='border-color:{color}'>"
            f"<h2>{dev.fqdn}</h2>"
            f"<span class='badge' style='background:{color}'>{state.upper()}</span>"
            f" <span class='since'>for {_fmt_duration(dev.state.since_seconds())}</span>"
            f"{extras}{auto}<div class='cards'>{''.join(state_cards)}</div></div>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>Meridian Retail — network estate control</title>
<style>
 body {{ background:#1a1d21; color:#d8dee4; font-family:system-ui,sans-serif;
        margin:2rem auto; max-width:76rem; padding:0 1rem; }}
 h1 {{ font-weight:600; font-size:1.3rem; color:#9aa4af; }}
 h1 b {{ color:#d8dee4; }}
 .dev {{ border:2px solid #333; border-radius:.6rem; padding:1rem 1.2rem;
        margin-top:1.2rem; background:#22262b; }}
 .dev h2 {{ margin:.1rem 0 .5rem; font-size:1.05rem; }}
 .badge {{ display:inline-block; padding:.25rem .8rem; border-radius:.4rem;
          color:#fff; font-weight:700; letter-spacing:.04em; }}
 .since {{ color:#9aa4af; }}
 .tag {{ color:#9aa4af; margin:.4rem 0 0; }}
 .extra {{ color:#f9a825; margin-top:.4rem; }}
 .cards {{ display:flex; gap:.8rem; margin-top:.9rem; flex-wrap:wrap; }}
 .card {{ flex:1 1 15rem; border:2px solid #333; border-radius:.5rem;
         padding:.7rem .9rem; background:#1e2226; opacity:.85; }}
 .card.active {{ opacity:1; background:#262b31; }}
 .card h3 {{ margin:.1rem 0 .3rem; font-size:.95rem; }}
 .card ul {{ padding-left:1.1rem; margin:.3rem 0 .8rem; }}
 .card li {{ margin:.2rem 0; font-size:.85rem; }}
 .btn {{ display:inline-block; padding:.35rem .9rem; border-radius:.4rem;
        color:#fff; text-decoration:none; font-weight:600; font-size:.9rem; }}
 .btn.current {{ background:#444; color:#aaa; cursor:default; }}
 .foot {{ margin-top:2rem; color:#666; font-size:.85rem; }}
</style></head><body>
 <h1>network estate control — <b>SNMP walk simulator</b>
 <span style="color:#555">(auto-refreshes every 5 s)</span></h1>
 {"".join(cards)}
 {_steady_section(steady)}
 <div class="foot">walks: {WALKS_DIR} (rewritten every {RENDER_INTERVAL:g} s)
  · curl API: /admin/&lt;device&gt;/degrade|break|heal · / (JSON status)<br>
  discover hosts in Checkmk while HEALTHY — down interfaces are never
  discovered, and the interface target state is recorded at discovery.</div>
</body></html>"""


def _steady_section(steady: list[Device]) -> str:
    """Compact chip grid for the (many) steady-green devices."""
    if not steady:
        return ""
    by_role: dict[str, list[Device]] = {}
    for d in steady:
        by_role.setdefault(d.role, []).append(d)
    blocks = []
    for role in sorted(by_role):
        chips = " ".join(
            f"<span class='chip' title='{d.location}'>{d.short}</span>"
            for d in sorted(by_role[role], key=lambda x: x.short)
        )
        blocks.append(f"<div class='crole'><b>{role}</b> ({len(by_role[role])})<br>{chips}</div>")
    return (
        f"<h2 style='color:#9aa4af;font-size:1.05rem;margin-top:1.6rem'>"
        f"steady-green devices — {len(steady)} "
        f"<span style='color:#2e7d32;font-size:.85rem'>no toggles</span></h2>"
        "<style>.chip{display:inline-block;background:#22313f;color:#9fc59f;"
        "border-radius:.25rem;padding:.06rem .4rem;margin:.12rem;"
        "font-size:.78rem}.crole{margin:.5rem 0;color:#9aa4af}</style>" + "".join(blocks)
    )


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "netsim-ctl/1.0"

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

        if path == "/admin/shutdown":
            # localhost demo tool: lets estate.py stop a netsim that runs as
            # the site user without needing sudo again
            save_state()
            self._send(200, {"shutdown": True})
            threading.Thread(
                target=lambda: (time.sleep(0.3), os._exit(0)),  # noqa: SLF001
                daemon=True,
            ).start()
            return None

        if path.startswith("/admin/"):
            parts = path[len("/admin/") :].split("/")
            if len(parts) == 2 and parts[1] in ACTION_TO_STATE:
                dev = next((d for d in DEVICES if d.short == parts[0]), None)
                if dev is None or not dev.incident:
                    return self._send(404, {"error": f"unknown device {parts[0]}"})
                dev.state.set(ACTION_TO_STATE[parts[1]])
                write_walks()  # take effect on the very next poll
                save_state()
                print(f"[ctl] {dev.short} -> {dev.state.get().upper()}")
                if "ui=1" in query:
                    self.send_response(303)
                    self.send_header("Location", "/admin")
                    self.end_headers()
                    return None
                return self._send(200, {"device": dev.short, "state": dev.state.get()})
            return self._send(404, {"error": "unknown action"})

        return self._send(
            200,
            {
                "devices": {
                    d.short: {
                        "fqdn": d.fqdn,
                        "state": d.state.get(),
                        "in_state_for_s": round(d.state.since_seconds(), 1),
                        "incident": d.incident,
                        "extras": d.status_extras(),
                        "role": d.role,
                        "parent": d.parent,
                        "location": d.location,
                        # live SNMP: all devices share one ip:port; the community
                        # (= the device short name) is what routes a poll to a device
                        "community": d.short if TRANSPORT == "snmp" else None,
                    }
                    for d in DEVICES
                },
                "transport": TRANSPORT,
                "snmp_port": SNMP_PORT,
                "walks_dir": WALKS_DIR,
                "render_interval_s": RENDER_INTERVAL,
                "toggles": [
                    f"/admin/{d.short}/{a}" for d in DEVICES if d.incident for a in ACTION_TO_STATE
                ],
                "ui": "/admin",
            },
        )


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def resolve_walks_dir(args: argparse.Namespace) -> str:
    if args.walks_dir:
        return args.walks_dir
    if args.site:
        return f"/omd/sites/{args.site}/var/check_mk/snmpwalks"
    if os.environ.get("OMD_ROOT"):
        return os.path.join(os.environ["OMD_ROOT"], "var/check_mk/snmpwalks")
    sys.exit("need --walks-dir, --site, or a site context ($OMD_ROOT)")


def assemble_devices(args: argparse.Namespace) -> list[Device]:
    """Build the device list from the flags (shared by both transports)."""
    devs: list[Device] = [SwCore()]
    devs += [SwAccess(n) for n in range(1, max(1, args.access_switches) + 1)]
    devs += [RtWan(), Ups()]
    if args.fleet:
        replay = build_replay_devices(args.walklib)
        devs += replay
        print(
            f"[boot] replay fleet: {len(replay)} devices from "
            f"{ReplayModel.loaded_count()} walk models ({args.walklib})"
        )
    if args.devices.strip():
        wanted = {d.strip() for d in args.devices.split(",") if d.strip()}
        devs = [d for d in devs if d.short in wanted]
        if not devs:
            sys.exit(f"--devices matched nothing (wanted: {sorted(wanted)})")
    return devs


# per-device rendered-table cache (keyed by community == device short name): a
# Checkmk walk fires HUNDREDS of GETBULKs in a burst; cache the built Table long
# enough that the whole walk reuses one snapshot (no mid-walk rebuild stalls)
# yet well under the ~60s check interval so consecutive polls still see advanced
# counters. Big replay devices (17k OIDs) make the build cost real — this keeps
# it to once per walk per device.
_TABLE_CACHE: dict[str, list[Any]] = {}  # community -> [built_or_refreshed_ts, Table]
_TABLE_TTL = 15.0


def _main_snmp(args: argparse.Namespace) -> None:
    """Serve SNMP live on ONE UDP port, routing to a device by its (unique)
    community string — so a single port ports-maps into a normal container
    (like the gateway), no --network host for per-device IPs. No sudo, no site
    filesystem. The community IS the device's short name."""
    global DEVICES, SNMP_PORT, TRANSPORT
    from snmpserver import SnmpServer, Table  # local: walk mode needn't import

    TRANSPORT = "snmp"
    SNMP_PORT = args.snmp_port
    DEVICES = assemble_devices(args)
    by_comm = {d.short: d for d in DEVICES}  # community == device short name
    load_state()

    def table_for(community: str):
        # Route by community; unknown -> None (dropped). Compile each device's
        # table ONCE (full walk) and, once the TTL lapses, refresh only the
        # DYNAMIC OIDs in place (~2-4 % of a replay device) — the OID set is
        # stable and ~96 % of values never change, so a from-scratch rebuild
        # every poll would waste CPU and churn MBs. [ts, table] is mutable so
        # the refresh keeps the same table object (no realloc, no double-buffer).
        dev = by_comm.get(community)
        if dev is None:
            return None
        now = time.time()
        hit = _TABLE_CACHE.get(community)
        if hit is None:
            rows = [
                (oid, val)
                for oid, _, val in (ln.partition(" ") for ln in dev.walk(now).splitlines())
                if oid
            ]
            _TABLE_CACHE[community] = [now, Table(rows)]
            return _TABLE_CACHE[community][1]
        if now - hit[0] >= _TABLE_TTL:
            hit[1].patch(dev.dynamic_rows(now))
            hit[0] = now
        return hit[1]

    server = SnmpServer(args.bind, args.snmp_port, table_for)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_state_persist_loop, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=auto_break_watchdog, daemon=True).start()

    http = ThreadingHTTPServer(("0.0.0.0", args.http_port), HttpHandler)  # nosec B104
    print(
        f"[boot] transport: SNMP v2c on {args.bind}:{SNMP_PORT} "
        f"({len(DEVICES)} devices, routed by community = device name)"
    )
    print(f"[boot] control: http://localhost:{args.http_port}/admin")
    print("[boot] IMPORTANT: run service discovery in Checkmk while HEALTHY.")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        save_state()


def _state_persist_loop() -> None:
    while True:
        time.sleep(RENDER_INTERVAL)
        try:
            save_state()
        except OSError as exc:
            print(f"[state] save failed: {exc}")


def main() -> None:
    global WALKS_DIR, DEVICES
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--site", help="write into /omd/sites/<site>/var/check_mk/snmpwalks")
    parser.add_argument("--walks-dir", help="explicit target directory for walk files")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT)
    parser.add_argument(
        "--access-switches",
        type=int,
        default=int(os.environ.get("NETSIM_ACCESS_SWITCHES", "1")),
        help="stamp out N access switches (sw-access-01..NN); "
        "only the first carries the incident story",
    )
    parser.add_argument(
        "--devices",
        default=os.environ.get("NETSIM_DEVICES", ""),
        help="comma list of device shorts to render (default: all)",
    )
    parser.add_argument(
        "--fleet",
        action="store_true",
        default=os.environ.get("NETSIM_FLEET", "0") == "1",
        help="also render the replay fleet (~110 devices from anonymized real walks in --walklib)",
    )
    parser.add_argument(
        "--walklib",
        default=os.environ.get(
            "NETSIM_WALKLIB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "walklib")
        ),
        help="directory with the curated model walks "
        "(walklib/*.walk; estate.py copies it to a "
        "site-readable location)",
    )
    parser.add_argument(
        "--once", action="store_true", help="render one set of walks and exit (no daemon)"
    )
    parser.add_argument(
        "--transport",
        choices=("snmp", "walk"),
        default=os.environ.get("NETSIM_TRANSPORT", "snmp"),
        help="snmp = answer SNMP live on one port, routed by "
        "community "
        "(no site filesystem, no sudo); walk = write "
        "stored-walk files into the site (legacy, needs "
        "the site user)",
    )
    parser.add_argument(
        "--snmp-port",
        type=int,
        default=int(os.environ.get("NETSIM_SNMP_PORT", "1161")),
        help="UDP port the SNMP responder listens on (per "
        "device IP); a non-privileged port needs no root",
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("NETSIM_BIND", "127.0.0.1"),
        help="address the SNMP responder binds (127.0.0.1 as a "
        "host process; 0.0.0.0 in a port-mapped container)",
    )
    args = parser.parse_args()

    if args.transport == "snmp" and not args.once:
        return _main_snmp(args)

    WALKS_DIR = resolve_walks_dir(args)
    os.makedirs(WALKS_DIR, exist_ok=True)
    probe = os.path.join(WALKS_DIR, ".netsim-probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as exc:
        sys.exit(
            f"cannot write to {WALKS_DIR} ({exc}) — run as the site user, "
            f"e.g.: sudo -u <site> python3 netsim.py"
        )

    DEVICES = assemble_devices(args)
    load_state()
    write_walks()

    if args.once:
        for d in DEVICES:
            print(f"[once] wrote {os.path.join(WALKS_DIR, d.fqdn)}")
        save_state()
        return

    threading.Thread(target=render_loop, daemon=True).start()
    if AUTO_BREAK_AFTER_MIN > 0:
        threading.Thread(target=auto_break_watchdog, daemon=True).start()

    http = ThreadingHTTPServer(("0.0.0.0", args.http_port), HttpHandler)  # nosec B104
    if len(DEVICES) <= 8:
        print(f"[boot] devices: {', '.join(d.fqdn for d in DEVICES)}")
    else:
        print(
            f"[boot] devices: {len(DEVICES)} "
            f"({sum(1 for d in DEVICES if d.incident)} with incident toggles)"
        )
    print(f"[boot] walks:   {WALKS_DIR} (every {RENDER_INTERVAL:g} s)")
    print(f"[boot] control: http://localhost:{args.http_port}/admin")
    print("[boot] IMPORTANT: run service discovery in Checkmk while HEALTHY —")
    print("[boot] down interfaces are never discovered, and the interface")
    print("[boot] target state is the one recorded at discovery.")
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        print("\n[boot] shutting down")
        save_state()


if __name__ == "__main__":
    main()
