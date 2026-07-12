#!/usr/bin/env python3
"""One-shot Checkmk site setup for the demo estate (agent + SNMP side).

The REST-API deployment engine behind ../estate.py — usually you run THAT.
Standalone use against a running estate works exactly like before:

    ./cmk_setup.py --site-url http://localhost/prod \
        --user automation --secret '...'

On a Checkmk dev box, sites made by cmk-dev-site / cmk-dev-install-site
(cmkadmin/cmk, http://localhost/<site>) need no options at all:

    ./cmk_setup.py --site        # newest running local v* dev site
    ./cmk_setup.py --site v300   # a specific local site

What it does (idempotent — safe to re-run):

  0. computes a fingerprint of the intended Setup state (roster, ports, SNMP
     device set, applicable BI tiers, folder + a SCHEMA_VERSION) and compares
     it to the one stored as a label on the shell host. Unchanged? It returns
     in ~1s — the slow discovery + activation is skipped. `--force` re-runs
     regardless. (Live metric values are NOT in the fingerprint — they change
     every poll but never the set of discovered services.)
  1. asks the delivery control panel (:8099) which hosts it actually carries
     (so an ESTATE_HOSTS subset — and ESTATE_REPLICAS replication — is
     handled automatically) and HEALS any non-healthy host first — services
     must be discovered in the healthy state (db-postgres-01's SMART check
     baselines raw values at discovery);
  2. creates a dedicated Setup ROOT folder (default: "Meridian Retail demo")
     and, beneath it, a role-based subfolder tree (Applications, Databases,
     Storage, Infrastructure, Windows servers, Network/{Switches,Routers,UPS})
     so the estate looks like a real infrastructure; each host is sorted into
     its subfolder by name;
  3. creates the estate hosts. Self-hosted (datasource delivery): every host
     is a Checkmk-agent host whose agent source is ONE "Individual program call
     instead of agent access" rule on the root folder — `cat <dir>/$HOSTNAME$`
     — inherited by all subfolders, reading the per-host files the delivery
     shell writes. Cloud (piggyback delivery): the shell is a TCP host (agent
     port via an "agent_ports" rule) and every estate host is a piggyback host;
  4. if the SNMP simulator (snmp/netsim.py, panel :8101) is running, creates
     its devices as SNMP/no-agent/no-IP hosts plus a "usewalk_hosts" rule
     ("Simulating SNMP by using a stored SNMP walk") scoped to exactly those
     hosts — Checkmk then reads the rendered walk files instead of the
     network (the StoredWalk backend bypasses NO_IP with 127.0.0.1);
  5. runs service discovery — the shell FIRST, because that initial agent
     fetch is what stores the piggyback payloads the other hosts need. Hosts
     that already carry monitored services are skipped (the shell never is:
     it re-delivers piggyback), so a roster that only grew pays for the new
     hosts alone; --force rescans everything;
  6. activates the changes.

Deployment modes (--mode, default self-hosted):

  self-hosted  full access to the Checkmk site filesystem. The SNMP layer
               (stored walks) is available, and the agent hosts use the
               datasource-program delivery: the shell writes each host's agent
               output to a file (--agent-output-dir) and Checkmk reads it per
               host via the "cat $HOSTNAME$" program instead of piggyback.
               Scales better (no single-shell fetch bottleneck).
  cloud        Checkmk Cloud (SaaS): no filesystem access. Data can only enter
               through the agent controller / relay, so the SNMP layer is
               skipped (--snmp is forced off) and the agent hosts stay on
               piggyback delivery via the shell.

`--remove` tears the whole thing down again (hosts, rules, BI pack, folder).

Needs: a site user with write access to Setup ("Administrator" role or an
automation user), and the agent port reachable FROM THE SITE (default
127.0.0.1:6559 — override --agent-ip if the site runs elsewhere).

Stdlib only, like everything else in this repo.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Literal, NoReturn, overload

if TYPE_CHECKING:
    from collections.abc import Callable

# Bump when a re-run must re-discover even though the roster is unchanged —
# i.e. whenever the SET OF SERVICES a host discovers changes (a fake agent
# starts/stops emitting a section) or the Setup objects this script creates
# change shape (host attributes, rule values, BI tiers). It is mixed into the
# estate fingerprint (see _estate_fingerprint), so bumping it invalidates the
# stored fingerprint on every site and forces the full discovery/activation
# pass once. Pure value changes in existing sections need NO bump — they don't
# change which services exist.
#   v2: introduced --mode (self-hosted/cloud); mode is now part of the estate
#       fingerprint.
#   v3: datasource delivery (cat program) for self-hosted + hosts sorted into a
#       role-based subfolder tree; the datasource command and per-host folder
#       are now part of the fingerprint.
#   v4: the 300-host company estate — fleet hosts + SNMP walk-replay devices,
#       roles delivered by the panels, per-device SNMP parents, new taxonomy
#       folders (Firewalls, Load balancers, Wireless, Printers, OOB mgmt,
#       Hypervisors), bulk discovery.
#   v5: replay-walk green pass — fortigate signatures/apc output-phase/printer
#       alert sections dropped, residual-current rule for the Raritan PDUs.
#   v6: the delivery shell now hangs off the campus core (sw-core-01) like every
#       server, so sw-core-01 is the estate's single parentless root.
#   v7: tiered topology — endpoints hang off access switches, not the core
#       (servers/shell → sw-access-01; fleet iron → DC ToR round-robin); VMs
#       are children of their hypervisor; a hypervisor per warehouse.
#   v8: warehouse WAN path — each warehouse gets a local CPE router
#       (rt-wh1-01/rt-wh2-01) behind the DC head-end rt-wan-01; warehouse gear
#       hangs off its CPE (fixes rt-wan-02 which never existed / collided).
#   v9: distribution tier — DC ToR + leaves hang off the sw-dc-dist pair, HQ
#       gear off a new sw-hq-dist-01; only distribution switches, firewalls and
#       edge routers uplink to the core (fan-out 88 -> ~10).
#   v10: leaf-level realism — DC OOB mgmt switch (sw-dc-oob-01) carrying
#        iDRACs/PDUs/env sensors; HQ printers on their floor switches;
#        warehouse printers/power on the hall switches (mezzanine daisy-chain);
#        ups-01 behind sw-access-01; base devices recast as DC gear.
#   v11: live SNMP transport — netsim answers SNMP on a UDP port per device
#        (no stored walks, no sudo); SNMP devices get a loopback ipaddress +
#        ip-v4-only and a folder community/port rule instead of usewalk.
#   v12: one shared SNMP port routed by community — SNMP hosts share
#        ipaddress 127.0.0.1 and carry a unique per-host community attribute
#        (the device name); only a folder port rule remains. Lets netsim run
#        as a normal port-mapped container like the gateway.
SCHEMA_VERSION = 12

# Host label on the delivery shell holding the last-activated estate
# fingerprint. Lives on the site (survives across `estate.py up` runs) and is
# what lets an unchanged re-run skip discovery + activation entirely.
FINGERPRINT_LABEL = "meridian_demo/fingerprint"

RULE_DESCRIPTION = "Meridian Retail demo: agent port of the piggyback delivery shell"
BI_RULE_DESCRIPTION = "Meridian Retail demo: payments platform business service"
SNMP_RULE_DESCRIPTION = "Meridian Retail demo: stored SNMP walks (snmp/netsim.py)"
SNMP_COMMUNITY_RULE_DESCRIPTION = "Meridian Retail demo: SNMP community (netsim responder)"
SNMP_PORT_RULE_DESCRIPTION = "Meridian Retail demo: SNMP port (netsim responder)"
DATASOURCE_RULE_DESCRIPTION = (
    "Meridian Retail demo: agent output via datasource program (cat $HOSTNAME$)"
)
RESIDUAL_RULE_DESCRIPTION = "Meridian Retail demo: PDUs without residual-current sensors stay OK"

# --- Folder taxonomy --------------------------------------------------------
# Hosts are sorted into a role-based subfolder tree under the estate root so
# the demo looks like a real infrastructure (not one flat folder). Each role
# maps to a chain of (folder-name, title) under the root; nested chains (the
# network gear) become sub-subfolders. Classification is by host short-name
# prefix so replicas (web-frontend-02, ...) land next to their originals.
FOLDER_TAXONOMY: dict[str, list[tuple[str, str]]] = {
    "applications": [("applications", "Applications")],
    "databases": [("databases", "Databases")],
    "storage": [("storage", "Storage")],
    "infrastructure": [("infrastructure", "Infrastructure")],
    "virtualization": [("virtualization", "Hypervisors")],
    "windows": [("windows", "Windows servers")],
    "printers": [("printers", "Printers")],
    "mgmt": [("oob", "Out-of-band management")],
    "net_switches": [("network", "Network"), ("switches", "Switches")],
    "net_routers": [("network", "Network"), ("routers", "Routers & WAN")],
    "net_firewalls": [("network", "Network"), ("firewalls", "Firewalls")],
    "net_loadbalancers": [("network", "Network"), ("loadbalancers", "Load balancers")],
    "net_wifi": [("network", "Network"), ("wireless", "Wireless")],
    "net_ups": [("network", "Network"), ("ups", "UPS & power")],
    "network": [("network", "Network")],
}
_AGENT_ROLE_PREFIXES = [
    ("web-frontend", "applications"),
    ("payment-api", "applications"),
    ("app-worker", "applications"),
    ("app-redis", "applications"),
    ("db-postgres", "databases"),
    ("fileserver", "storage"),
    ("backup", "storage"),
    ("mail-relay", "infrastructure"),
    ("win-dc", "windows"),
]


def _agent_role(short: str) -> str:
    for prefix, role in _AGENT_ROLE_PREFIXES:
        if short.startswith(prefix):
            return role
    return "infrastructure"


def _host_role(h: dict[str, Any]) -> str:
    """Panel-delivered role (fleet hosts / netsim devices carry one) with the
    short-name prefix table as fallback for the classic roster."""
    role = h.get("role")
    if role in FOLDER_TAXONOMY:
        return role
    return _agent_role(h["name"])


def _snmp_role(short: str) -> str:
    if short.startswith("sw-"):
        return "net_switches"
    if short.startswith(("wan", "router", "rtr")):
        return "net_routers"
    if short.startswith("ups"):
        return "net_ups"
    return "network"


# --- BI pack: "Payments platform" -------------------------------------------
# Tier rules (worst-of) feeding one top rule; leaves reference services by
# (host short name, service regex prefix). Only services every host discovers
# with DEFAULT rules are used. Hosts not carried (ESTATE_HOSTS subset) are
# skipped; empty tiers are dropped.
BI_PACK_ID = "meridian_demo"
BI_PACK_TITLE = "Meridian Retail demo"
BI_TOP_RULE_ID = "meridian_payments_platform"
BI_AGGR_ID = "meridian_payments_platform"
BI_AGGR_TITLE = "Payments platform"  # = top rule title = aggregation name
BI_GROUP = "Meridian Retail"
BI_TIERS = [
    (
        "meridian_network_path",
        "Network path",
        [
            # the SNMP campus core (only present when the SNMP layer is deployed;
            # the tier is dropped automatically otherwise)
            ("sw-core-01", "Interface"),
            ("sw-core-01", "CPU utilization"),
        ],
    ),
    (
        "meridian_customer_entry",
        "Customer entry",
        [
            ("web-frontend-01", "Interface"),
            ("web-frontend-01", "CPU utilization"),
            ("web-frontend-01", "Memory"),
        ],
    ),
    (
        "meridian_payment_api",
        "Payment API",
        [
            ("payment-api", "Systemd Service Summary"),
            ("payment-api", "CPU load"),
            ("payment-api", "Memory"),
            ("payment-api", "TCP Connections"),
        ],
    ),
    (
        "meridian_processing",
        "Processing & cache",
        [
            ("app-worker-01", "Memory"),
            ("app-worker-01", "Systemd Service Summary"),
            ("app-worker-01", "CPU load"),
            ("app-redis-01", "Redis MERIDIAN_CACHE"),
            ("app-redis-01", "Memory"),
        ],
    ),
    (
        "meridian_data_layer",
        "Data layer",
        [
            ("db-postgres-01", "PostgreSQL"),
            ("db-postgres-01", "Disk IO SUMMARY"),
            ("db-postgres-01", "CPU load"),
            ("db-postgres-02", "PostgreSQL Connections"),
            ("db-postgres-02", "PostgreSQL Instance"),
        ],
    ),
    (
        "meridian_storage",
        "Storage",
        [
            ("fileserver-01", "Filesystem /srv/shares"),
        ],
    ),
]


# --------------------------------------------------------------------------- #
#  Tiny REST client (urllib, no redirect-following so we can poll async runs)
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class CmkApi:
    def __init__(self, site_url: str, user: str, secret: str) -> None:
        self.base = site_url.rstrip("/") + "/check_mk/api/1.0"
        self.auth = f"Bearer {user} {secret}"
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        etag: str | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """Return (status, parsed-json-or-None, headers). HTTP errors are
        returned, not raised — callers decide which codes are fine."""
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if etag is not None:
            req.add_header("If-Match", etag)
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, _maybe_json(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, _maybe_json(err.read()), dict(err.headers)
        except urllib.error.URLError as err:
            die(f"cannot reach the site API at {self.base}: {err.reason}")


def _maybe_json(raw: bytes):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def die(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def api_error(what: str, status: int, payload) -> None:
    detail = ""
    if isinstance(payload, dict):
        detail = ": " + "; ".join(str(payload[k]) for k in ("title", "detail") if payload.get(k))
        if payload.get("fields"):
            detail += f" {payload['fields']}"
    die(f"{what} failed (HTTP {status}){detail}")


# --------------------------------------------------------------------------- #
#  Local dev sites (cmk-dev-site / cmk-dev-install-site conventions)
# --------------------------------------------------------------------------- #
def _site_alive(name: str) -> bool:
    """A started site answers its REST API url (401 without credentials)."""
    try:
        urllib.request.urlopen(  # noqa: S310
            f"http://localhost/{name}/check_mk/api/1.0/version", timeout=5
        )
        return True
    except urllib.error.HTTPError as err:
        return err.code in (401, 200)
    except (urllib.error.URLError, OSError):
        return False


def detect_dev_site() -> str:
    """Newest running local OMD site named like cmk-dev-site makes them (v300,
    v260p1, ...). Newest = creation order via the version symlink's ctime."""
    try:
        candidates = [s for s in os.listdir("/omd/sites") if s.startswith("v") and s[1:2].isdigit()]
    except OSError:
        candidates = []
    if not candidates:
        die("no local v* dev sites found under /omd/sites — pass --site NAME or --site-url URL")
    candidates.sort(key=lambda s: os.lstat(f"/omd/sites/{s}/version").st_ctime, reverse=True)
    for name in candidates:
        if _site_alive(name):
            return name
    die(
        f"none of the local dev sites ({', '.join(candidates)}) answers on "
        "http://localhost/<site>/ — is one started? (omd start <site>)"
    )
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
#  Delivery control panel (source of truth for the carried roster)
# --------------------------------------------------------------------------- #
@overload
def panel_get(
    panel: str, path: str = "/", *, optional: Literal[False] = False
) -> dict[str, Any]: ...
@overload
def panel_get(panel: str, path: str = "/", *, optional: Literal[True]) -> dict[str, Any] | None: ...
def panel_get(panel: str, path: str = "/", *, optional: bool = False) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(panel.rstrip("/") + path, timeout=10) as r:  # noqa: S310
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as err:
        if optional:
            return None
        die(
            f"cannot reach the delivery control panel at {panel}: {err}\n"
            "       Is the estate running?  ./estate.py up  (or: cd "
            "deploy/piggyback && docker compose up --build -d)"
        )


