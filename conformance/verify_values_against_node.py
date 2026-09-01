#!/usr/bin/env python3
"""Compare the byte corpus to what a running node encodes and accepts.

`values-v1.json` carries `not_yet_verified_against: a running node`, and it is
the corpus where that gap matters most, because its own header states the trap:

    Decoding alone does not check a codec. A codec that is wrong in the same way
    on both sides round-trips perfectly.

That sentence is written about a client. This script applies it to the node.

WHY THIS SPEAKS THE WIRE PROTOCOL AND NOT HTTP
----------------------------------------------
The §4 value codec never appears on the HTTP surface: §5.5 says a `/script`
parameter carries TessariQL **source**, not an encoded value. So the bytes this
corpus is about are only observable over §3, and this is a second program rather
than a flag on `verify_json_against_node.py`.

TWO CHECKS PER CASE, AND WHY ONE WOULD NOT DO
---------------------------------------------
  * `encode`     — send `RETURN <literal>;` as source and compare the answer's
                   value payload to the corpus bytes. The expectation comes from
                   `generate.py`, a second implementation written from the
                   document alone, so this puts the node's ENCODER on trial
                   against something that never saw the node.
  * `roundtrip`  — send the corpus bytes as a bound parameter and compare what
                   comes back. This exercises the decoder too, but it is the
                   WEAK check: a decoder and an encoder wrong in the same way
                   agree with each other, which is precisely what the corpus
                   header warns about. It is kept because it reaches cases
                   `encode` cannot — a value with no literal still has bytes.

A case with no TessariQL literal is `unreachable` for `encode`, with its reason,
never skipped: a run that passes over a case must not print what a run that
checked it prints.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from pathlib import Path

from verify_json_against_node import NAMES, Unreachable, float_from_bits, literal

MAGIC = b"TESS"
MAJOR, MINOR = 1, 0

FRAME_REQUEST = 1
FRAME_ANSWER = 2
FRAME_REFUSAL = 3

OUTCOME_DONE = 0
OUTCOME_RECORDS = 1
OUTCOME_VALUE = 2
OUTCOME_KEYS = 3
OUTCOME_REMOVED = 4

CEILING = 16 * 1024 * 1024


class Refused(Exception):
    """The store said no, in its own words (§3.6)."""


class Malformed(Exception):
    """A body is not the shape its header claims (§3.11)."""


# --- §2.1 frame-layer primitives ----------------------------------------------


def u32(n: int) -> bytes:
    return struct.pack(">I", n)


def text(s: str) -> bytes:
    raw = s.encode("utf-8")
    return u32(len(raw)) + raw


class Reader:
    """A cursor over one body. Every read is bounded by the body's own length."""

    def __init__(self, raw: bytes) -> None:
        self.raw, self.at = raw, 0

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.raw):
            raise Malformed(f"wanted {n} bytes at offset {self.at}, body holds {len(self.raw)}")
        chunk = self.raw[self.at : self.at + n]
        self.at += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]

    def text(self) -> str:
        return self.take(self.u32()).decode("utf-8")

    def lenbytes(self) -> bytes:
        return self.take(self.u32())


# --- §3 the connection --------------------------------------------------------


