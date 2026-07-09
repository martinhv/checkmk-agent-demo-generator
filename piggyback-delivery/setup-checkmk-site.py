#!/usr/bin/env python3
"""One-shot Checkmk site setup for the piggyback delivery estate.

Automates section "Set it up in Checkmk" of the README via the Checkmk REST
API — instead of clicking through Setup, run ONE command against a running
delivery container and a Checkmk site:

    ./setup-checkmk-site.py --site-url http://localhost/prod \
        --user automation --secret '...'

On a Checkmk dev box, sites made by cmk-dev-site / cmk-dev-install-site
(cmkadmin/cmk, http://localhost/<site>) need no options at all:

    ./setup-checkmk-site.py --site        # newest running local v* dev site
    ./setup-checkmk-site.py --site v300   # a specific local site

What it does (idempotent — safe to re-run):

  1. asks the delivery control panel (:8099) which hosts it actually carries
     (so an ESTATE_HOSTS subset is handled automatically) and HEALS any
     non-healthy host first — services must be discovered in the healthy
     state (db-postgres-01's SMART check baselines raw values at discovery);
  2. creates a dedicated Setup folder (default: "Meridian Retail demo");
  3. creates the delivery shell as a normal TCP host (agent port via an
     "agent_ports" rule) and every estate host as a pure piggyback host
     (no agent, "always use piggyback data");
  4. runs service discovery — the shell FIRST, because that initial agent
     fetch is what stores the piggyback payloads the other hosts need;
  5. activates the changes.

`--remove` tears the whole thing down again (hosts, rule, folder).

Needs: a site user with write access to Setup ("Administrator" role or an
automation user), and the agent port reachable FROM THE SITE (default
127.0.0.1:6559 — override --agent-ip if the site runs elsewhere).

Stdlib only, like everything else in this repo.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RULE_DESCRIPTION = "Meridian Retail demo: agent port of the piggyback delivery shell"
BI_RULE_DESCRIPTION = "Meridian Retail demo: payments platform business service"

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
    ("meridian_network_path", "Network path", [
        ("core-gw-01", "Interface"),
        ("core-gw-01", "CPU load"),
        ("leaf-sw-01", "Interface"),
        ("leaf-sw-01", "CPU load"),
    ]),
    ("meridian_customer_entry", "Customer entry", [
        ("web-frontend-01", "Interface"),
        ("web-frontend-01", "CPU utilization"),
        ("web-frontend-01", "Memory"),
    ]),
    ("meridian_payment_api", "Payment API", [
        ("payment-api", "Systemd Service Summary"),
        ("payment-api", "CPU load"),
        ("payment-api", "Memory"),
        ("payment-api", "TCP Connections"),
    ]),
    ("meridian_processing", "Processing & cache", [
        ("app-worker-01", "Memory"),
        ("app-worker-01", "Systemd Service Summary"),
        ("app-worker-01", "CPU load"),
        ("app-redis-01", "Redis MERIDIAN_CACHE"),
        ("app-redis-01", "Memory"),
    ]),
    ("meridian_data_layer", "Data layer", [
        ("db-postgres-01", "PostgreSQL"),
        ("db-postgres-01", "Disk IO SUMMARY"),
        ("db-postgres-01", "CPU load"),
        ("db-postgres-02", "PostgreSQL Connections"),
        ("db-postgres-02", "PostgreSQL Instance"),
    ]),
    ("meridian_storage", "Storage", [
        ("fileserver-01", "Filesystem /srv/shares"),
    ]),
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

    def request(self, method: str, path: str, body: dict | None = None,
                query: dict | None = None, etag: str | None = None):
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


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def api_error(what: str, status: int, payload) -> None:
    detail = ""
    if isinstance(payload, dict):
        detail = ": " + "; ".join(
            str(payload[k]) for k in ("title", "detail") if payload.get(k))
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
            f"http://localhost/{name}/check_mk/api/1.0/version", timeout=5)
        return True
    except urllib.error.HTTPError as err:
        return err.code in (401, 200)
    except (urllib.error.URLError, OSError):
        return False


def detect_dev_site() -> str:
    """Newest running local OMD site named like cmk-dev-site makes them (v300,
    v260p1, ...). Newest = creation order via the version symlink's ctime."""
    try:
        candidates = [s for s in os.listdir("/omd/sites")
                      if s.startswith("v") and s[1:2].isdigit()]
    except OSError:
        candidates = []
    if not candidates:
        die("no local v* dev sites found under /omd/sites — pass --site NAME "
            "or --site-url URL")
    candidates.sort(
        key=lambda s: os.lstat(f"/omd/sites/{s}/version").st_ctime, reverse=True)
    for name in candidates:
        if _site_alive(name):
            return name
    die(f"none of the local dev sites ({', '.join(candidates)}) answers on "
        "http://localhost/<site>/ — is one started? (omd start <site>)")
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
#  Delivery control panel (source of truth for the carried roster)
# --------------------------------------------------------------------------- #
def panel_get(panel: str, path: str = "/"):
    try:
        with urllib.request.urlopen(panel.rstrip("/") + path, timeout=10) as r:  # noqa: S310
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as err:
        die(f"cannot reach the delivery control panel at {panel}: {err}\n"
            "       Is the container running?  cd piggyback-delivery && "
            "docker compose up --build -d")


