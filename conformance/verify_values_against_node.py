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
import sys
from pathlib import Path

from verify_json_against_node import NAMES, Unreachable, float_from_bits, literal
from wire import Malformed, Node, Refused, prepare

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
