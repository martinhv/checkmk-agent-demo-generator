#!/usr/bin/env python3
"""Curate real SNMP walks into anonymized, trimmed walklib/ files.

Source walks (~/git/zeug_cmk/walks) come from real devices and MUST NOT enter
this repo verbatim: they carry sysName/sysContact/sysLocation, port
descriptions, LLDP/CDP neighbors, ARP/route/FDB tables, serial numbers, MACs
and IP addresses of real networks. This script:

  1. STRIPS whole subtrees that are identifying and/or bulk and that no
     Checkmk check reads (RMON, bridge FDB, IP/route/ARP tables, LLDP/CDP,
     hrSWRun process tables, TCP/UDP connection tables, routing protocols);
  2. REWRITES identity OIDs (sysName/sysContact/sysLocation, ifAlias,
     ENTITY/printer/vendor serial numbers, ifPhysAddress MACs);
  3. SCRUBS values generically: the device's own recorded sysName wherever it
     appears (sysDescr!), e-mail addresses, "SN:..." tokens, and IPv4
     addresses (deterministically mapped into 10.77.0.0/16);
  4. AUDITS what remains: every distinct value still containing letters is
     printed for manual review (--audit), so leftover identifiers can be
     added to a model's `strip` / `set` config here.

Usage:
    ./curate_walks.py --source ~/git/zeug_cmk/walks [--only MODEL] [--audit]

Output: snmp/walklib/<model>.walk (numerically sorted, netsim walk format)
plus walklib/manifest.json metadata for netsim.py's replay layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
WALKLIB = os.path.join(HERE, "walklib")

# --------------------------------------------------------------------------- #
#  Subtrees stripped from EVERY walk: identifying data and/or dead weight.
#  Verified against the checks: nothing in cmk/plugins reads these for
#  monitoring (hr_ps would read hrSWRun — we deliberately drop process tables).
# --------------------------------------------------------------------------- #
GLOBAL_STRIP = [
    ".1.3.6.1.2.1.3.",  # AT (net-to-media: real MAC/IP pairs)
    ".1.3.6.1.2.1.4.",  # ip: own addresses, routes, ARP
    ".1.3.6.1.2.1.5.",  # icmp counters (useless bulk)
    ".1.3.6.1.2.1.6.",  # tcp incl. connection table (real peers)
    ".1.3.6.1.2.1.7.",  # udp table
    ".1.3.6.1.2.1.10.",  # transmission (dot3 etc. — unused bulk)
    ".1.3.6.1.2.1.14.",  # OSPF (real neighbors)
    ".1.3.6.1.2.1.15.",  # BGP (real peers/AS)
    ".1.3.6.1.2.1.16.",  # RMON (packet capture stats — huge)
    ".1.3.6.1.2.1.17.",  # bridge MIB incl. FDB (real MACs)
    ".1.3.6.1.2.1.25.4.",  # hrSWRun (process names)
    ".1.3.6.1.2.1.25.5.",  # hrSWRunPerf
    ".1.3.6.1.2.1.25.6.",  # hrSWInstalled
    ".1.3.6.1.2.1.26.",  # MAU (bulk)
    ".1.0.8802.",  # LLDP (real neighbor names/IPs)
    ".1.3.6.1.4.1.9.9.23.",  # Cisco CDP (real neighbors)
    ".1.3.6.1.4.1.9.9.43.",  # Cisco config-copy (tftp server IPs)
    ".1.3.6.1.6.3.",  # SNMP framework/usm/notification targets
]

# Identity/serial OIDs rewritten in every walk (prefix -> replacement scheme).
SYSNAME = ".1.3.6.1.2.1.1.5.0"
SYSCONTACT = ".1.3.6.1.2.1.1.4.0"
SYSLOCATION = ".1.3.6.1.2.1.1.6.0"
IFALIAS = ".1.3.6.1.2.1.31.1.1.1.18."
IFPHYS = ".1.3.6.1.2.1.2.2.1.6."
SERIAL_PREFIXES = [
    ".1.3.6.1.2.1.47.1.1.1.1.11.",  # entPhysicalSerialNum
    ".1.3.6.1.2.1.47.1.1.1.1.14.",  # entPhysicalAlias
    ".1.3.6.1.2.1.47.1.1.1.1.15.",  # entPhysicalAssetID
    ".1.3.6.1.2.1.43.5.1.1.17.",  # prtGeneralSerialNumber
    ".1.3.6.1.4.1.318.1.1.1.1.2.3.",  # APC UPS serial
    ".1.3.6.1.4.1.318.1.4.1.4.",  # APC hw serial (mgmt card)
    ".1.3.6.1.4.1.318.1.4.2.4.1.2.",  # APC module serial table
    ".1.3.6.1.4.1.12356.100.1.1.1.",  # Fortinet serial
    ".1.3.6.1.4.1.6574.1.5.2.",  # Synology serial
    ".1.3.6.1.4.1.674.10892.5.1.3.2.",  # iDRAC chassis service tag
    ".1.3.6.1.4.1.674.10892.5.1.3.3.",  # iDRAC express service code
    ".1.3.6.1.4.1.674.10892.5.4.300.1.1.11.",  # iDRAC system service tag
]

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_SN = re.compile(r"\bSN:? ?[A-Za-z0-9-]{4,}")
RE_IP = re.compile(r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])")
RE_HEX = re.compile(r'^"([0-9A-F]{2} )*"$')


def map_ip(match: re.Match[str]) -> str:
    """Deterministically remap a real IP into 10.77.0.0/16 (distinct inputs
    stay distinct with overwhelming probability)."""
    ip = match.group(0)
    if ip.startswith(("127.", "0.", "255.")) or ip == "0.0.0.0":
        return ip
    h = hashlib.sha256(ip.encode()).digest()
    return f"10.77.{h[0]}.{h[1] or 1}"


def fake_serial(model: str, oid: str) -> str:
    h = hashlib.sha256(f"{model}:{oid}".encode()).hexdigest().upper()
    return "MR" + h[:10]


def fake_mac(model: str, index: str) -> str:
    h = hashlib.sha256(f"{model}:{index}".encode()).digest()
    return f'"00 1B 2C {h[0]:02X} {h[1]:02X} {h[2]:02X} "'


def decode_hex(value: str) -> str | None:
    if not RE_HEX.match(value):
        return None
    try:
        raw = bytes(int(b, 16) for b in value.strip('"').split())
        text = raw.decode("ascii")
        return text if text.isprintable() or "\r" in text or "\n" in text else None
    except (ValueError, UnicodeDecodeError):
        return None


def encode_hex(text: str) -> str:
    return '"' + "".join(f"{b:02X} " for b in text.encode("ascii", "replace")) + '"'


# --------------------------------------------------------------------------- #
#  Model registry: source file -> walklib model. `strip`: extra per-model
#  subtree strips (audit findings). `set`: hard value overrides by exact OID.
# --------------------------------------------------------------------------- #
# Recurring per-model subs: neighbor/config tables of the source orgs use
# recognizable device-name schemes — rename them deterministically.
_BM_SW = [(r"(?i)\bbm-[a-z0-9-]+", "mr-sw-{h}")]

# hpSwitchCpuStat — netsim wobbles this at render time; the value set here is
# the base the wobble moves around.
_HP_CPU = ".1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0"

MODELS: dict[str, dict[str, Any]] = {
    # -- switches ------------------------------------------------------------
    "aruba-2930f": {
        "src": "Aruba-JL261A-2930F-24G-PoE-4SFP-Switch.txt",
        "cls": "switch",
        "sub": _BM_SW,
        "set": {_HP_CPU: "20"},
    },  # recorded 55 % — headroom
    "aruba-6200f": {
        "src": "Aruba-JL725B-6200F-24G-CL4-4SFP-370W-Switch.txt",
        "cls": "switch",
        # 802.1X client table: authenticated user/host
        # identities + client MACs in the OID index
        "strip": [".1.3.6.1.4.1.47196.4.1.1.3.17."],
    },
    "hp-2530": {
        "src": "HP-J9772A-2530-48G-PoEP-Switch.txt",
        "cls": "switch",
        "sub": _BM_SW,
        # recorded CPU was 94 % -> CRIT at the 80/90 defaults
        "set": {_HP_CPU: "12"},
    },
    "procurve-2510": {
        "src": "ProCurve-J9279A-Switch-2510G-24.txt",
        "cls": "switch",
        # trunk/neighbor names reveal the source org
        "sub": [(r"(?i)\bsw-[a-z0-9-]{3,}", "mr-sw-{h}")],
        "set": {_HP_CPU: "9"},
    },
    "hp-5406r": {
        "src": "HP-J9850A-Switch-5406Rzl2.txt",
        "cls": "switch",
        "sub": _BM_SW,
        # aux PSU slots recorded unpowered (9) -> CRIT; and
        # keep the recorded 75 % CPU clear of the levels
        "set": {
            _HP_CPU: "24",
            ".1.3.6.1.4.1.11.2.14.11.5.1.55.1.1.1.2.1": "3",
            ".1.3.6.1.4.1.11.2.14.11.5.1.55.1.1.1.2.2": "3",
        },
    },
    "huawei-s6730": {"src": "HUAWEI-CloudEngine-S6730-H-V2.txt", "cls": "switch"},
    # -- routers / firewalls / wlc / lb ---------------------------------------
    "lancom-router": {"src": "network-lancom-voip-router-lldp", "cls": "router"},
    "fortigate": {
        "src": "Fortigate.txt",
        "cls": "firewall",
        "sub": [
            (r"Serial#: ?\w+", "Serial#: MR0000000000"),
            (r"\bFGT80FTK\w+", "MR0000000000"),
            (r"FBIN", "MRHQ"),
        ],
        # AV/IPS signature ages are recorded timestamps ->
        # permanently CRIT-old; drop the section
        "strip": [".1.3.6.1.4.1.12356.101.4.2."],
    },
    "cisco-asa": {
        "src": "cisco-asa-9.16-no-name",
        "cls": "firewall",
        "sub": [(r"\bACYN[-\w]*", "mr-fw-dmz")],
        # recorded mempools sat at 95/99.98 % (normal for a
        # real ASA heapcache, red at the cmk 80/90 defaults)
        "set": {
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.7.2.4": "120000000",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.18.2.4": "120000000",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.8.2.4": "192475648",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.20.2.4": "192475648",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.7.2.6": "9000000",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.18.2.6": "9000000",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.8.2.6": "15514560",
            ".1.3.6.1.4.1.9.9.221.1.1.1.1.20.2.6": "15514560",
        },
    },
    "extreme-wlc": {
        "src": "network-extreme-wlc",
        "cls": "wlc",
        # AP names carry the org's site codes; SSIDs its name
        "sub": [(r"AP-[A-Z]{2,8}(-[A-Z]{2,8})*(?=[-_\d])", "AP-S{h}"), (r"\bMMH-", "MR-")],
    },
    "kemp-lb": {
        "src": "loadbalancer-kemp-1",
        "cls": "loadbalancer",
        "sub": [(r"\bITC(?=[ -])", "MR")],
    },
    # -- printers --------------------------------------------------------------
    # printers: recorded with empty toners + active alert tables — refill the
    # supplies and drop prtAlertTable (a wall of green, not a service call)
    "printer-ricoh": {
        "src": "printer-ricoh-c4000",
        "cls": "printer",
        "strip": [".1.3.6.1.2.1.43.18."],
        # printer_supply_ricoh reads column 5 (percent)
        "set": {
            ".1.3.6.1.4.1.367.3.2.1.2.24.1.1.5.1": "70",
            ".1.3.6.1.4.1.367.3.2.1.2.24.1.1.5.2": "80",
            ".1.3.6.1.4.1.367.3.2.1.2.24.1.1.5.3": "75",
            # bypass tray recorded 'unavailable on request'
            ".1.3.6.1.2.1.43.8.2.1.11.1.4": "0",
        },
    },
    "printer-canon": {
        "src": "printer-canon-c5240-1",
        "cls": "printer",
        "strip": [".1.3.6.1.2.1.43.18."],
        "set": {
            ".1.3.6.1.2.1.43.11.1.1.9.1.1": "62",
            ".1.3.6.1.2.1.43.11.1.1.9.1.2": "85",
            ".1.3.6.1.2.1.43.11.1.1.9.1.3": "71",
            ".1.3.6.1.2.1.43.11.1.1.9.1.4": "78",
        },
    },
    "printer-zebra": {"src": "printer-zebra", "cls": "printer"},
    # -- power / environment ----------------------------------------------------
    # Output-phase tree stripped: parse_apc_symmetra_output crashes on the
    # 3.0.0 dev branch (ElPhase.from_dict re-parses ReadingWithState ->
    # TypeError; introduced in c1802b42504) — restore once fixed upstream.
    # Self-test date (.7.2.4.0) is rendered dynamically by netsim.
    "apc-symmetra": {
        "src": "usv-apc-symmetra-1",
        "cls": "ups",
        "strip": [".1.3.6.1.4.1.318.1.1.1.4.2."],
        "set": {".1.3.6.1.4.1.318.1.1.1.7.2.4.0": "01/01/2026"},
    },
    "apc-pdu": {"src": "apc-netshelterpdu-advanced", "cls": "pdu"},
    "gude-pdu": {
        "src": "gude-power-switch-1",
        "cls": "pdu",
        "sub": [(r"eth_cf52235", "eth_001b2c0")],
    },
    "raritan-pdu": {"src": "pdu-raritan-1", "cls": "pdu"},
    "akcp-sensor": {
        "src": "akcp-sensor-probe",
        "cls": "sensor",
        # dry contact 0 recorded in error state (4) -> normal
        "set": {".1.3.6.1.4.1.3854.1.2.2.1.18.1.3.0": "2"},
    },
    "avtech-ra3s": {"src": "avtech-roomalert-3s", "cls": "sensor"},
    # -- storage / san / oob mgmt / appliances ----------------------------------
    "synology-nas": {
        "src": "storage-synology-1",
        "cls": "nas",
        # kernel cmdline embeds the real NIC MACs
        "sub": [(r"\bmac(\d)=[0-9a-fA-F]{12}", r"mac\g<1>=001B2C00000\g<1>")],
        # update status recorded 3 (Connecting): the check
        # yields NOTHING for it -> service pends forever;
        # 2 = no update available -> OK
        "set": {".1.3.6.1.4.1.6574.1.5.4.0": "2"},
    },
    "brocade-fc": {
        "src": "fcswitch-brocade",
        "cls": "fcswitch",
        # swEvent log: real login source hostnames
        "strip": [".1.3.6.1.4.1.1588.2.1.1.1.8."],
    },
    "idrac": {
        "src": "idrac-dell-1",
        "cls": "mgmt",
        # OS volume I: recorded at 85 % -> WARN at 80/90
        "set": {".1.3.6.1.2.1.25.2.3.1.6.7": "30150000"},
    },
    "meinberg-ntp": {
        "src": "meinberg-lantime-1",
        "cls": "appliance",
        # real admin contact + the site's GPS coordinates
        "sub": [
            (r"Daniele Basile - ZID Basel", "NetOps - Meridian Retail"),
            (r"GPS Position: [-0-9. ]+m", "GPS Position: 48.1374 11.5755 519m"),
        ],
        # refclock recorded 'not synchronized / antenna
        # disconnected' (2/3) -> synchronized / GPS sync
        # with 9 of 12 satellites (levels_lower 3/3);
        # /mnt/flash recorded 89.7 % full -> ~49 %
        "set": {
            ".1.3.6.1.4.1.5597.30.0.1.2.1.4.1": "1",
            ".1.3.6.1.4.1.5597.30.0.1.2.1.5.1": "1",
            ".1.3.6.1.4.1.5597.30.0.1.2.1.6.1": "9",
            ".1.3.6.1.2.1.25.2.3.1.6.37": "24000",
        },
    },
}


def parse_walk(path: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, encoding="latin-1") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.startswith("."):
                continue
            oid, _, value = line.partition(" ")
            rows.append((oid, value))
    return rows


def oid_key(oid: str) -> tuple[int, ...]:
    return tuple(int(p) for p in oid.strip(".").split("."))


def _apply_subs(text: str, subs: list[tuple[str, str]]) -> str:
    """Per-model regex subs; '{h}' in the replacement becomes a 2-digit
    deterministic hash of the matched text (stable renaming of site codes)."""
    for pattern, repl in subs:
        if "{h}" in repl:

            def _r(m: re.Match[str], repl: str = repl) -> str:
                h = int(hashlib.sha256(m.group(0).encode()).hexdigest(), 16) % 90 + 10
                return repl.replace("{h}", str(h))

            text = re.sub(pattern, _r, text)
        else:
            text = re.sub(pattern, repl, text)
    return text


def scrub_value(value: str, sysname: str, model: str, subs: list[tuple[str, str]]) -> str:
    """Generic value scrub: recorded sysName, e-mails, SN tokens, IPs, plus
    the model's own subs — applied to plain AND hex-encoded string values."""

    def scrub_text(t: str) -> str:
        if sysname and len(sysname) >= 3:
            t = re.sub(re.escape(sysname), f"mr-{model}", t, flags=re.I)
        t = RE_EMAIL.sub("netops@meridian-retail.com", t)
        t = RE_SN.sub("SN:MR0000000000", t)
        t = RE_IP.sub(map_ip, t)
        return _apply_subs(t, subs)

    decoded = decode_hex(value)
    if decoded is not None:
        scrubbed = scrub_text(decoded)
        return value if scrubbed == decoded else encode_hex(scrubbed)
    return scrub_text(value)