def heal_estate(panel: str, hosts: list[dict]) -> None:
    """Toggle every non-healthy host back to healthy before discovery."""
    unhealthy = [h for h in hosts
                 if h.get("state") != "healthy" and "heal" in h.get("actions", [])]
    for h in unhealthy:
        print(f"  healing {h['name']} (was: {h.get('state')})")
        try:
            urllib.request.urlopen(  # noqa: S310
                f"{panel.rstrip('/')}/admin/{h['name']}/heal", timeout=10).read()
        except (urllib.error.URLError, OSError) as err:
            die(f"healing {h['name']} failed: {err}")
    if unhealthy:
        time.sleep(2)  # let the children settle before the discovery fetch


# --------------------------------------------------------------------------- #
#  Setup objects
# --------------------------------------------------------------------------- #
def folder_id(folder_name: str) -> str:
    # REST API folder idents use "~" as path separator ("~" = root)
    return "~" if folder_name in ("", "/", "~") else "~" + folder_name.strip("/~")

def ensure_folder(api: CmkApi, folder_name: str, title: str) -> str:
    ident = folder_id(folder_name)
    if ident == "~":
        return ident
    status, _, _ = api.request("GET", f"/objects/folder_config/{ident}")
    if status == 200:
        print(f"  folder {ident} exists")
        return ident
    status, payload, _ = api.request(
        "POST", "/domain-types/folder_config/collections/all",
        body={"name": ident.lstrip("~"), "title": title, "parent": "~"})
    if status != 200:
        api_error(f"creating folder {ident}", status, payload)
    print(f"  created folder {ident} ({title!r})")
    return ident


def get_host(api: CmkApi, name: str) -> dict | None:
    status, payload, _ = api.request("GET", f"/objects/host_config/{name}")
    return payload if status == 200 else None


def ensure_host(api: CmkApi, name: str, folder: str, attributes: dict) -> None:
    """Create the host if missing; reconcile the parents attribute if not."""
    existing = get_host(api, name)
    if existing is None:
        status, payload, _ = api.request(
            "POST", "/domain-types/host_config/collections/all",
            body={"host_name": name, "folder": folder, "attributes": attributes})
        if status != 200:
            api_error(f"creating host {name}", status, payload)
        print(f"  created host {name}")
        return
    # reconcile the attributes that carry the topology/datasource contract
    # (may change between script versions); everything else is left as the
    # user configured it
    current = (existing.get("extensions") or {}).get("attributes") or {}
    fix = {k: attributes[k] for k in ("parents", "tag_agent")
           if k in attributes and current.get(k) != attributes[k]}
    if fix:
        status, payload, _ = api.request(
            "PUT", f"/objects/host_config/{name}",
            body={"update_attributes": fix}, etag="*")
        if status != 200:
            api_error(f"updating attributes of {name}", status, payload)
        print(f"  host {name} exists — updated {', '.join(sorted(fix))}")
    else:
        print(f"  host {name} exists")


def _marked_rules(api: CmkApi, ruleset: str,
                  description: str) -> list[tuple[dict, list[str]]]:
    """All (rule, condition host names) in a ruleset carrying our marker
    description. Rules are owned per shell host — several estates (different
    shells) may share a site, so callers must additionally match the hosts."""
    status, payload, _ = api.request(
        "GET", "/domain-types/rule/collections/all",
        query={"ruleset_name": ruleset})
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
        "POST", "/domain-types/rule/collections/all",
        body={
            "ruleset": "agent_ports",
            "folder": "/",
            "properties": {"description": RULE_DESCRIPTION, "disabled": False},
            "value_raw": str(port),
            "conditions": {
                "host_name": {"match_on": [delivery_host], "operator": "one_of"},
            },
        })
    if status != 200:
        api_error("creating the agent port rule", status, payload)
    print(f"  created agent port rule ({delivery_host} -> {port})")