def heal_estate(panel: str, hosts: list[dict[str, Any]]) -> None:
    """Toggle every non-healthy host back to healthy before discovery."""
    unhealthy = [h for h in hosts if h.get("state") != "healthy" and "heal" in h.get("actions", [])]
    for h in unhealthy:
        print(f"  healing {h['name']} (was: {h.get('state')})")
        try:
            urllib.request.urlopen(  # noqa: S310
                f"{panel.rstrip('/')}/admin/{h['name']}/heal", timeout=10
            ).read()
        except (urllib.error.URLError, OSError) as err:
            die(f"healing {h['name']} failed: {err}")
    if unhealthy:
        time.sleep(2)  # let the children settle before the discovery fetch


# --------------------------------------------------------------------------- #
#  Setup objects
# --------------------------------------------------------------------------- #
def _folder_segs(folder: str) -> tuple[str, ...]:
    """Normalise a folder path in any notation ("~a~b", "/a/b", "a/b") to its
    tuple of segments; root -> ()."""
    return tuple(s for s in str(folder).replace("~", "/").split("/") if s)


def folder_ident(*parts: str) -> str:
    """REST API folder ident ("~" = root, "~" separates segments). Each part
    may itself be a multi-segment path in any notation; all are flattened."""
    segs: list[str] = []
    for p in parts:
        segs.extend(_folder_segs(p))
    return "~" + "~".join(segs) if segs else "~"


