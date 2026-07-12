#!/usr/bin/env python3
"""A tiny, stdlib-only SNMP v2c responder — enough for Checkmk to poll.

Why this exists: the alternative (Checkmk's "stored walk" backend) reads walk
files from the site-owned `var/check_mk/snmpwalks/` dir, so writing them means
being the site user -> sudo on every deploy. Answering SNMP live over UDP
instead needs NO site filesystem access at all: netsim binds a high UDP port
per device on 127.0.0.0/8 (the whole /8 routes to loopback, no root, no added
addresses) and Checkmk polls it like any real device. That also unblocks the
SNMP layer in cloud mode.

Scope: SNMP v2c only (Checkmk walks with GETBULK). GET / GETNEXT / GETBULK,
definite-length BER, the value types Checkmk actually needs. Values are served
as OCTET STRING regardless of the "real" MIB type: Checkmk's SNMP layer hands
check plugins the STRINGIFIED value, and an OCTET STRING "12345" stringifies
identically to a Counter32 12345 — so no per-OID type table is needed. Binary
values (MACs etc., stored as `"AA BB "` uppercase-hex) are emitted as their raw
bytes so Checkmk renders them back to the same hex.

Self-test: `python3 snmpserver.py --selftest` (no external tools needed).
"""

from __future__ import annotations

import bisect
import contextlib
import re
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
#  BER / ASN.1 — just the pieces SNMP uses, definite length only
# --------------------------------------------------------------------------- #
# universal tags
T_INT = 0x02
T_OCTETSTR = 0x04
T_NULL = 0x05
T_OID = 0x06
T_SEQ = 0x30
# context / application tags used by SNMP
T_GET = 0xA0
T_GETNEXT = 0xA1
T_RESPONSE = 0xA2
T_GETBULK = 0xA5
T_NOSUCHOBJECT = 0x80  # context [0] — implicit NULL
T_NOSUCHINSTANCE = 0x81  # context [1]
T_ENDOFMIBVIEW = 0x82  # context [2]

_HEX_VALUE = re.compile(r'^"((?:[0-9A-Fa-f]{2} )*)"$')


class BERError(ValueError):
    pass


def _enc_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = []
    while n:
        body.append(n & 0xFF)
        n >>= 8
    body.reverse()
    return bytes([0x80 | len(body)]) + bytes(body)


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _enc_len(len(body)) + body


def enc_int(value: int) -> bytes:
    # minimal two's-complement, big-endian, at least one byte
    if value == 0:
        body = b"\x00"
    else:
        n, body = value, bytearray()
        if n > 0:
            while n:
                body.append(n & 0xFF)
                n >>= 8
            if body[-1] & 0x80:  # keep it positive
                body.append(0x00)
        else:
            n = -value
            bits = n.bit_length() + 1
            nbytes = (bits + 7) // 8
            val = (1 << (8 * nbytes)) + value  # two's complement
            for _ in range(nbytes):
                body.append(val & 0xFF)
                val >>= 8
        body.reverse()
    return _tlv(T_INT, bytes(body))


def enc_octetstr(data: bytes) -> bytes:
    return _tlv(T_OCTETSTR, data)


def enc_null() -> bytes:
    return _tlv(T_NULL, b"")


def oid_to_bytes(oid: tuple[int, ...]) -> bytes:
    if len(oid) < 2:
        raise BERError(f"OID too short: {oid}")
    first = 40 * oid[0] + oid[1]
    out = bytearray(_subid(first))
    for sub in oid[2:]:
        out += _subid(sub)
    return bytes(out)