# --------------------------------------------------------------------------- #
#  BI pack: tier rules -> top rule -> aggregation -> special-agent service
# --------------------------------------------------------------------------- #
def _bi_leaf(fqdn: str, service_regex: str) -> dict:
    return {"search": {"type": "empty"},
            "action": {"type": "state_of_service",
                       "host_regex": fqdn, "service_regex": service_regex}}


def _bi_call(rule_id: str) -> dict:
    return {"search": {"type": "empty"},
            "action": {"type": "call_a_rule", "rule_id": rule_id,
                       "params": {"arguments": []}}}


def _ensure_bi_object(api: CmkApi, kind: str, ident: str, body: dict,
                      label: str) -> None:
    status, _, _ = api.request("GET", f"/objects/{kind}/{ident}")
    if status == 200:
        print(f"  {label} exists")
        return
    status, payload, _ = api.request("POST", f"/objects/{kind}/{ident}", body=body)
    if status != 200:
        api_error(f"creating {label}", status, payload)
    print(f"  created {label}")


def _bi_rule_body(rule_id: str, title: str, nodes: list[dict]) -> dict:
    return {
        "id": rule_id,
        "pack_id": BI_PACK_ID,
        "nodes": nodes,
        "params": {"arguments": []},
        "node_visualization": {"type": "none", "style_config": {}},
        "properties": {"title": title, "comment": "", "docu_url": "",
                       "icon": "", "state_messages": {}},
        "aggregation_function": {"type": "worst", "count": 1, "restrict_state": 2},
        "computation_options": {"disabled": False},
    }


def ensure_bi_pack(api: CmkApi, fqdn_by_short: dict[str, str]) -> None:
    _ensure_bi_object(api, "bi_pack", BI_PACK_ID,
                      {"title": BI_PACK_TITLE, "contact_groups": [],
                       "public": True},
                      f"BI pack {BI_PACK_ID}")
    top_nodes = []
    for rule_id, title, leaves in BI_TIERS:
        nodes = [_bi_leaf(fqdn_by_short[short], svc)
                 for short, svc in leaves if short in fqdn_by_short]
        if not nodes:
            continue  # tier entirely absent from the carried subset
        _ensure_bi_object(api, "bi_rule", rule_id,
                          _bi_rule_body(rule_id, title, nodes),
                          f"BI rule {title!r}")
        top_nodes.append(_bi_call(rule_id))
    if not top_nodes:
        print("  no BI tiers applicable — skipping aggregation")
        return
    _ensure_bi_object(api, "bi_rule", BI_TOP_RULE_ID,
                      _bi_rule_body(BI_TOP_RULE_ID, BI_AGGR_TITLE, top_nodes),
                      f"BI rule {BI_AGGR_TITLE!r}")
    _ensure_bi_object(api, "bi_aggregation", BI_AGGR_ID, {
        "id": BI_AGGR_ID,
        "pack_id": BI_PACK_ID,
        "groups": {"names": [BI_GROUP], "paths": []},
        "node": _bi_call(BI_TOP_RULE_ID),
        "aggregation_visualization": {"ignore_rule_styles": False,
                                      "layout_id": "builtin_default",
                                      "line_style": "round"},
        "computation_options": {"disabled": False,
                                "escalate_downtimes_as_warn": False,
                                "use_hard_states": False},
        "comment": "",
        "customer": None,
    }, f"BI aggregation {BI_AGGR_TITLE!r}")


def ensure_bi_service_rule(api: CmkApi, delivery_host: str) -> None:
    """special_agents:bi on the shell -> a 'BI Aggregation' service that goes
    red with the payments platform. Requires the shell to be 'all-agents'
    (special agent IN ADDITION TO the TCP agent)."""
    if any(hosts == [delivery_host] for _, hosts in
           _marked_rules(api, "special_agents:bi", BI_RULE_DESCRIPTION)):
        print("  BI service rule exists")
        return
    value = {"options": [{"site": ("local", None),
                          "filter": {"aggr_name": [BI_AGGR_TITLE]}}]}
    status, payload, _ = api.request(
        "POST", "/domain-types/rule/collections/all",
        body={
            "ruleset": "special_agents:bi",
            "folder": "/",
            "properties": {"description": BI_RULE_DESCRIPTION, "disabled": False},
            "value_raw": repr(value),
            "conditions": {
                "host_name": {"match_on": [delivery_host], "operator": "one_of"},
            },
        })
    if status != 200:
        api_error("creating the BI service rule", status, payload)
    print(f"  created BI service rule ({delivery_host})")