def curate(model: str, cfg: dict[str, Any], source_dir: str, audit: bool) -> dict[str, Any] | None:
    src = os.path.join(source_dir, cfg["src"])
    if not os.path.exists(src):
        print(f"  !! {model}: source {cfg['src']} missing — skipped")
        return None
    rows = parse_walk(src)
    strip = GLOBAL_STRIP + cfg.get("strip", [])
    overrides: dict[str, str] = dict(cfg.get("set", {}))
    subs: list[tuple[str, str]] = cfg.get("sub", [])

    sysname = next((v for o, v in rows if o == SYSNAME), "")
    if RE_HEX.match(sysname):
        sysname = decode_hex(sysname) or ""

    out: list[tuple[str, str]] = []
    for oid, value in rows:
        if any(oid.startswith(p) for p in strip):
            continue
        if oid in overrides:
            out.append((oid, overrides[oid]))
            continue
        if oid == SYSNAME:
            value = f"mr-{model}"  # netsim overrides per instance
        elif oid == SYSCONTACT:
            value = "netops@meridian-retail.com"
        elif oid == SYSLOCATION:
            value = "Meridian Retail"  # netsim overrides per instance
        elif oid.startswith(IFALIAS):
            value = ""
        elif oid.startswith(IFPHYS):
            value = fake_mac(model, oid.rsplit(".", 1)[-1])
        elif any(oid.startswith(p) for p in SERIAL_PREFIXES):
            if value.strip().strip('"'):
                value = fake_serial(model, oid)
        else:
            value = scrub_value(value, sysname, model, subs)
        out.append((oid, value))

    out.sort(key=lambda r: oid_key(r[0]))
    os.makedirs(WALKLIB, exist_ok=True)
    dest = os.path.join(WALKLIB, f"{model}.walk")
    with open(dest, "w") as f:
        for oid, value in out:
            f.write(f"{oid} {value}\n")
    kept = len(out)
    print(f"  {model:15} {len(rows):>7} -> {kept:>6} rows  ({cfg['src']})")

    if audit:
        seen: set[str] = set()
        for _oid, value in out:
            text = decode_hex(value)
            if text is None:
                text = value
            if not re.search(r"[A-Za-z]{3,}", text):
                continue
            if text in seen:
                continue
            seen.add(text)
        print(f"    -- audit: {len(seen)} distinct string values --")
        for t in sorted(seen):
            print(f"    | {t[:140]}")
    return {"file": f"{model}.walk", "class": cfg["cls"], "source": cfg["src"], "rows": kept}


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("--source", default=os.path.expanduser("~/git/zeug_cmk/walks"))
    p.add_argument("--only", help="curate a single model")
    p.add_argument(
        "--audit", action="store_true", help="print every remaining string value for review"
    )
    args = p.parse_args()

    models = {args.only: MODELS[args.only]} if args.only else MODELS
    manifest_path = os.path.join(WALKLIB, "models.json")
    manifest: dict[str, Any] = {}
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    print(f"curating {len(models)} models from {args.source}")
    for model, cfg in models.items():
        meta = curate(model, cfg, args.source, args.audit)
        if meta:
            manifest[model] = meta
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