class Node:
    """One connection, which is one session (§3.10)."""

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.sendall(MAGIC + bytes([MAJOR, MINOR]))
        greeting = self.exactly(6)
        if greeting[:4] != MAGIC:
            raise SystemExit(f"not this protocol: peer opened with {greeting[:4]!r}, not {MAGIC!r}")
        self.major, self.minor = greeting[4], greeting[5]
        if self.major != MAJOR:
            raise SystemExit(f"wrong version: node speaks major {self.major}, this speaks {MAJOR}")

    def exactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise Malformed(f"stream ended after {len(out)} of {n} bytes")
            out += chunk
        return out

    def send_frame(self, kind: int, body: bytes) -> None:
        if len(body) > CEILING:
            raise Malformed(f"body of {len(body)} bytes exceeds the 16 MiB ceiling")
        self.sock.sendall(bytes([kind]) + u32(len(body)) + body)

    def read_frame(self) -> tuple[int, bytes]:
        header = self.exactly(5)
        kind, length = header[0], struct.unpack(">I", header[1:])[0]
        if length > CEILING:
            raise Malformed(f"declared length {length} exceeds the 16 MiB ceiling")
        return kind, self.exactly(length)

    def request(self, script: str, params: list[tuple[str, bytes]] | None = None) -> list[dict]:
        """§3.4 out, §3.5 back. Returns one dict per outcome, in order."""
        params = params or []
        body = text(script) + b"\x00" + u32(len(params))
        for name, encoded in params:
            body += text(name) + u32(len(encoded)) + encoded
        self.send_frame(FRAME_REQUEST, body)

        kind, answer = self.read_frame()
        if kind == FRAME_REFUSAL:
            # §3.6: the body is the store's own message, whole, unprefixed.
            raise Refused(answer.decode("utf-8", "replace"))
        if kind != FRAME_ANSWER:
            raise Malformed(f"expected an Answer frame, got kind {kind}")
        return self.read_outcomes(Reader(answer))

    @staticmethod
    def read_outcomes(body: Reader) -> list[dict]:
        outcomes = []
        for _ in range(body.u32()):
            length = body.u32()
            inner = Reader(body.take(length))  # the length is a bound, not just a cursor
            outcomes.append(Node.read_outcome(inner))
        return outcomes

    @staticmethod
    def read_outcome(inner: Reader) -> dict:
        tag = inner.u8()
        if tag == OUTCOME_DONE:
            return {"kind": "done"}
        if tag == OUTCOME_VALUE:
            names = Node.read_names(inner)
            return {"kind": "value", "names": names, "bytes": inner.lenbytes()}
        if tag == OUTCOME_KEYS:
            return {"kind": "keys", "keys": [inner.text() for _ in range(inner.u32())]}
        if tag == OUTCOME_REMOVED:
            return {"kind": "removed", "count": inner.u64()}
        if tag == OUTCOME_RECORDS:
            inner.u8()  # access path
            names = Node.read_names(inner)
            records = [(inner.text(), inner.lenbytes()) for _ in range(inner.u32())]
            return {"kind": "records", "names": names, "records": records}
        # §3.5: an unrecognised tag is reported, its bytes stepped over, and the
        # read carries on. That is the whole point of the per-outcome length.
        return {"kind": "unknown", "tag": tag}

    @staticmethod
    def read_names(inner: Reader) -> dict[int, str]:
        return {inner.u32(): inner.text() for _ in range(inner.u32())}

    def close(self) -> None:
        self.sock.close()


# --- writing a value of THIS corpus as TessariQL ------------------------------
#
# Everything but geometry shares the value model of `json-v1.json`, so `literal`
# is imported rather than copied. Geometry does NOT: this corpus writes a polygon
# as `{exterior, interiors}` and carries `multipoint`, `multiline` and
# `multipolygon` as their own shapes, where the JSON corpus writes rings as a
# bare list and never names the multi- shapes. Two models, so two writers.


def point_literal(p: dict) -> str:
    lon, lat = float_from_bits(p["lon"]), float_from_bits(p["lat"])
    for c in (lon, lat):
        if c != c or c in (float("inf"), float("-inf")):
            raise Unreachable("a non-finite coordinate has no float literal to write it with")
    return f"[{lon!r}, {lat!r}]"


def ring(points: list) -> str:
    return "[" + ", ".join(point_literal(p) for p in points) + "]"


def polygon_rings(body: dict) -> str:
    return "[" + ", ".join(ring(r) for r in [body["exterior"], *body["interiors"]]) + "]"


def geometry_literal(body: dict) -> str:
    shape, payload = next(iter(body.items()))
    if shape == "point":
        return "{ type: 'Point', coordinates: " + point_literal(payload) + " }"
    if shape == "line":
        return "{ type: 'LineString', coordinates: " + ring(payload) + " }"
    if shape == "polygon":
        return "{ type: 'Polygon', coordinates: " + polygon_rings(payload) + " }"
    if shape == "multipoint":
        return "{ type: 'MultiPoint', coordinates: " + ring(payload) + " }"
    if shape == "multiline":
        return "{ type: 'MultiLineString', coordinates: [" + ", ".join(ring(r) for r in payload) + "] }"
    if shape == "multipolygon":
        members = ", ".join(polygon_rings(p) for p in payload)
        return "{ type: 'MultiPolygon', coordinates: [" + members + "] }"
    if shape == "collection":
        members = ", ".join(geometry_literal(g) for g in payload)
        return "{ type: 'GeometryCollection', geometries: [" + members + "] }"
    raise Unreachable(f"no literal rule for geometry shape `{shape}`")