# kept for the fingerprint/teardown call sites that pass a single folder name
def folder_id(folder_name: str) -> str:
    return folder_ident(folder_name)


def ensure_folder(api: CmkApi, ident: str, title: str, parent: str = "~") -> str:
    """Create folder `ident` (its own name = the last segment) under `parent`
    if missing. Root ("~") always exists. Idempotent."""
    if ident == "~":
        return ident
    status, _, _ = api.request("GET", f"/objects/folder_config/{ident}")
    if status == 200:
        return ident
    name = ident.rsplit("~", 1)[-1]
    status, payload, _ = api.request(
        "POST",
        "/domain-types/folder_config/collections/all",
        body={"name": name, "title": title, "parent": parent},
    )
    if status != 200:
        api_error(f"creating folder {ident}", status, payload)
    print(f"  created folder {ident} ({title!r})")
    return ident


def ensure_folder_chain(api: CmkApi, root_ident: str, chain: list[tuple[str, str]]) -> str:
    """Ensure a nested (name, title) chain exists under root_ident (parents
    first) and return the leaf folder's ident."""
    parent = root_ident
    ident = root_ident
    for name, title in chain:
        ident = folder_ident(parent, name)
        ensure_folder(api, ident, title, parent)
        parent = ident
    return ident


def get_host(api: CmkApi, name: str) -> dict[str, Any] | None:
    status, payload, _ = api.request("GET", f"/objects/host_config/{name}")
    return payload if status == 200 else None


def ensure_host(api: CmkApi, name: str, folder: str, attributes: dict[str, Any]) -> None:
    """Create the host if missing; reconcile the parents attribute if not."""
    existing = get_host(api, name)
    if existing is None:
        status, payload, _ = api.request(
            "POST",
            "/domain-types/host_config/collections/all",
            body={"host_name": name, "folder": folder, "attributes": attributes},
        )
        if status != 200:
            api_error(f"creating host {name}", status, payload)
        print(f"  created host {name}")
        return
    # reconcile the attributes that carry the topology/datasource contract
    # (parents, and the agent/piggyback/address-family tags that differ between
    # the piggyback and datasource delivery modes) so a mode switch on an
    # existing estate takes effect; everything else is left as the user set it
    current = (existing.get("extensions") or {}).get("attributes") or {}
    fix = {
        k: attributes[k]
        for k in (
            "parents",
            "tag_agent",
            "tag_piggyback",
            "tag_address_family",
            "ipaddress",
            "snmp_community",
        )
        if k in attributes and current.get(k) != attributes[k]
    }
    if fix:
        status, payload, _ = api.request(
            "PUT", f"/objects/host_config/{name}", body={"update_attributes": fix}, etag="*"
        )
        if status != 200:
            api_error(f"updating attributes of {name}", status, payload)
        print(f"  host {name} exists — updated {', '.join(sorted(fix))}")
    else:
        print(f"  host {name} exists")


def _in_subtree(host_folder: str, root_segs: tuple[str, ...]) -> bool:
    """Is the host's folder the estate root or one of its subfolders?"""
    hsegs = _folder_segs(host_folder)
    return bool(root_segs) and hsegs[: len(root_segs)] == root_segs


def prune_subtree(api: CmkApi, root_ident: str, keep: set[str]) -> None:
    """Delete hosts ANYWHERE under the estate root that left the roster
    (removed host classes, scaled-down replicas) — the whole subtree is fully
    managed. Guarded against a root ('~') estate so we never mass-delete a
    shared site."""
    root_segs = _folder_segs(root_ident)
    if not root_segs:
        return  # estate at site root — refuse to prune the whole site
    status, payload, _ = api.request("GET", "/domain-types/host_config/collections/all")
    if status != 200:
        return
    for h in (payload or {}).get("value", []):
        name = h["id"]
        if name in keep:
            continue
        if not _in_subtree(h.get("extensions", {}).get("folder", ""), root_segs):
            continue
        st, _, _ = api.request("DELETE", f"/objects/host_config/{name}", etag="*")
        if st == 204:
            print(f"  pruned stale host {name}")


def _marked_rules(
    api: CmkApi, ruleset: str, description: str
) -> list[tuple[dict[str, Any], list[str]]]:
    """All (rule, condition host names) in a ruleset carrying our marker
    description. Rules are owned per shell host — several estates (different
    shells) may share a site, so callers must additionally match the hosts."""
    status, payload, _ = api.request(
        "GET", "/domain-types/rule/collections/all", query={"ruleset_name": ruleset}
    )
    if status != 200:
        api_error(f"listing {ruleset} rules", status, payload)
    marked = []
    for rule in (payload or {}).get("value", []):
        ext = rule.get("extensions", {})
        if ext.get("properties", {}).get("description") != description:
            continue
        cond = (ext.get("conditions") or {}).get("host_name") or {}
        marked.append((rule, cond.get("match_on") or []))
    return marked


def ensure_port_rule(api: CmkApi, delivery_host: str, port: int) -> None:
    for rule, hosts in _marked_rules(api, "agent_ports", RULE_DESCRIPTION):
        if hosts != [delivery_host]:
            continue  # another estate's shell — leave it alone
        if rule["extensions"].get("value_raw") == str(port):
            print("  agent port rule exists")
            return
        # our shell, different port (changed --agent-port)
        api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
        print("  removed stale agent port rule")
    # root folder, not the demo folder: the explicit host-name condition scopes
    # it, and it keeps working if the delivery host already exists elsewhere
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "agent_ports",
            "folder": "/",
            "properties": {"description": RULE_DESCRIPTION, "disabled": False},
            "value_raw": str(port),
            "conditions": {
                "host_name": {"match_on": [delivery_host], "operator": "one_of"},
            },
        },
    )
    if status != 200:
        api_error("creating the agent port rule", status, payload)
    print(f"  created agent port rule ({delivery_host} -> {port})")


# --------------------------------------------------------------------------- #
#  SNMP estate (stored walks rendered by snmp/netsim.py)
# --------------------------------------------------------------------------- #
def ensure_usewalk_rule(api: CmkApi, fqdns: list[str]) -> None:
    """usewalk_hosts ("Simulating SNMP by using a stored SNMP walk") for
    exactly our devices — root folder, explicit host-name condition, marker
    description (same ownership pattern as the agent-port rule)."""
    wanted = sorted(fqdns)
    for rule, hosts in _marked_rules(api, "usewalk_hosts", SNMP_RULE_DESCRIPTION):
        if sorted(hosts) == wanted:
            print("  usewalk rule exists")
            return
        # our marker, stale host set (device roster changed) — replace
        api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
        print("  removed stale usewalk rule")
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "usewalk_hosts",
            "folder": "/",
            "properties": {"description": SNMP_RULE_DESCRIPTION, "disabled": False},
            "value_raw": "True",
            "conditions": {
                "host_name": {"match_on": wanted, "operator": "one_of"},
            },
        },
    )
    if status != 200:
        api_error("creating the usewalk_hosts rule", status, payload)
    print(f"  created usewalk rule ({len(wanted)} devices)")


