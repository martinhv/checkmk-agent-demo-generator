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

import re
import socket
import threading
from typing import Callable

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
T_NOSUCHOBJECT = 0x80    # context [0] — implicit NULL
T_NOSUCHINSTANCE = 0x81  # context [1]
T_ENDOFMIBVIEW = 0x82    # context [2]

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
            if body[-1] & 0x80:        # keep it positive
                body.append(0x00)
        else:
            n = -value
            bits = n.bit_length() + 1
            nbytes = (bits + 7) // 8
            val = (1 << (8 * nbytes)) + value    # two's complement
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
        length = int.from_bytes(buf[pos:pos + nbytes], "big")
        pos += nbytes
    if pos + length > len(buf):
        raise BERError("truncated body")
    return tag, buf[pos:pos + length], pos + length


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
class VarBind:
    __slots__ = ("oid", "value_tlv")

    def __init__(self, oid: tuple[int, ...], value_tlv: bytes) -> None:
        self.oid = oid
        self.value_tlv = value_tlv           # already-encoded value (TLV)

    def encode(self) -> bytes:
        return _tlv(T_SEQ, enc_oid(self.oid) + self.value_tlv)


def _parse_varbinds(body: bytes) -> list[tuple[int, ...]]:
    """Return the list of requested OIDs (values are NULL in a request)."""
    oids, pos = [], 0
    while pos < len(body):
        tag, vb, pos = _read_tlv(body, pos)
        if tag != T_SEQ:
            raise BERError("varbind not a sequence")
        otag, obody, p = _read_tlv(vb, 0)
        if otag != T_OID:
            raise BERError("varbind name not an OID")
        oids.append(bytes_to_oid(obody))
    return oids


class Table:
    """A device's current OID space: sorted (oid_tuple, value_tlv) pairs, with
    fast lexicographic 'next' lookup for GETNEXT/GETBULK."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        pairs = {}
        for oid_s, val_s in rows:
            pairs[parse_oid(oid_s)] = enc_octetstr(encode_value(val_s))
        self.oids = sorted(pairs)
        self.value = pairs

    def get(self, oid: tuple[int, ...]) -> bytes | None:
        return self.value.get(oid)

    def next(self, oid: tuple[int, ...]) -> tuple[int, ...] | None:
        # first stored OID strictly greater than `oid` (lexicographic)
        import bisect
        i = bisect.bisect_right(self.oids, oid)
        return self.oids[i] if i < len(self.oids) else None


def handle_message(data: bytes, table: Table,
                   community: str | None = None) -> bytes | None:
    """Parse a v2c request and return the encoded response (or None to drop)."""
    tag, body, _ = _read_tlv(data, 0)
    if tag != T_SEQ:
        return None
    pos = 0
    vtag, vbody, pos = _read_tlv(body, pos)      # version
    if vtag != T_INT or dec_int(vbody) != 1:     # 1 == v2c
        return None
    ctag, cbody, pos = _read_tlv(body, pos)      # community
    if ctag != T_OCTETSTR:
        return None
    if community is not None and cbody.decode("latin1") != community:
        return None
    ptag, pbody, _ = _read_tlv(body, pos)        # PDU
    if ptag not in (T_GET, T_GETNEXT, T_GETBULK):
        return None

    p = 0
    rtag, rbody, p = _read_tlv(pbody, p)         # request-id
    request_id = dec_int(rbody)
    n1tag, n1body, p = _read_tlv(pbody, p)       # error-status / non-repeaters
    n2tag, n2body, p = _read_tlv(pbody, p)       # error-index / max-repetitions
    vbtag, vbbody, _ = _read_tlv(pbody, p)       # varbind list
    oids = _parse_varbinds(vbbody)

    if ptag == T_GET:
        out = [VarBind(o, table.get(o) or _tlv(T_NOSUCHINSTANCE, b"")) for o in oids]
    elif ptag == T_GETNEXT:
        out = [_next_vb(table, o) for o in oids]
    else:                                        # GETBULK
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

    resp_pdu = (enc_int(request_id) + enc_int(0) + enc_int(0)
                + _tlv(T_SEQ, b"".join(vb.encode() for vb in out)))
    return _tlv(T_SEQ, enc_int(1) + enc_octetstr(cbody) + _tlv(T_RESPONSE, resp_pdu))


def _next_vb(table: Table, oid: tuple[int, ...]) -> VarBind:
    nxt = table.next(oid)
    if nxt is None:
        return VarBind(oid, _tlv(T_ENDOFMIBVIEW, b""))
    return VarBind(nxt, table.get(nxt))


# --------------------------------------------------------------------------- #
#  UDP server — one socket per device IP, a single selectors loop
# --------------------------------------------------------------------------- #
class SnmpServer:
    """Binds `ip:port` per device; `table_for(short)` returns a current
    `Table` for that device (the caller builds + caches it — a device's OID
    space is stable within a poll, so it need not be rebuilt per packet)."""

    def __init__(self, port: int, community: str | None,
                 table_for: "Callable[[str], Table]") -> None:
        self.port = port
        self.community = community
        self.table_for = table_for
        self._socks: list[tuple[socket.socket, str]] = []

    def bind(self, short: str, ip: str) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((ip, self.port))
        self._socks.append((s, short))

    def serve_forever(self) -> None:
        # One thread per device socket. A Checkmk bulk discovery walks many
        # devices at once; a single loop would serialize their (large) walks
        # and the SNMP fetches would time out. Per-socket threads are cheap
        # (idle on a blocking recvfrom) and let concurrent walks interleave.
        threads = [threading.Thread(target=self._serve, args=(s, short),
                                    daemon=True)
                   for s, short in self._socks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _serve(self, sock: socket.socket, short: str) -> None:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except OSError:
                return
            try:
                reply = handle_message(data, self.table_for(short), self.community)
            except Exception:
                continue
            if reply is not None:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass


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
        "3029" "020101" "0406" "7075626c6963"
        "a01c" "02040000002a" "020100" "020100"
        "300e" "300c" "0608" "2b06010201010100" "0500")
    tag, body, _ = _read_tlv(pkt, 0)
    assert tag == T_SEQ
    _, _, p = _read_tlv(body, 0)
    _, comm, p = _read_tlv(body, p)
    assert comm == b"public"
    ptag, pbody, _ = _read_tlv(body, p)
    assert ptag == T_GET
    _, rid, pp = _read_tlv(pbody, 0)
    assert dec_int(rid) == 0x2a
    _, _, pp = _read_tlv(pbody, pp)
    _, _, pp = _read_tlv(pbody, pp)
    _, vbs, _ = _read_tlv(pbody, pp)
    assert _parse_varbinds(vbs) == [parse_oid("1.3.6.1.2.1.1.1.0")]

    # 3. GET / GETNEXT / GETBULK against a small table
    rows = [(".1.3.6.1.2.1.1.1.0", "Test Device"),
            (".1.3.6.1.2.1.1.3.0", "12345"),
            (".1.3.6.1.2.1.2.2.1.6.1", '"B2 E0 7D 2C 4D 15 "'),
            (".1.3.6.1.2.1.2.2.1.10.1", "9998887776")]
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
            ot, ob, r = _read_tlv(vb, 0)
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
    assert handle_message(build(T_GET, [".1.3.6.1.2.1.1.1.0"], community=b"nope"),
                          table, "public") is None

    print("snmpserver self-test: OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: snmpserver.py --selftest")