def source_for(value: dict) -> str:
    if len(value) == 1 and "geometry" in value:
        return "geometry " + geometry_literal(value["geometry"])
    return literal(value)


# --- the run ------------------------------------------------------------------


def one_value(outcomes: list[dict]) -> bytes:
    """The value payload of the last outcome, or a shape complaint."""
    if not outcomes or outcomes[-1]["kind"] != "value":
        raise Malformed(f"expected a Value outcome last, got {[o['kind'] for o in outcomes]}")
    return outcomes[-1]["bytes"]


def check(node: Node, script: str, params, expected: bytes) -> dict:
    try:
        got = one_value(node.request(script, params))
    except Refused as why:
        return {"verdict": "refused", "why": str(why)}
    except Malformed as why:
        return {"verdict": "shape", "why": str(why)}
    if got == expected:
        return {"verdict": "ok"}
    return {"verdict": "MISMATCH", "corpus": expected.hex(), "node": got.hex()}


def prepare(node: Node) -> None:
    """Select a namespace and a database, once.

    One connection is one session (§3.10), so unlike the HTTP harness this is a
    single call rather than a prelude on every script. Each `DEFINE` tolerates
    its own refusal so the harness may be re-run against a node that is still
    up — a `--serve` process outlives one run of this file.
    """
    for statement in (
        "DEFINE NAMESPACE conformance;",
        "USE NAMESPACE conformance;",
        "DEFINE DATABASE conformance;",
        "USE DATABASE conformance;",
    ):
        try:
            node.request(statement)
        except Refused as why:
            if "already in use" not in str(why):
                raise


def verify(corpus: dict, node: Node) -> list[dict]:
    rows = []
    for case in corpus["cases"]:
        expected = bytes.fromhex(case["bytes"])

        try:
            source = source_for(case["value"])
        except Unreachable as why:
            encode = {"verdict": "unreachable", "why": str(why)}
        else:
            encode = check(node, f"RETURN {source};", [], expected)

        roundtrip = check(node, "RETURN $p0;", [("p0", expected)], expected)
        rows.append({"case": case["name"], "encode": encode, "roundtrip": roundtrip})
    return rows


MARKS = {"ok": "  ok", "MISMATCH": "FAIL", "unreachable": "  --", "refused": " ref", "shape": " shp"}


def report(rows: list[dict]) -> int:
    for row in rows:
        e, r = row["encode"], row["roundtrip"]
        print(f"{MARKS.get(e['verdict'], '  ??')} {MARKS.get(r['verdict'], '  ??')}  {row['case']}")
        for label, result in (("encode", e), ("roundtrip", r)):
            if result["verdict"] == "MISMATCH":
                print(f"        {label} corpus {result['corpus']}")
                print(f"        {label} node   {result['node']}")
            elif result["verdict"] in ("unreachable", "refused", "shape"):
                print(f"        {label}: {result['why']}")

    counts: dict[str, int] = {}
    for row in rows:
        for label in ("encode", "roundtrip"):
            key = f"{label}-{row[label]['verdict']}"
            counts[key] = counts.get(key, 0) + 1

    total = len(rows)
    print(f"\nunits: source={total} | " + " | ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(
        "encode is the check that cannot be satisfied by a decoder and an encoder\n"
        "being wrong in the same way; roundtrip alone can be, which is what the\n"
        "corpus header warns about. An unreachable case is neither a pass nor a\n"
        "failure — see the module docstring."
    )
    bad = sum(v for k, v in counts.items() if k.split("-", 1)[1] in ("MISMATCH", "refused", "shape"))
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", default="127.0.0.1:47901", help="host:port of a node serving --serve")
    parser.add_argument(
        "--corpus", default=str(Path(__file__).with_name("values-v1.json")), help="path to values-v1.json"
    )
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    NAMES.update(corpus.get("names") or {})

    host, _, port = args.node.rpartition(":")
    node = Node(host, int(port))
    try:
        prepare(node)
        rows = verify(corpus, node)
    finally:
        node.close()

    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    return report(rows)


if __name__ == "__main__":
    sys.exit(main())