def delete_bi_objects(api: CmkApi) -> None:
    # order matters: aggregation -> top rule -> tier rules -> pack
    for kind, ident in ([("bi_aggregation", BI_AGGR_ID),
                         ("bi_rule", BI_TOP_RULE_ID)]
                        + [("bi_rule", rid) for rid, _, _ in BI_TIERS]
                        + [("bi_pack", BI_PACK_ID)]):
        status, _, _ = api.request("DELETE", f"/objects/{kind}/{ident}", etag="*")
        if status in (200, 204):
            print(f"  deleted {kind} {ident}")


def _delete_marked_rules(api: CmkApi, ruleset: str, description: str,
                         estate_hosts: set[str]) -> None:
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
            "GET",
            f"/objects/service_discovery_run/{host}/actions/wait-for-completion/invoke")
        if status == 204:
            return
        if status not in (302, 303):
            break
        time.sleep(1)
    die(f"discovery on {host} did not finish (last HTTP status {status})")


def discover(api: CmkApi, host: str, timeout: float = 180.0) -> None:
    # Two phases: "refresh" actually contacts the data source (async job) —
    # for the shell this fetch is also what stores the piggyback payloads —
    # then the synchronous "fix_all" accepts everything the scan found
    # (fix_all alone only operates on cached data).
    status, payload, _ = api.request(
        "POST", "/domain-types/service_discovery_run/actions/start/invoke",
        body={"host_name": host, "mode": "refresh"})
    if status in (302, 303, 409):  # 409: a run is already active — wait for it
        _wait_for_discovery(api, host, timeout)
    elif status != 200:
        api_error(f"starting discovery on {host}", status, payload)
    status, payload, _ = api.request(
        "POST", "/domain-types/service_discovery_run/actions/start/invoke",
        body={"host_name": host, "mode": "fix_all"})
    if status not in (200,):
        api_error(f"accepting discovered services on {host}", status, payload)
    print(f"  discovered {host}")


def activate(api: CmkApi, force_foreign: bool, timeout: float = 120.0) -> None:
    status, payload, _ = api.request(
        "POST", "/domain-types/activation_run/actions/activate-changes/invoke",
        body={"redirect": False, "sites": [], "force_foreign_changes": force_foreign},
        etag="*")
    if status == 422:  # "no changes to activate" — fine on re-runs
        print("  nothing to activate")
        return
    if status != 200:
        hint = (" (foreign changes pending? re-run with --force-foreign)"
                if status == 401 else "")
        api_error("activating changes" + hint, status, payload)
    run_id = (payload or {}).get("id")
    deadline = time.time() + timeout
    while run_id and time.time() < deadline:
        status, _, _ = api.request(
            "GET",
            f"/objects/activation_run/{run_id}/actions/wait-for-completion/invoke")
        if status == 204:
            break
        if status not in (302, 303):
            die(f"activation did not finish (HTTP {status})")
        time.sleep(1)
    print("  changes activated")


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
        die(f"children not up yet (no state): {', '.join(missing)} — "
            "wait a few seconds and re-run")
    print(f"  shell {delivery} carrying {len(hosts)} hosts")

    print("* healing the estate (services must be discovered healthy)")
    heal_estate(args.panel, hosts)

    print("* creating Setup objects")
    folder = ensure_folder(api, args.folder, "Meridian Retail demo")
    ensure_host(api, delivery, folder, {
        "ipaddress": args.agent_ip,
        # all-agents: TCP agent AND the BI special agent below — plain
        # cmk-agent would let a configured special agent REPLACE the TCP
        # fetch and cut off the piggyback delivery
        "tag_agent": "all-agents",
    })
    ensure_port_rule(api, delivery, args.agent_port)
    carried_fqdns = {h["fqdn"] for h in hosts}
    for h in hosts:  # roster order is parents-first (network devices lead)
        attrs = {
            "tag_agent": "no-agent",
            "tag_piggyback": "piggyback",
            "tag_address_family": "no-ip",
        }
        # only reference parents that are actually carried (ESTATE_HOSTS
        # subsets may omit the network devices)
        if h.get("parent") in carried_fqdns:
            attrs["parents"] = [h["parent"]]
        ensure_host(api, h["fqdn"], folder, attrs)

    print("* creating the Payments platform BI pack")
    ensure_bi_pack(api, {h["name"]: h["fqdn"] for h in hosts})
    ensure_bi_service_rule(api, delivery)

    print("* running service discovery (shell first — its fetch delivers the "
          "piggyback data)")
    discover(api, delivery)
    for h in hosts:
        discover(api, h["fqdn"])

    print("* activating changes")
    activate(api, args.force_foreign)

    # the BI special agent only yields the aggregation once the estate is
    # live in the core, so the business service needs a second look
    print("* discovering the business service on the shell")
    time.sleep(10)
    discover(api, delivery)
    activate(api, args.force_foreign)

    print(f"""
Done. The estate is live:
  - monitoring:     {args.site_url.rstrip('/')}/check_mk/
  - control panel:  {args.panel}/admin   (break/heal any host from one screen)
Piggyback hosts only have data while the delivery container runs and is polled.""")