def _subid(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    chunks = [n & 0x7F]
    n >>= 7
    while n:
        chunks.append((n & 0x7F) | 0x80)
        n >>= 7
    chunks.reverse()
    return bytes(chunks)


def enc_oid(oid: tuple[int, ...]) -> bytes:
    return _tlv(T_OID, oid_to_bytes(oid))


def _read_tlv(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """Return (tag, body, next_pos)."""
    if pos + 1 > len(buf):
        raise BERError("truncated tag")
    tag = buf[pos]
    pos += 1
    if pos >= len(buf):
        raise BERError("truncated length")
    first = buf[pos]
    pos += 1
    if first < 0x80:
        length = first
    else:
        nbytes = first & 0x7F
        if nbytes == 0 or pos + nbytes > len(buf):
            raise BERError("bad long-form length")
        length = int.from_bytes(buf[pos : pos + nbytes], "big")
        pos += nbytes
    if pos + length > len(buf):
        raise BERError("truncated body")
    return tag, buf[pos : pos + length], pos + length


def dec_int(body: bytes) -> int:
    if not body:
        return 0
    return int.from_bytes(body, "big", signed=True)


def bytes_to_oid(body: bytes) -> tuple[int, ...]:
    if not body:
        raise BERError("empty OID")
    first = body[0]
    out = [first // 40, first % 40]
    n, i = 0, 1
    while i < len(body):
        b = body[i]
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80):
            out.append(n)
            n = 0
        i += 1
    return tuple(out)


# --------------------------------------------------------------------------- #
#  OID helpers
# --------------------------------------------------------------------------- #
def parse_oid(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.strip().strip(".").split("."))


def encode_value(walk_value: str) -> bytes:
    """A stored-walk value string -> the OCTET STRING payload bytes.

    Binary values are stored as uppercase hex pairs with a trailing space
    inside quotes (`"B2 E0 7D "`); everything else is the literal printable
    text. Empty -> empty octet string."""
    m = _HEX_VALUE.match(walk_value)
    if m:
        hexpart = m.group(1).strip()
        if not hexpart:
            return b""
        return bytes(int(h, 16) for h in hexpart.split())
    return walk_value.encode("utf-8", "replace")


# --------------------------------------------------------------------------- #
#  PDU handling
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class VarBind:
    oid: tuple[int, ...]
    value_tlv: bytes  # already-encoded value (TLV)

    def encode(self) -> bytes:
        return _tlv(T_SEQ, enc_oid(self.oid) + self.value_tlv)


def _parse_varbinds(body: bytes) -> list[tuple[int, ...]]:
    """Return the list of requested OIDs (values are NULL in a request)."""
    oids, pos = [], 0
    while pos < len(body):
        tag, vb, pos = _read_tlv(body, pos)
        if tag != T_SEQ:
            raise BERError("varbind not a sequence")
        otag, obody, _ = _read_tlv(vb, 0)
        if otag != T_OID:
            raise BERError("varbind name not an OID")
        oids.append(bytes_to_oid(obody))
    return oids


class Table:
    """A device's current OID space: sorted (oid_tuple, value_tlv) pairs, with
    fast lexicographic 'next' lookup for GETNEXT/GETBULK.

    Built once from the full walk, then refreshed in place with patch(): a
    replayed device's OID SET never changes and ~96% of its values are static
    (only counters/uptime move), so re-parsing/encoding/sorting all 17k OIDs
    every poll is wasted work. patch() re-encodes just the dynamic rows."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        pairs = {}
        for oid_s, val_s in rows:
            pairs[parse_oid(oid_s)] = enc_octetstr(encode_value(val_s))
        self.oids = sorted(pairs)
        self.value = pairs

    def patch(self, rows: list[tuple[str, str]]) -> None:
        """Overwrite the values of the given (already-present) OIDs in place.
        The sorted OID list is untouched (the set is stable)."""
        for oid_s, val_s in rows:
            self.value[parse_oid(oid_s)] = enc_octetstr(encode_value(val_s))

    def get(self, oid: tuple[int, ...]) -> bytes | None:
        return self.value.get(oid)

    def next(self, oid: tuple[int, ...]) -> tuple[int, ...] | None:
        # first stored OID strictly greater than `oid` (lexicographic)
        i = bisect.bisect_right(self.oids, oid)
        return self.oids[i] if i < len(self.oids) else None


def peek_community(data: bytes) -> str | None:
    """Extract the v2c community from a request without handling it — the
    responder routes to a device by community (one shared UDP port, a unique
    community per host), so it must read the community before picking a table."""
    try:
        _, body, _ = _read_tlv(data, 0)
        vtag, vbody, pos = _read_tlv(body, 0)
        if vtag != T_INT or dec_int(vbody) != 1:
            return None
        ctag, cbody, _ = _read_tlv(body, pos)
        if ctag != T_OCTETSTR:
            return None
        return cbody.decode("latin1")
    except BERError:
        return None


def handle_message(data: bytes, table: Table, community: str | None = None) -> bytes | None:
    """Parse a v2c request and return the encoded response (or None to drop)."""
    tag, body, _ = _read_tlv(data, 0)
    if tag != T_SEQ:
        return None
    pos = 0
    vtag, vbody, pos = _read_tlv(body, pos)  # version
    if vtag != T_INT or dec_int(vbody) != 1:  # 1 == v2c
        return None
    ctag, cbody, pos = _read_tlv(body, pos)  # community
    if ctag != T_OCTETSTR:
        return None
    if community is not None and cbody.decode("latin1") != community:
        return None
    ptag, pbody, _ = _read_tlv(body, pos)  # PDU
    if ptag not in (T_GET, T_GETNEXT, T_GETBULK):
        return None

    p = 0
    _, rbody, p = _read_tlv(pbody, p)  # request-id
    request_id = dec_int(rbody)
    _, n1body, p = _read_tlv(pbody, p)  # error-status / non-repeaters
    _, n2body, p = _read_tlv(pbody, p)  # error-index / max-repetitions
    _, vbbody, _ = _read_tlv(pbody, p)  # varbind list
    oids = _parse_varbinds(vbbody)

    if ptag == T_GET:
        out = [VarBind(o, table.get(o) or _tlv(T_NOSUCHINSTANCE, b"")) for o in oids]
    elif ptag == T_GETNEXT:
        out = [_next_vb(table, o) for o in oids]
    else:  # GETBULK
        non_rep = max(0, dec_int(n1body))
        max_rep = max(0, dec_int(n2body))
        out = []
        for o in oids[:non_rep]:
            out.append(_next_vb(table, o))
        for o in oids[non_rep:]:
            cur = o
            for _ in range(max_rep):
                vb = _next_vb(table, cur)
                out.append(vb)
                if vb.value_tlv == _tlv(T_ENDOFMIBVIEW, b""):
                    break
                cur = vb.oid

    resp_pdu = (
        enc_int(request_id)
        + enc_int(0)
        + enc_int(0)
        + _tlv(T_SEQ, b"".join(vb.encode() for vb in out))
    )
    return _tlv(T_SEQ, enc_int(1) + enc_octetstr(cbody) + _tlv(T_RESPONSE, resp_pdu))


def _next_vb(table: Table, oid: tuple[int, ...]) -> VarBind:
    nxt = table.next(oid)
    if nxt is None:
        return VarBind(oid, _tlv(T_ENDOFMIBVIEW, b""))
    val = table.get(nxt)
    assert val is not None  # nxt came from table.next(): it's a stored key
    return VarBind(nxt, val)


# --------------------------------------------------------------------------- #
#  UDP server — ONE socket, devices distinguished by community string
# --------------------------------------------------------------------------- #
class SnmpServer:
    """Answers SNMP on a single `bind:port`. Every device has a unique v2c
    community, so one shared port serves them all — the responder reads the
    community and routes: `table_for(community)` returns that device's current
    `Table` (built + cached by the caller) or None for an unknown community.

    A single port means netsim ports-maps into a normal container like the
    delivery gateway (no --network host for ~120 loopback IPs). A pool of
    worker threads reads the shared socket (the kernel hands each datagram to
    one), so concurrent bulk-discovery walks still interleave."""

    def __init__(
        self, bind: str, port: int, table_for: Callable[[str], Table | None], workers: int = 16
    ) -> None:
        self.bind = bind
        self.port = port
        self.table_for = table_for
        self.workers = workers
        self.sock: socket.socket | None = None

    def serve_forever(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        self.sock = s
        threads = [threading.Thread(target=self._serve, daemon=True) for _ in range(self.workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _serve(self) -> None:
        sock = self.sock
        assert sock is not None  # set by serve_forever before threads start
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                return
            try:
                community = peek_community(data)
                table = self.table_for(community) if community else None
                if table is None:
                    continue  # unknown community -> silently drop
                reply = handle_message(data, table, community=None)
            except Exception:
                continue
            if reply is not None:
                with contextlib.suppress(OSError):
                    sock.sendto(reply, addr)


# --------------------------------------------------------------------------- #
#  Self-test — no external tools
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    # 1. codec round-trips
    for v in (0, 1, 127, 128, 255, 256, 65535, -1, -128, 2**31 - 1, 2**32):
        t, b, _ = _read_tlv(enc_int(v), 0)
        assert t == T_INT and dec_int(b) == v, f"int {v}"
    for o in ("1.3.6.1.2.1.1.1.0", "1.3.6.1.4.1.318.1.4.2.6.1.4.2", "0.0"):
        oid = parse_oid(o)
        t, b, _ = _read_tlv(enc_oid(oid), 0)
        assert t == T_OID and bytes_to_oid(b) == oid, f"oid {o}"

    # 2. decode a hand-built real SNMPv2c GET (sysDescr.0, community "public")
    pkt = bytes.fromhex(
        "302902010104067075626c6963a01c02040000002a020100020100300e300c06082b060102010101000500"
    )
    tag, body, _ = _read_tlv(pkt, 0)
    assert tag == T_SEQ
    _, _, p = _read_tlv(body, 0)
    _, comm, p = _read_tlv(body, p)
    assert comm == b"public"
    ptag, pbody, _ = _read_tlv(body, p)
    assert ptag == T_GET
    _, rid, pp = _read_tlv(pbody, 0)
    assert dec_int(rid) == 0x2A
    _, _, pp = _read_tlv(pbody, pp)
    _, _, pp = _read_tlv(pbody, pp)
    _, vbs, _ = _read_tlv(pbody, pp)
    assert _parse_varbinds(vbs) == [parse_oid("1.3.6.1.2.1.1.1.0")]

    # 3. GET / GETNEXT / GETBULK against a small table
    rows = [
        (".1.3.6.1.2.1.1.1.0", "Test Device"),
        (".1.3.6.1.2.1.1.3.0", "12345"),
        (".1.3.6.1.2.1.2.2.1.6.1", '"B2 E0 7D 2C 4D 15 "'),
        (".1.3.6.1.2.1.2.2.1.10.1", "9998887776"),
    ]
    table = Table(rows)

    def build(pdu_tag, oids, n1=0, n2=0, community=b"public"):
        vbl = b"".join(_tlv(T_SEQ, enc_oid(parse_oid(o)) + enc_null()) for o in oids)
        pdu = enc_int(7) + enc_int(n1) + enc_int(n2) + _tlv(T_SEQ, vbl)
        return _tlv(T_SEQ, enc_int(1) + enc_octetstr(community) + _tlv(pdu_tag, pdu))

    def resp_varbinds(reply):
        _, body, _ = _read_tlv(reply, 0)
        _, _, p = _read_tlv(body, 0)
        _, _, p = _read_tlv(body, p)
        _, pdu, _ = _read_tlv(body, p)
        pp = 0
        for _ in range(3):
            _, _, pp = _read_tlv(pdu, pp)
        _, vbl, _ = _read_tlv(pdu, pp)
        out, q = [], 0
        while q < len(vbl):
            _, vb, q = _read_tlv(vbl, q)
            _, ob, r = _read_tlv(vb, 0)
            vt, vbody, _ = _read_tlv(vb, r)
            out.append((bytes_to_oid(ob), vt, vbody))
        return out

    # GET exact
    vb = resp_varbinds(handle_message(build(T_GET, [".1.3.6.1.2.1.1.1.0"]), table, "public"))
    assert vb[0][1] == T_OCTETSTR and vb[0][2] == b"Test Device", vb

    # GET binary -> raw bytes
    vb = resp_varbinds(handle_message(build(T_GET, [".1.3.6.1.2.1.2.2.1.6.1"]), table, "public"))
    assert vb[0][2] == bytes.fromhex("B2E07D2C4D15"), vb

    # GET missing -> noSuchInstance
    vb = resp_varbinds(handle_message(build(T_GET, [".1.3.6.1.2.1.99.0"]), table, "public"))
    assert vb[0][1] == T_NOSUCHINSTANCE, vb

    # GETNEXT walks in order
    vb = resp_varbinds(handle_message(build(T_GETNEXT, [".1.3.6.1.2.1.1.1.0"]), table, "public"))
    assert vb[0][0] == parse_oid("1.3.6.1.2.1.1.3.0"), vb

    # GETBULK returns the whole table then endOfMibView
    reply = handle_message(build(T_GETBULK, [".1.3.6.1.2.1.1.0"], n1=0, n2=10), table, "public")
    vb = resp_varbinds(reply)
    assert [v[0] for v in vb[:4]] == table.oids, vb
    assert vb[4][1] == T_ENDOFMIBVIEW, vb

    # wrong community -> dropped
    assert (
        handle_message(build(T_GET, [".1.3.6.1.2.1.1.1.0"], community=b"nope"), table, "public")
        is None
    )

    print("snmpserver self-test: OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: snmpserver.py --selftest")
