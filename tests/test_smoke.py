"""Smoke tests — prove the modules import and the core paths run. Real unit
tests can grow from here (drop --cov-fail-under into pyproject once they do)."""

# tests exercise snmpserver internals (_selftest, _tlv, ...) by design
# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _sub in ("snmp", "deploy", "fleet", "deploy/delivery"):
    sys.path.insert(0, str(REPO / _sub))


def test_snmpserver_selftest_passes() -> None:
    # The responder's own BER-codec + GET/GETNEXT/GETBULK + community checks.
    import snmpserver

    snmpserver._selftest()  # asserts internally; raises on regression


def test_snmpserver_community_routing_round_trip() -> None:
    import snmpserver as s

    rows = [(".1.3.6.1.2.1.1.5.0", "demo-device"), (".1.3.6.1.2.1.1.3.0", "12345")]
    table = s.Table(rows)

    # a GET for sysName comes back as the OCTET STRING we stored
    vbl = s._tlv(s.T_SEQ, s.enc_oid(s.parse_oid("1.3.6.1.2.1.1.5.0")) + s.enc_null())
    pdu = s.enc_int(1) + s.enc_int(0) + s.enc_int(0) + s._tlv(s.T_SEQ, vbl)
    req = s._tlv(s.T_SEQ, s.enc_int(1) + s.enc_octetstr(b"public") + s._tlv(s.T_GET, pdu))
    assert s.peek_community(req) == "public"
    assert s.handle_message(req, table, community=None) is not None


def test_netsim_renders_a_device_walk() -> None:
    import netsim

    core = netsim.SwCore()
    walk = core.walk(0.0)
    assert "<<<" not in walk  # SNMP walk, not an agent section
    assert ".1.3.6.1.2.1.1.1.0" in walk  # sysDescr present


def test_fleet_profiles_expand() -> None:
    import profiles

    classes = profiles.all_classes()
    assert classes
    assert all("prefix" in c and "count" in c for c in classes)


def test_cmk_setup_imports() -> None:
    import cmk_setup

    assert hasattr(cmk_setup, "SCHEMA_VERSION")


def test_estate_sample_config_loads() -> None:
    # the shipped sample must parse and map to real `up` argparse dests
    sys.path.insert(0, str(REPO))
    import estate

    overrides, env = estate.load_config(str(REPO / "estate.sample.toml"))
    assert set(overrides) <= estate.CONFIG_KEYS
    assert overrides["site"] == "auto"  # `site = true` -> newest dev site
    assert overrides["mode"] == "self-hosted"
    assert isinstance(env, dict)  # [env] table (commented out -> empty)


def test_estate_config_rejects_unknown_key(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO))
    import estate

    bad = tmp_path / "bad.toml"
    bad.write_text('scale = "full"\nscail = "typo"\n')
    with pytest.raises(SystemExit):
        estate.load_config(str(bad))


def test_cross_host_cascade_fires_in_order() -> None:
    # disable persistence so the test doesn't touch /var/tmp
    os.environ["CASCADE_STATE_FILE"] = ""
    import serve  # deploy/delivery/serve.py (the delivery shell)

    casc = serve.Cascade()
    # the story's participants (carried hosts) — db-postgres-01 is the root cause
    assert "db-postgres-01" in casc.participants
    assert casc.steps and casc.steps[0].host == "db-postgres-01"
    # delays are non-decreasing (a timeline, not a jumble)
    delays = [s.delay_s for s in casc.steps]
    assert delays == sorted(delays)

    # idle before trigger
    assert casc.status()["active"] is False

    # arm it, force the clock forward, and tick once: every step becomes due and
    # fires (toggles fail fast against unbound child ports — the ORDERING and
    # bookkeeping are what we assert here, not the child side effects)
    casc.start()
    assert casc.started_at is not None
    casc.started_at -= 10_000  # pretend the whole timeline elapsed
    casc._tick()  # pyright: ignore[reportPrivateUsage]
    st = casc.status()
    assert st["active"] is True
    assert all(s["fired"] for s in st["steps"] if not s.get("skipped"))
    assert st["complete"] is True

    # heal clears the run
    casc.heal()
    assert casc.status()["active"] is False


@pytest.mark.parametrize("script", ["estate.py", "snmp/netsim.py"])
def test_cli_help(script: str) -> None:
    r = subprocess.run(
        [sys.executable, str(REPO / script), "--help"],  # noqa: S603
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