def teardown(api: CmkApi, args: argparse.Namespace) -> None:
    # Prefer the live roster; fall back to deleting whatever is in the folder.
    print("* removing Setup objects")
    names: list[str] = []
    try:
        info = panel_get(args.panel)
        # children before their parents (roster is parents-first), shell last
        names = [h["fqdn"] for h in reversed(info["carried_hosts"])]
        names.append(info["delivery_host"])
    except SystemExit:
        print("  (panel unreachable — deleting all hosts in the folder instead)")
        status, payload, _ = api.request(
            "GET", "/domain-types/host_config/collections/all")
        if status == 200:
            names = [h["id"] for h in (payload or {}).get("value", [])
                     if h.get("extensions", {}).get("folder", "").strip("/~")
                     == folder_id(args.folder).lstrip("~")]
    delete_bi_objects(api)
    _delete_marked_rules(api, "special_agents:bi", BI_RULE_DESCRIPTION, set(names))
    for name in names:
        status, _, _ = api.request("DELETE", f"/objects/host_config/{name}", etag="*")
        if status == 204:
            print(f"  deleted host {name}")
    _delete_marked_rules(api, "agent_ports", RULE_DESCRIPTION, set(names))
    ident = folder_id(args.folder)
    if ident != "~":
        status, _, _ = api.request(
            "DELETE", f"/objects/folder_config/{ident}",
            query={"delete_mode": "recursive"}, etag="*")
        if status == 204:
            print(f"  deleted folder {ident}")
        elif status != 404:
            print(f"  WARN: could not delete folder {ident} (HTTP {status})")
    print("* activating changes")
    activate(api, args.force_foreign)
    print("Done — estate removed from the site.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="One-shot Checkmk site setup for the piggyback demo estate.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--site-url",
                   help="site base URL, e.g. http://localhost/prod")
    p.add_argument("--site", nargs="?", const="auto", metavar="NAME",
                   help="local dev site made by cmk-dev-site/cmk-dev-install-site: "
                        "implies --site-url http://localhost/NAME and the dev "
                        "credentials cmkadmin/cmk; without NAME picks the newest "
                        "running local v* site")
    p.add_argument("--user", help="site user (Setup write access; default: "
                                  "automation, or cmkadmin with --site)")
    p.add_argument("--secret", default=os.environ.get("CMK_AUTOMATION_SECRET"),
                   help="user password/secret (or env CMK_AUTOMATION_SECRET; "
                        "prompted if omitted; default with --site: cmk)")
    p.add_argument("--agent-ip", default="127.0.0.1",
                   help="IP of the delivery agent AS SEEN FROM THE SITE")
    p.add_argument("--agent-port", type=int, default=6559,
                   help="published delivery agent port")
    p.add_argument("--panel", default="http://localhost:8099",
                   help="delivery control panel URL (from where this script runs)")
    p.add_argument("--folder", default="meridian_demo",
                   help="Setup folder for the estate ('/' = root)")
    p.add_argument("--force-foreign", action="store_true",
                   help="activate even if other users have pending changes")
    p.add_argument("--remove", action="store_true",
                   help="tear down: delete the hosts, rule and folder again")
    args = p.parse_args()

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
        api_error("authenticating against the site (check --user/--secret)",
                  status, payload)
    print(f"* site {args.site_url} ({(payload or {}).get('versions', {}).get('checkmk', '?')})")

    if args.remove:
        teardown(api, args)
    else:
        setup(api, args)


if __name__ == "__main__":
    main()
