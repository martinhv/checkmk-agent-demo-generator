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


def host_exists(api: CmkApi, name: str) -> bool:
    status, _, _ = api.request("GET", f"/objects/host_config/{name}")
    return status == 200


def ensure_host(api: CmkApi, name: str, folder: str, attributes: dict) -> bool:
    """Create the host if missing; returns True if it was created."""
    if host_exists(api, name):
        print(f"  host {name} exists")
        return False
    status, payload, _ = api.request(
        "POST", "/domain-types/host_config/collections/all",
        body={"host_name": name, "folder": folder, "attributes": attributes})
    if status != 200:
        api_error(f"creating host {name}", status, payload)
    print(f"  created host {name}")
    return True


def ensure_port_rule(api: CmkApi, delivery_host: str, port: int) -> None:
    status, payload, _ = api.request(
        "GET", "/domain-types/rule/collections/all",
        query={"ruleset_name": "agent_ports"})
    if status != 200:
        api_error("listing agent_ports rules", status, payload)
    for rule in (payload or {}).get("value", []):
        ext = rule.get("extensions", {})
        if ext.get("properties", {}).get("description") != RULE_DESCRIPTION:
            continue
        cond = (ext.get("conditions") or {}).get("host_name") or {}
        if (ext.get("value_raw") == str(port)
                and cond.get("match_on") == [delivery_host]):
            print("  agent port rule exists")
            return
        # same marker, different port/host (changed --agent-port or domain)
        api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
        print("  removed stale agent port rule")
        break
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


def delete_port_rule(api: CmkApi) -> None:
    status, payload, _ = api.request(
        "GET", "/domain-types/rule/collections/all",
        query={"ruleset_name": "agent_ports"})
    if status != 200:
        return
    for rule in (payload or {}).get("value", []):
        props = rule.get("extensions", {}).get("properties", {})
        if props.get("description") == RULE_DESCRIPTION:
            api.request("DELETE", f"/objects/rule/{rule['id']}", etag="*")
            print("  deleted agent port rule")


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
        "tag_agent": "cmk-agent",
    })
    ensure_port_rule(api, delivery, args.agent_port)
    for h in hosts:
        ensure_host(api, h["fqdn"], folder, {
            "tag_agent": "no-agent",
            "tag_piggyback": "piggyback",
            "tag_address_family": "no-ip",
        })

    print("* running service discovery (shell first — its fetch delivers the "
          "piggyback data)")
    discover(api, delivery)
    for h in hosts:
        discover(api, h["fqdn"])

    print("* activating changes")
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
        names = [info["delivery_host"]] + [h["fqdn"] for h in info["carried_hosts"]]
    except SystemExit:
        print("  (panel unreachable — deleting all hosts in the folder instead)")
        status, payload, _ = api.request(
            "GET", "/domain-types/host_config/collections/all")
        if status == 200:
            names = [h["id"] for h in (payload or {}).get("value", [])
                     if h.get("extensions", {}).get("folder", "").strip("/~")
                     == folder_id(args.folder).lstrip("~")]
    for name in names:
        status, _, _ = api.request("DELETE", f"/objects/host_config/{name}", etag="*")
        if status == 204:
            print(f"  deleted host {name}")
    delete_port_rule(api)
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