def ensure_snmp_port_rule(api: CmkApi, root_ident: str, port: int) -> None:
    """Live-SNMP transport: one `snmp_ports` rule on the estate ROOT folder for
    the shared responder port (inherited by the Network subfolders; agent hosts
    ignore it). The community is per-host (device name), so there is NO shared
    community rule. Idempotent by folder+marker; replaced if the port drifts."""
    root_segs = _folder_segs(root_ident)
    want = repr(port)
    found = None
    for rule, _hosts in _marked_rules(api, "snmp_ports", SNMP_PORT_RULE_DESCRIPTION):
        if _folder_segs(rule.get("extensions", {}).get("folder", "")) == root_segs:
            found = rule
            break
    if found is not None:
        if found["extensions"].get("value_raw") == want:
            print("  SNMP port rule exists")
            return
        api.request("DELETE", f"/objects/rule/{found['id']}", etag="*")
        print("  removed stale SNMP port rule")
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "snmp_ports",
            "folder": root_ident,
            "properties": {"description": SNMP_PORT_RULE_DESCRIPTION, "disabled": False},
            "value_raw": want,
            "conditions": {},
        },
    )
    if status != 200:
        api_error("creating the snmp_ports rule", status, payload)
    print(f"  created SNMP port rule ({port})")


def _datasource_command(agent_output_dir: str) -> str:
    """The 'Individual program call' command line: cat the per-host agent file
    the delivery shell writes, keyed by the host name Checkmk substitutes."""
    return f"cat {agent_output_dir.rstrip('/')}/$HOSTNAME$"


def ensure_datasource_rule(api: CmkApi, root_ident: str, agent_output_dir: str) -> None:
    """ONE 'Individual program call instead of agent access' rule on the estate
    ROOT folder — inherited by every subfolder, so a single rule serves the
    whole site: `cat <dir>/$HOSTNAME$`. Only Checkmk-agent hosts run it; the
    no-agent SNMP devices in the same tree ignore it. Owned by folder+marker,
    so a second estate (different root folder) keeps its own rule."""
    command = _datasource_command(agent_output_dir)
    root_segs = _folder_segs(root_ident)
    for rule, _hosts in _marked_rules(api, "datasource_programs", DATASOURCE_RULE_DESCRIPTION):
        if _folder_segs(rule.get("extensions", {}).get("folder", "")) != root_segs:
            continue  # another estate's rule (different root folder)
        if rule["extensions"].get("value_raw") == repr(command):
            print("  datasource program rule exists")
            return
        api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
        print("  removed stale datasource program rule")
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "datasource_programs",
            "folder": root_ident,
            "properties": {"description": DATASOURCE_RULE_DESCRIPTION, "disabled": False},
            "value_raw": repr(command),
            "conditions": {},
        },
    )
    if status != 200:
        api_error("creating the datasource program rule", status, payload)
    print(f"  created datasource program rule ({command!r})")


def ensure_residual_current_rule(api: CmkApi, root_ident: str) -> None:
    """The raritan_px2 residual-current check WARNs by default when a PDU has
    no residual-current sensors at all (warn_missing_data=True) — the replayed
    Raritan model legitimately lacks them, so tune the default off for the
    estate folder, exactly like a real admin would."""
    value = {"warn_missing_data": False, "warn_missing_levels": False}
    root_segs = _folder_segs(root_ident)
    for rule, _hosts in _marked_rules(
        api, "checkgroup_parameters:residual_current", RESIDUAL_RULE_DESCRIPTION
    ):
        if _folder_segs(rule.get("extensions", {}).get("folder", "")) == root_segs:
            print("  residual-current rule exists")
            return
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "checkgroup_parameters:residual_current",
            "folder": root_ident,
            "properties": {"description": RESIDUAL_RULE_DESCRIPTION, "disabled": False},
            "value_raw": repr(value),
            "conditions": {},
        },
    )
    if status != 200:
        api_error("creating the residual-current rule", status, payload)
    print("  created residual-current rule")


def delete_residual_current_rule(api: CmkApi, root_ident: str) -> None:
    root_segs = _folder_segs(root_ident)
    for rule, _hosts in _marked_rules(
        api, "checkgroup_parameters:residual_current", RESIDUAL_RULE_DESCRIPTION
    ):
        if _folder_segs(rule.get("extensions", {}).get("folder", "")) == root_segs:
            api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
            print("  deleted residual-current rule")


def delete_snmp_access_rules(api: CmkApi, root_ident: str) -> None:
    # snmp_ports is what we create now (community is per-host); snmp_communities
    # is only present from an older (v11) deploy — clean it up too if found.
    root_segs = _folder_segs(root_ident)
    for ruleset, desc in (
        ("snmp_communities", SNMP_COMMUNITY_RULE_DESCRIPTION),
        ("snmp_ports", SNMP_PORT_RULE_DESCRIPTION),
    ):
        for rule, _hosts in _marked_rules(api, ruleset, desc):
            if _folder_segs(rule.get("extensions", {}).get("folder", "")) == root_segs:
                api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
                print(f"  deleted SNMP {ruleset} rule")


def delete_datasource_rule(api: CmkApi, root_ident: str) -> None:
    root_segs = _folder_segs(root_ident)
    for rule, _hosts in _marked_rules(api, "datasource_programs", DATASOURCE_RULE_DESCRIPTION):
        if _folder_segs(rule.get("extensions", {}).get("folder", "")) == root_segs:
            api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
            print("  deleted datasource program rule")


def _topo_order(devices: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Devices in parents-before-children order (the REST API validates that
    a named parent exists at creation time). Round-based: emit every device
    whose parent is already emitted or not part of this device set; sorted
    within a round for determinism. An (impossible) parent cycle falls back
    to name order rather than looping forever."""
    remaining = dict(devices)
    out: list[tuple[str, dict[str, Any]]] = []
    while remaining:
        # ready = parent is unset, or is outside this set (base device /
        # already emitted — emitted devices are popped from `remaining`)
        ready = sorted(
            s
            for s, d in remaining.items()
            if not d.get("parent") or d.get("parent") not in remaining
        )
        if not ready:  # cycle — never expected
            ready = sorted(remaining)
        for s in ready:
            out.append((s, remaining.pop(s)))
    return out


def setup_snmp(
    api: CmkApi, args: argparse.Namespace, leaf_for: Callable[[str], str], root_ident: str
) -> list[str]:
    """SNMP devices as no-agent hosts — this IS the estate's network layer:
    sw-core-01 tops the parent topology, everything else (and, in setup(),
    every server) hangs off it. Sorted into the Network/* subfolders via
    leaf_for(). Two transports (from the netsim panel):
      snmp  — netsim answers on ONE shared UDP port; every host shares
              ipaddress 127.0.0.1 but gets a UNIQUE community (the device name)
              that routes the poll — a per-host attribute, plus one folder rule
              for the shared port.
      walk  — legacy stored walks; hosts are no-IP and a usewalk rule points
              the StoredWalk backend at the site's snmpwalks dir.
    Returns the device FQDNs."""
    info = panel_get(args.snmp_panel, optional=True)
    if info is None or "devices" not in info:
        if args.snmp == "on":
            die(
                f"--snmp on, but the netsim panel at {args.snmp_panel} does "
                "not answer — start snmp/netsim.py first (estate.py does "
                "this automatically)"
            )
        print(f"  netsim panel {args.snmp_panel} not reachable — skipping SNMP devices")
        return []
    devices = info["devices"]
    live = info.get("transport", "walk") == "snmp"

    # heal first: interface target states are recorded at discovery
    for short, dev in devices.items():
        if dev.get("incident") and dev.get("state") != "healthy":
            print(f"  healing {short} (was: {dev.get('state')})")
            panel_get(args.snmp_panel, f"/admin/{short}/heal")

    core_fqdn = next((d["fqdn"] for s, d in devices.items() if s.startswith("sw-core")), None)
    fqdn_of = {s: d["fqdn"] for s, d in devices.items()}
    # The REST API rejects a host whose parent does not yet exist, so create in
    # topological order (parents first) — the chains are several levels deep
    # (printer -> floor switch -> distribution -> core).
    fqdns = []
    for short, dev in _topo_order(devices):
        attrs: dict[str, object] = {
            "tag_snmp_ds": "snmp-v2",
            "tag_agent": "no-agent",
        }
        if live:
            # live SNMP: all devices share the loopback IP + port; the UNIQUE
            # community (device name) routes netsim to the right device. The
            # community is a per-host attribute (overrides any rule); "tag_snmp_ds
            # first" is required for it to take, which we set right here.
            attrs["ipaddress"] = "127.0.0.1"
            attrs["tag_address_family"] = "ip-v4-only"
            attrs["snmp_community"] = {
                "type": "v1_v2_community",
                "community": dev.get("community") or short,
            }
        else:
            attrs["tag_address_family"] = "no-ip"  # StoredWalk substitutes .1
        parent_fqdn = fqdn_of.get(dev.get("parent") or "") or core_fqdn
        if parent_fqdn and dev["fqdn"] != parent_fqdn:
            attrs["parents"] = [parent_fqdn]
        dev_role = dev.get("role")
        role = (
            dev_role
            if isinstance(dev_role, str) and dev_role in FOLDER_TAXONOMY
            else _snmp_role(short)
        )
        ensure_host(api, dev["fqdn"], leaf_for(role), attrs)
        fqdns.append(dev["fqdn"])
    if live:
        ensure_snmp_port_rule(api, root_ident, int(info.get("snmp_port") or 161))
    else:
        ensure_usewalk_rule(api, fqdns)
    return fqdns


def _planned_snmp(args: argparse.Namespace) -> list[tuple[str, str | None, str | None]]:
    """The (fqdn, parent, role) triples setup_snmp WOULD create — a read-only,
    heal-free, create-free preview used only for the fingerprint. Mirrors
    setup_snmp's parenting (panel-declared parent, campus core as fallback)."""
    if args.snmp == "off":
        return []
    info = panel_get(args.snmp_panel, optional=True)
    if info is None or "devices" not in info:
        if args.snmp == "on":
            die(
                f"--snmp on, but the netsim panel at {args.snmp_panel} does "
                "not answer — start snmp/netsim.py first (estate.py does "
                "this automatically)"
            )
        return []
    devices = info["devices"]
    core = next((d["fqdn"] for s, d in devices.items() if s.startswith("sw-core")), None)
    fqdn_of = {s: d["fqdn"] for s, d in devices.items()}
    out = []
    for d in devices.values():
        parent = fqdn_of.get(d.get("parent") or "") or core
        out.append((d["fqdn"], parent if parent != d["fqdn"] else None, d.get("role")))
    return out


def snmp_teardown_names(args: argparse.Namespace) -> list[str]:
    info = panel_get(args.snmp_panel, optional=True)
    if not info or "devices" not in info:
        return []
    # Delete children before parents (the mirror of the create constraint):
    # reverse topological order, leaves first, the core last.
    return [d["fqdn"] for _, d in reversed(_topo_order(info["devices"]))]


# --------------------------------------------------------------------------- #
#  BI pack: tier rules -> top rule -> aggregation -> special-agent service
# --------------------------------------------------------------------------- #
def _bi_leaf(fqdn: str, service_regex: str) -> dict[str, Any]:
    return {
        "search": {"type": "empty"},
        "action": {"type": "state_of_service", "host_regex": fqdn, "service_regex": service_regex},
    }


def _bi_call(rule_id: str) -> dict[str, Any]:
    return {
        "search": {"type": "empty"},
        "action": {"type": "call_a_rule", "rule_id": rule_id, "params": {"arguments": []}},
    }


def _ensure_bi_object(api: CmkApi, kind: str, ident: str, body: dict[str, Any], label: str) -> None:
    status, _, _ = api.request("GET", f"/objects/{kind}/{ident}")
    if status == 200:
        print(f"  {label} exists")
        return
    status, payload, _ = api.request("POST", f"/objects/{kind}/{ident}", body=body)
    if status != 200:
        api_error(f"creating {label}", status, payload)
    print(f"  created {label}")


def _bi_rule_body(rule_id: str, title: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": rule_id,
        "pack_id": BI_PACK_ID,
        "nodes": nodes,
        "params": {"arguments": []},
        "node_visualization": {"type": "none", "style_config": {}},
        "properties": {
            "title": title,
            "comment": "",
            "docu_url": "",
            "icon": "",
            "state_messages": {},
        },
        "aggregation_function": {"type": "worst", "count": 1, "restrict_state": 2},
        "computation_options": {"disabled": False},
    }


def ensure_bi_pack(api: CmkApi, fqdn_by_short: dict[str, str]) -> None:
    _ensure_bi_object(
        api,
        "bi_pack",
        BI_PACK_ID,
        {"title": BI_PACK_TITLE, "contact_groups": [], "public": True},
        f"BI pack {BI_PACK_ID}",
    )
    top_nodes = []
    for rule_id, title, leaves in BI_TIERS:
        nodes = [
            _bi_leaf(fqdn_by_short[short], svc) for short, svc in leaves if short in fqdn_by_short
        ]
        if not nodes:
            continue  # tier entirely absent from the carried subset
        _ensure_bi_object(
            api, "bi_rule", rule_id, _bi_rule_body(rule_id, title, nodes), f"BI rule {title!r}"
        )
        top_nodes.append(_bi_call(rule_id))
    if not top_nodes:
        print("  no BI tiers applicable — skipping aggregation")
        return
    _ensure_bi_object(
        api,
        "bi_rule",
        BI_TOP_RULE_ID,
        _bi_rule_body(BI_TOP_RULE_ID, BI_AGGR_TITLE, top_nodes),
        f"BI rule {BI_AGGR_TITLE!r}",
    )
    _ensure_bi_object(
        api,
        "bi_aggregation",
        BI_AGGR_ID,
        {
            "id": BI_AGGR_ID,
            "pack_id": BI_PACK_ID,
            "groups": {"names": [BI_GROUP], "paths": []},
            "node": _bi_call(BI_TOP_RULE_ID),
            "aggregation_visualization": {
                "ignore_rule_styles": False,
                "layout_id": "builtin_default",
                "line_style": "round",
            },
            "computation_options": {
                "disabled": False,
                "escalate_downtimes_as_warn": False,
                "use_hard_states": False,
            },
            "comment": "",
            "customer": None,
        },
        f"BI aggregation {BI_AGGR_TITLE!r}",
    )


def ensure_bi_service_rule(api: CmkApi, delivery_host: str) -> None:
    """special_agents:bi on the shell -> a 'BI Aggregation' service that goes
    red with the payments platform. Requires the shell to be 'all-agents'
    (special agent IN ADDITION TO the TCP agent)."""
    if any(
        hosts == [delivery_host]
        for _, hosts in _marked_rules(api, "special_agents:bi", BI_RULE_DESCRIPTION)
    ):
        print("  BI service rule exists")
        return
    value = {"options": [{"site": ("local", None), "filter": {"aggr_name": [BI_AGGR_TITLE]}}]}
    status, payload, _ = api.request(
        "POST",
        "/domain-types/rule/collections/all",
        body={
            "ruleset": "special_agents:bi",
            "folder": "/",
            "properties": {"description": BI_RULE_DESCRIPTION, "disabled": False},
            "value_raw": repr(value),
            "conditions": {
                "host_name": {"match_on": [delivery_host], "operator": "one_of"},
            },
        },
    )
    if status != 200:
        api_error("creating the BI service rule", status, payload)
    print(f"  created BI service rule ({delivery_host})")


def delete_bi_objects(api: CmkApi) -> None:
    # order matters: aggregation -> top rule -> tier rules -> pack
    for kind, ident in (
        [("bi_aggregation", BI_AGGR_ID), ("bi_rule", BI_TOP_RULE_ID)]
        + [("bi_rule", rid) for rid, _, _ in BI_TIERS]
        + [("bi_pack", BI_PACK_ID)]
    ):
        status, _, _ = api.request("DELETE", f"/objects/{kind}/{ident}", etag="*")
        if status in (200, 204):
            print(f"  deleted {kind} {ident}")


def _delete_marked_rules(
    api: CmkApi, ruleset: str, description: str, estate_hosts: set[str]
) -> None:
    """Delete our marker rules, but only those scoped to hosts of THIS estate
    (the ones being torn down) — a second estate's rules survive."""
    for rule, hosts in _marked_rules(api, ruleset, description):
        if hosts and set(hosts) <= estate_hosts:
            api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
            print(f"  deleted rule {description!r}")


# --------------------------------------------------------------------------- #
#  Discovery + activation (async REST runs — poll until finished)
# --------------------------------------------------------------------------- #
def _wait_for_discovery(api: CmkApi, host: str, timeout: float) -> None:
    deadline = time.time() + timeout
    status = 0
    while time.time() < deadline:
        status, _, _ = api.request(
            "GET", f"/objects/service_discovery_run/{host}/actions/wait-for-completion/invoke"
        )
        if status == 204:
            return
        if status not in (302, 303):
            break
        time.sleep(1)
    die(f"discovery on {host} did not finish (last HTTP status {status})")


def _monitored_service_count(api: CmkApi, host: str) -> int | None:
    """How many services the host already carries in the 'monitored' phase —
    i.e. discovery has run AND the services were accepted into monitoring. A
    cheap read: this endpoint returns the stored discovery state (the
    DiscoveryAction.NONE path) without contacting the data source. Returns 0
    for a never-discovered host, None if the state can't be read."""
    status, payload, _ = api.request("GET", f"/objects/service_discovery/{host}")
    if status != 200:
        return None
    table = ((payload or {}).get("extensions") or {}).get("check_table") or {}
    return sum(
        1 for v in table.values() if (v.get("extensions") or {}).get("service_phase") == "monitored"
    )


def discover(api: CmkApi, host: str, timeout: float = 180.0, *, allow_skip: bool = True) -> None:
    # Skip the (expensive) data-source fetch when the host is already fully
    # discovered — a re-run only needs to touch NEW hosts. NEVER skip-able for
    # the shell (allow_skip=False): its refresh is what re-delivers piggyback to
    # any freshly added child, and the final pass must re-scan for the BI
    # aggregation service. --force also forces a rescan (allow_skip=False).
    if allow_skip:
        n = _monitored_service_count(api, host)
        if n:
            print(f"  {host} already has {n} monitored services — skipping discovery")
            return
    # Two phases: "refresh" actually contacts the data source (async job) —
    # for the shell this fetch is also what stores the piggyback payloads —
    # then the synchronous "fix_all" accepts everything the scan found
    # (fix_all alone only operates on cached data).
    status, payload, _ = api.request(
        "POST",
        "/domain-types/service_discovery_run/actions/start/invoke",
        body={"host_name": host, "mode": "refresh"},
    )
    if status in (302, 303, 409):  # 409: a run is already active — wait for it
        _wait_for_discovery(api, host, timeout)
    elif status != 200:
        api_error(f"starting discovery on {host}", status, payload)
    status, payload, _ = api.request(
        "POST",
        "/domain-types/service_discovery_run/actions/start/invoke",
        body={"host_name": host, "mode": "fix_all"},
    )
    if status not in (200,):
        api_error(f"accepting discovered services on {host}", status, payload)
    print(f"  discovered {host}")


def bulk_discover(api: CmkApi, hostnames: list[str], timeout: float = 3600.0) -> None:
    """One background bulk-discovery job for many hosts — the per-host REST
    round-trip (~2-4 s each) would take ~20 min for a 300-host estate; the
    bulk job scans `bulk_size` hosts per worker batch server-side. The options
    mirror discover()'s refresh+fix_all (accept everything found)."""
    status, payload, headers = api.request(
        "POST",
        "/domain-types/discovery_run/actions/bulk-discovery-start/invoke",
        body={
            "hostnames": hostnames,
            "options": {
                "monitor_undecided_services": True,
                "remove_vanished_services": True,
                "update_service_labels": True,
                "update_service_parameters": True,
                "update_host_labels": True,
            },
            "do_full_scan": True,
            "bulk_size": 10,
            "ignore_errors": True,
        },
    )
    if status not in (200, 303):
        api_error("starting bulk discovery", status, payload)
    # the job id is random (bulk_discovery-<id>) — it's only in the redirect
    location = headers.get("Location") or headers.get("location") or ""
    job_id = location.rstrip("/").rsplit("/", 1)[-1] or "bulk_discovery"
    deadline = time.time() + timeout
    last_print = 0.0
    while time.time() < deadline:
        status, payload, _ = api.request("GET", f"/objects/background_job/{job_id}")
        ext = (payload or {}).get("extensions") or {}
        if status == 200 and not ext.get("active", True):
            state = (ext.get("status") or {}).get("state")
            print(f"  bulk discovery finished ({len(hostnames)} hosts, state {state})")
            return
        if time.time() - last_print > 30:
            print(f"  ... bulk discovery running ({len(hostnames)} hosts, job {job_id})")
            last_print = time.time()
        time.sleep(5)
    die("bulk discovery did not finish in time")


def activate(api: CmkApi, force_foreign: bool, timeout: float = 120.0) -> None:
    status, payload, _ = api.request(
        "POST",
        "/domain-types/activation_run/actions/activate-changes/invoke",
        body={"redirect": False, "sites": [], "force_foreign_changes": force_foreign},
        etag="*",
    )
    if status == 422:  # "no changes to activate" — fine on re-runs
        print("  nothing to activate")
        return
    if status != 200:
        hint = " (foreign changes pending? re-run with --force-foreign)" if status == 401 else ""
        api_error("activating changes" + hint, status, payload)
    run_id = (payload or {}).get("id")
    deadline = time.time() + timeout
    while run_id and time.time() < deadline:
        status, _, _ = api.request(
            "GET", f"/objects/activation_run/{run_id}/actions/wait-for-completion/invoke"
        )
        if status == 204:
            break
        if status not in (302, 303):
            die(f"activation did not finish (HTTP {status})")
        time.sleep(1)
    print("  changes activated")


# --------------------------------------------------------------------------- #
#  Estate fingerprint (skip the slow re-run when nothing changed)
# --------------------------------------------------------------------------- #
def _estate_fingerprint(
    args: argparse.Namespace,
    delivery: str,
    hosts: list[dict[str, Any]],
    snmp_plan: list[tuple[str, str | None, str | None]],
) -> str:
    """A stable digest of the Setup objects setup() would create: the mode,
    the root folder, the shell (host/ip/port), every host with its EFFECTIVE
    parent AND its subfolder, the SNMP device set with parents+subfolders, the
    datasource command (self-hosted), and which BI tiers apply. Two runs share
    a fingerprint iff they would produce byte-for-byte the same configuration —
    so a match means discovery has nothing new to find. Live metric VALUES are
    deliberately absent (they change every poll but never the service set)."""
    datasource = args.mode == "self-hosted"
    carried_fqdns = {h["fqdn"] for h in hosts}
    parent_ok = carried_fqdns | {f for f, _, _ in snmp_plan}
    # (fqdn, effective parent, role/subfolder) — folder moves change the config
    host_rows = sorted(
        [h["fqdn"], h["parent"] if h.get("parent") in parent_ok else None, _host_role(h)]
        for h in hosts
    )
    snmp_rows = sorted(
        [f, p, r if r in FOLDER_TAXONOMY else _snmp_role(f.split(".")[0])] for f, p, r in snmp_plan
    )
    # applicable BI tiers — same "tier present iff a leaf host exists" filter
    # as ensure_bi_pack, so a scaled-down subset changes the fingerprint
    fqdn_by_short = {h["name"]: h["fqdn"] for h in hosts}
    fqdn_by_short.update({f.split(".")[0]: f for f, _, _ in snmp_plan})
    tiers = sorted(
        rid for rid, _, leaves in BI_TIERS if any(short in fqdn_by_short for short, _ in leaves)
    )
    canon = {
        "schema": SCHEMA_VERSION,
        "mode": args.mode,
        "folder": folder_id(args.folder),
        "delivery": [delivery, args.agent_ip, args.agent_port],
        "hosts": host_rows,
        "snmp": snmp_rows,
        "bi_tiers": tiers,
        "datasource": _datasource_command(args.agent_output_dir) if datasource else None,
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return f"v{SCHEMA_VERSION}-{digest}"


def _read_fingerprint(api: CmkApi, delivery: str) -> str | None:
    """The fingerprint stored on the shell host, or None if the host does not
    exist yet (first run) or carries no fingerprint label."""
    host = get_host(api, delivery)
    if host is None:
        return None
    attrs = (host.get("extensions") or {}).get("attributes") or {}
    return (attrs.get("labels") or {}).get(FINGERPRINT_LABEL)


def _write_fingerprint(api: CmkApi, delivery: str, fp: str) -> None:
    """Persist the fingerprint as a shell-host label (merged with any existing
    labels). Creates a pending change picked up by the following activate()."""
    host = get_host(api, delivery)
    attrs = (host.get("extensions") or {}).get("attributes") or {} if host else {}
    labels = dict(attrs.get("labels") or {})
    if labels.get(FINGERPRINT_LABEL) == fp:
        return
    labels[FINGERPRINT_LABEL] = fp
    status, payload, _ = api.request(
        "PUT",
        f"/objects/host_config/{delivery}",
        body={"update_attributes": {"labels": labels}},
        etag="*",
    )
    if status != 200:
        api_error(f"storing the estate fingerprint on {delivery}", status, payload)


# --------------------------------------------------------------------------- #
#  Top-level flows
# --------------------------------------------------------------------------- #
def setup(api: CmkApi, args: argparse.Namespace) -> None:
    print(f"* querying delivery panel {args.panel}")
    info = panel_get(args.panel)
    delivery = info["delivery_host"]
    hosts = info["carried_hosts"]
    missing = [h["name"] for h in hosts if h.get("state") is None]
    if missing:
        die(f"children not up yet (no state): {', '.join(missing)} — wait a few seconds and re-run")
    print(f"  shell {delivery} carrying {len(hosts)} hosts")

    # Fast path: discovery + double activation is the slow part of setup, and
    # when the desired configuration is exactly what we last activated there is
    # nothing new to discover. Compare a fingerprint of the intended Setup
    # state against the one stored on the shell host and bail out in ~1s if it
    # matches (--force overrides). Cheap panel reads only — no healing yet.
    fp = _estate_fingerprint(args, delivery, hosts, _planned_snmp(args))
    if not args.force:
        stored = _read_fingerprint(api, delivery)
        if stored == fp:
            print(
                f"* estate already in sync (fingerprint {fp}) — nothing to do"
                "\n  (--force re-runs discovery/activation; `estate.py heal` "
                "resets demo state)"
            )
            return
        if stored:
            print(f"  configuration changed ({stored} -> {fp}) — reconfiguring")

    print("* healing the estate (services must be discovered healthy)")
    heal_estate(args.panel, hosts)

    # self-hosted => datasource delivery (files + "cat" program); cloud stays
    # on piggyback (no filesystem access). See the module docstring.
    datasource = args.mode == "self-hosted"

    print("* creating Setup objects (folder tree)")
    root_ident = ensure_folder(api, folder_ident(args.folder), "Meridian Retail demo")
    # lazily create + cache each role's leaf folder under the estate root
    _leaf: dict[str, str] = {}

    def leaf_for(role: str) -> str:
        if role not in _leaf:
            _leaf[role] = ensure_folder_chain(api, root_ident, FOLDER_TAXONOMY[role])
        return _leaf[role]

    # The network layer comes FIRST: the REST API rejects a host whose parent
    # does not already exist ("Host not found"), and the shell + every server
    # hang off the campus core, so the SNMP devices must be created before them.
    snmp_fqdns: list[str] = []
    if args.snmp != "off":
        print("* creating the SNMP network devices (stored walks)")
        snmp_fqdns = setup_snmp(api, args, leaf_for, root_ident)
        ensure_residual_current_rule(api, root_ident)
    # sw-core-01 tops the path (its 12 ports are all switch/router uplink
    # trunks); endpoints hang off the access switch sw-access-01, which uplinks
    # to the core — so the core stays the estate's single parentless root.
    # Both are None with --snmp off (no network layer → hosts are parentless).
    core_fqdn = next((f for f in snmp_fqdns if f.split(".")[0].startswith("sw-core")), None)
    access_fqdn = next((f for f in snmp_fqdns if f.split(".")[0].startswith("sw-access")), None)
    shell_parent = access_fqdn or core_fqdn

    # the delivery shell sits at the estate root (the datasource rule lives
    # there too, inherited by every subfolder below) and hangs off the access
    # switch like every server — created after the SNMP layer so the parent exists
    shell_attrs: dict[str, object]
    if datasource:
        # all-agents on the shell too: its Checkmk-agent source is the "cat"
        # datasource program (its own minimal file) AND the BI special agent.
        # No IP / no agent-port rule — nothing is polled over TCP.
        shell_attrs = {
            "tag_agent": "all-agents",
            "tag_address_family": "no-ip",
            "tag_piggyback": "no-piggyback",
        }
    else:
        shell_attrs = {
            "ipaddress": args.agent_ip,
            # all-agents: TCP agent AND the BI special agent below — plain
            # cmk-agent would let a configured special agent REPLACE the TCP
            # fetch and cut off the piggyback delivery
            "tag_agent": "all-agents",
        }
    if shell_parent and shell_parent != delivery:
        shell_attrs["parents"] = [shell_parent]
    ensure_host(api, delivery, root_ident, shell_attrs)
    if not datasource:
        ensure_port_rule(api, delivery, args.agent_port)

    carried_fqdns = {h["fqdn"] for h in hosts}
    parent_ok = carried_fqdns | set(snmp_fqdns)
    # A host whose parent is ANOTHER carried host (a VM naming its hypervisor)
    # must be created after that parent — the API validates parent existence.
    # Hosts parented to a switch (SNMP, already created) sort first; VMs last.
    # Stable sort keeps the roster order within each group.
    attrs: dict[str, object]
    for h in sorted(hosts, key=lambda h: h.get("parent") in carried_fqdns):
        if datasource:
            # Checkmk-agent host whose agent source is the "cat $HOSTNAME$"
            # datasource program (one root-folder rule below, inherited by all
            # subfolders). no-ip: is_tcp comes from the agent tag, not the
            # address family, so the program still runs and the host needs no
            # IP/ping. no-piggyback so it doesn't also pick up an (empty)
            # piggyback source.
            attrs = {
                "tag_agent": "cmk-agent",
                "tag_address_family": "no-ip",
                "tag_piggyback": "no-piggyback",
            }
        else:
            attrs = {
                "tag_agent": "no-agent",
                "tag_piggyback": "piggyback",
                "tag_address_family": "no-ip",
            }
        # only reference parents that actually exist in the site (without
        # the SNMP layer the servers simply have no parent)
        if h.get("parent") in parent_ok:
            attrs["parents"] = [h["parent"]]
        ensure_host(api, h["fqdn"], leaf_for(_host_role(h)), attrs)
    prune_subtree(api, root_ident, {delivery} | carried_fqdns | set(snmp_fqdns))

    if datasource:
        print("* creating the datasource program rule (cat $HOSTNAME$)")
        ensure_datasource_rule(api, root_ident, args.agent_output_dir)

    print("* creating the Payments platform BI pack")
    fqdn_by_short = {h["name"]: h["fqdn"] for h in hosts}
    fqdn_by_short.update({f.split(".")[0]: f for f in snmp_fqdns})
    ensure_bi_pack(api, fqdn_by_short)
    ensure_bi_service_rule(api, delivery)

    print("* running service discovery (shell first — its fetch delivers the piggyback data)")
    # the shell is always (re)scanned: its refresh re-delivers piggyback for
    # any newly added child. The children/SNMP devices skip discovery when
    # they already carry monitored services, so a roster that grew by one host
    # only pays for that one host (--force rescans everything).
    skip = not args.force
    discover(api, delivery, allow_skip=False)
    pending: list[str] = []
    for fqdn in [h["fqdn"] for h in hosts] + snmp_fqdns:
        if skip and _monitored_service_count(api, fqdn):
            continue
        pending.append(fqdn)
    already = len(hosts) + len(snmp_fqdns) - len(pending)
    if already:
        print(f"  {already} hosts already discovered — skipped")
    if len(pending) >= 10:
        # big estates: one server-side bulk job instead of per-host REST calls
        bulk_discover(api, pending)
    else:
        for fqdn in pending:
            discover(api, fqdn, allow_skip=False)

    print("* activating changes")
    activate(api, args.force_foreign)

    # the BI special agent only yields the aggregation once the estate is
    # live in the core, so the business service needs a second look (never
    # skipped — the shell already has services, but not yet the BI one)
    print("* discovering the business service on the shell")
    time.sleep(10)
    discover(api, delivery, allow_skip=False)
    # stamp the fingerprint so the next unchanged `up` short-circuits; this PUT
    # is a pending change the final activate() below flushes together with the
    # business-service discovery
    _write_fingerprint(api, delivery, fp)
    activate(api, args.force_foreign)

    snmp_line = (
        f"\n  - network panel:  {args.snmp_panel}/admin   (break/heal the SNMP devices)"
        if snmp_fqdns
        else ""
    )
    print(f"""
Done. The estate is live:
  - monitoring:     {args.site_url.rstrip("/")}/check_mk/
  - control panel:  {args.panel}/admin   (break/heal any host from one screen){snmp_line}
Piggyback hosts only have data while the delivery container runs and is polled.""")


def teardown(api: CmkApi, args: argparse.Namespace) -> None:
    # Prefer the live roster; fall back to deleting whatever is in the folder.
    print("* removing Setup objects")
    names: list[str] = []
    try:
        info = panel_get(args.panel)
        # children before parents: the servers reference the SNMP campus
        # core, so they go first, then the SNMP devices (core itself last
        # inside snmp_teardown_names), then the shell
        names = [h["fqdn"] for h in reversed(info["carried_hosts"])]
        names += snmp_teardown_names(args)
        names.append(info["delivery_host"])
    except SystemExit:
        print("  (panel unreachable — deleting all hosts under the folder tree instead)")
        root_segs = _folder_segs(folder_ident(args.folder))
        status, payload, _ = api.request("GET", "/domain-types/host_config/collections/all")
        if status == 200:
            names = [
                h["id"]
                for h in (payload or {}).get("value", [])
                if _in_subtree(h.get("extensions", {}).get("folder", ""), root_segs)
            ]
    delete_bi_objects(api)
    _delete_marked_rules(api, "usewalk_hosts", SNMP_RULE_DESCRIPTION, set(names))
    _delete_marked_rules(api, "special_agents:bi", BI_RULE_DESCRIPTION, set(names))
    delete_datasource_rule(api, folder_ident(args.folder))
    delete_residual_current_rule(api, folder_ident(args.folder))
    delete_snmp_access_rules(api, folder_ident(args.folder))
    for name in names:
        status, _, _ = api.request("DELETE", f"/objects/host_config/{name}", etag="*")
        if status == 204:
            print(f"  deleted host {name}")
    _delete_marked_rules(api, "agent_ports", RULE_DESCRIPTION, set(names))
    ident = folder_ident(args.folder)
    if ident != "~":
        status, _, _ = api.request(
            "DELETE",
            f"/objects/folder_config/{ident}",
            query={"delete_mode": "recursive"},
            etag="*",
        )
        if status == 204:
            print(f"  deleted folder {ident}")
        elif status != 404:
            print(f"  WARN: could not delete folder {ident} (HTTP {status})")
    print("* activating changes")
    activate(api, args.force_foreign)
    print("Done — estate removed from the site.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="One-shot Checkmk site setup for the demo estate.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--site-url", help="site base URL, e.g. http://localhost/prod")
    p.add_argument(
        "--site",
        nargs="?",
        const="auto",
        metavar="NAME",
        help="local dev site made by cmk-dev-site/cmk-dev-install-site: "
        "implies --site-url http://localhost/NAME and the dev "
        "credentials cmkadmin/cmk; without NAME picks the newest "
        "running local v* site",
    )
    p.add_argument(
        "--user",
        help="site user (Setup write access; default: automation, or cmkadmin with --site)",
    )
    p.add_argument(
        "--secret",
        default=os.environ.get("CMK_AUTOMATION_SECRET"),
        help="user password/secret (or env CMK_AUTOMATION_SECRET; "
        "prompted if omitted; default with --site: cmk)",
    )
    p.add_argument(
        "--agent-ip", default="127.0.0.1", help="IP of the delivery agent AS SEEN FROM THE SITE"
    )
    p.add_argument("--agent-port", type=int, default=6559, help="published delivery agent port")
    p.add_argument(
        "--panel",
        default="http://localhost:8099",
        help="delivery control panel URL (from where this script runs)",
    )
    p.add_argument(
        "--mode",
        choices=("self-hosted", "cloud"),
        default="self-hosted",
        help="self-hosted = full site-filesystem access (SNMP layer "
        "possible); cloud = Checkmk Cloud/SaaS, agent data only "
        "(forces --snmp off)",
    )
    p.add_argument(
        "--snmp",
        choices=("auto", "on", "off"),
        default="auto",
        help="include the SNMP devices: auto = if the netsim panel "
        "answers, on = require it, off = skip",
    )
    p.add_argument(
        "--snmp-panel",
        default="http://localhost:8101",
        help="netsim control panel URL (snmp/netsim.py)",
    )
    p.add_argument(
        "--folder",
        default="meridian_demo",
        help="Setup ROOT folder for the estate ('/' = site root); "
        "hosts are sorted into role subfolders beneath it",
    )
    p.add_argument(
        "--agent-output-dir",
        default="/var/tmp/cmk-demo-agent-output",
        help="self-hosted: directory holding the per-host agent "
        "files read by the 'cat $HOSTNAME$' datasource program",
    )
    p.add_argument(
        "--force-foreign",
        action="store_true",
        help="activate even if other users have pending changes",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="reconfigure even if the estate fingerprint is "
        "unchanged (re-run discovery + activation)",
    )
    p.add_argument(
        "--remove", action="store_true", help="tear down: delete the hosts, rule and folder again"
    )
    args = p.parse_args(argv)

    # cloud has no site filesystem, so the SNMP layer (stored walk files) is
    # impossible there — force it off (and reject an explicit --snmp on).
    if args.mode == "cloud":
        if args.snmp == "on":
            p.error(
                "--mode cloud cannot deploy the SNMP layer (no site "
                "filesystem for stored walks) — drop --snmp on"
            )
        args.snmp = "off"

    if bool(args.site) == bool(args.site_url):
        p.error("pass either --site (local dev site) or --site-url URL")
    if args.site:
        name = detect_dev_site() if args.site == "auto" else args.site
        args.site_url = f"http://localhost/{name}"
        user = args.user or "cmkadmin"
        secret = args.secret or "cmk"
        print(f"* dev site {name} -> {args.site_url} (user {user})")
    else:
        user = args.user or "automation"
        secret = args.secret or getpass.getpass(f"password/secret for {user}: ")
    api = CmkApi(args.site_url, user, secret)

    status, payload, _ = api.request("GET", "/version")
    if status != 200:
        api_error("authenticating against the site (check --user/--secret)", status, payload)
    print(f"* site {args.site_url} ({(payload or {}).get('versions', {}).get('checkmk', '?')})")

    if args.remove:
        teardown(api, args)
    else:
        setup(api, args)


if __name__ == "__main__":
    main()
