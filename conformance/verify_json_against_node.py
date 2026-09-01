#!/usr/bin/env python3
"""Compare the JSON-rendering corpus to what a running node actually emits.

json-v1.json carries `not_yet_verified_against: a running node`. This closes the
HTTP half of that gap: for every case it can express, it asks a node to produce
the value and compares the node's rendering to the corpus, structurally.

The corpus is written from the specification alone (LR-PROTO-004), so a
disagreement is a finding either way round: the document may be wrong about the
engine, or the engine may be wrong about the document. This script never decides
which; it reports the pair.

WHY A CASE CAN BE UNREACHABLE, AND WHY THAT IS AN OUTPUT RATHER THAN A SKIP
--------------------------------------------------------------------------
A case is verified by writing its value as TessariQL and asking the node to
render it back. Some values have no way to be written:

  * `inf`, `-inf` and `NaN` have no float literal — §3 of the language reference
    lists `1.5` and `1e10` and nothing else.
  * Empty `bytes` has no literal: `0x` is refused as "an odd number of digits".
  * `regex` is a value type with NO literal form in §3 at all.
  * An unresolvable table (`<table 99>`, `<record 99:1>`) is unreachable BY
    CONSTRUCTION — a dropped table cannot be named at parse time, which is the
    same wall W31 hit by hand.

An unreachable case is reported with its reason and counted separately. It is
NOT a pass and NOT a failure: a run that silently skipped them would report the
same "all verified" as a run that checked everything, which is the one output
shape this project treats as a defect.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path


class Unreachable(Exception):
    """This value cannot be written as TessariQL source."""


# --- writing a corpus value as TessariQL --------------------------------------


def quote_text(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def float_from_bits(hex_bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hex_bits))[0]


def float_literal(hex_bits: str) -> str:
    f = float_from_bits(hex_bits)
    if f != f:
        raise Unreachable("NaN has no float literal (language reference §3)")
    if f in (float("inf"), float("-inf")):
        raise Unreachable("infinity has no float literal (language reference §3)")
    # repr round-trips a finite double exactly in CPython; force a fraction so
    # the lexer cannot read the literal back as an integer.
    text = repr(f)
    return text if ("." in text or "e" in text or "E" in text) else text + ".0"


def literal(value: dict) -> str:
    """Render one corpus value-model object as TessariQL source."""
    if len(value) != 1:
        raise Unreachable(f"value object carries {len(value)} keys, expected 1")
    kind, body = next(iter(value.items()))

    if kind == "none":
        return "NONE"
    if kind == "null":
        return "NULL"
    if kind == "bool":
        return "true" if body else "false"
    if kind == "integer":
        return str(body)
    if kind == "float_bits":
        return float_literal(body)
    if kind == "decimal":
        mantissa, scale = int(body["mantissa"]), int(body["scale"])
        if scale == 0:
            return f"dec {mantissa}"
        sign = "-" if mantissa < 0 else ""
        digits = str(abs(mantissa)).rjust(scale + 1, "0")
        return f"dec {sign}{digits[:-scale]}.{digits[-scale:]}"
    if kind == "string":
        return quote_text(body)
    if kind == "bytes":
        if body == "":
            raise Unreachable("empty bytes has no literal — `0x` is refused as an odd number of digits")
        return "0x" + body
    if kind == "duration":
        # Built from the numbers, never from the corpus's own expected string:
        # feeding the expected rendering back in would test the round trip and
        # call it a check of the rendering.
        total_ns = int(body["seconds"]) * 1_000_000_000 + int(body["nanos"])
        return f"{total_ns}ns"
    if kind == "datetime":
        seconds, nanos = int(body["seconds"]), int(body["nanos"])
        moment = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
        text = moment.strftime("%Y-%m-%dT%H:%M:%S")
        if nanos:
            text += f".{nanos:09d}".rstrip("0")
        return f"datetime '{text}Z'"
    if kind == "uuid":
        h = body
        hyphenated = f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        return f"uuid '{hyphenated}'"
    if kind == "array":
        return "[" + ", ".join(literal(v) for v in body) + "]"
    if kind == "set":
        return "set [" + ", ".join(literal(v) for v in body) + "]"
    if kind == "object":
        return "{ " + ", ".join(f"{k}: {literal(v)}" for k, v in body.items()) + " }"
    if kind == "range":
        return range_literal(body)
    if kind == "geometry":
        return "geometry " + geojson_literal(body)
    if kind == "regex":
        raise Unreachable("regex is a value type with no literal form in language reference §3")
    if kind == "table":
        return table_name(body)
    if kind == "record":
        return f"{table_name(body['table'])}:{record_id_literal(body['id'])}"
    raise Unreachable(f"no literal rule for value kind `{kind}`")


# Filled from the corpus's own `names` block before a run. A table the corpus
# leaves out of it is UNRESOLVABLE ON PURPOSE — the `<table 99>` and
# `<record 99:1>` cases exist to pin how an unresolvable reference is written,
# and there is no way to ask a node for one: a name that resolves is not
# unresolvable, and a dropped table cannot be written at all, which is the same
# wall W31 hit by hand.
NAMES: dict[str, str] = {}


def table_name(table_id) -> str:
    name = NAMES.get(str(table_id))
    if name is None:
        raise Unreachable(
            f"table {table_id} is deliberately absent from the corpus's `names` block, so it is "
            "unresolvable by construction — a node can only be asked for references that resolve"
        )
    return name


def record_id_literal(rid: dict) -> str:
    kind, body = next(iter(rid.items()))
    if kind == "int":
        return str(body)
    if kind == "text":
        return quote_text(body)
    if kind == "bytes":
        return "0x" + body
    if kind == "uuid":
        h = body
        return f"uuid '{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}'"
    raise Unreachable(f"no literal rule for record id kind `{kind}`")


def range_literal(body: dict) -> str:
    start, end = body["start"], body["end"]
    if "unbounded" in start and "unbounded" in end:
        raise Unreachable("a wholly unbounded range has no literal — `..` is not in language reference §3")
    if "unbounded" in start or "unbounded" in end:
        raise Unreachable("a half-unbounded range has no literal in language reference §3")
    lo = literal(next(iter(start.values())))
    hi_bound, hi_value = next(iter(end.items()))
    hi = literal(hi_value)
    return f"{lo}..={hi}" if hi_bound == "included" else f"{lo}..{hi}"


def geojson_literal(body: dict) -> str:
    """Write a geometry as the object form the language accepts."""
    shape, payload = next(iter(body.items()))

    def point(p: dict) -> str:
        lon, lat = float_from_bits(p["lon"]), float_from_bits(p["lat"])
        for c in (lon, lat):
            if c != c or c in (float("inf"), float("-inf")):
                raise Unreachable("a non-finite coordinate has no float literal to write it with")
        return f"[{lon!r}, {lat!r}]"

    if shape == "point":
        return "{ type: 'Point', coordinates: " + point(payload) + " }"
    if shape == "line":
        return "{ type: 'LineString', coordinates: [" + ", ".join(point(p) for p in payload) + "] }"
    if shape == "polygon":
        rings = ", ".join("[" + ", ".join(point(p) for p in ring) + "]" for ring in payload)
        return "{ type: 'Polygon', coordinates: [" + rings + "] }"
    if shape == "collection":
        members = ", ".join(geojson_literal(g) for g in payload)
        return "{ type: 'GeometryCollection', geometries: [" + members + "] }"
    raise Unreachable(f"no literal rule for geometry shape `{shape}`")


# --- talking to the node ------------------------------------------------------


def run_script(node: str, script: str) -> tuple[int, object]:
    request = urllib.request.Request(
        f"http://{node}/script", data=script.encode(), headers={"Content-Type": "text/plain"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, json.loads(answer.read().decode())
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read().decode())


# --- the run ------------------------------------------------------------------


PRELUDE = "USE NAMESPACE conformance; USE DATABASE conformance; "


def prepare(node: str, corpus: dict) -> None:
    """Declare the namespace, the database and every table the corpus names.

    A record or table literal does not parse without a selected namespace, and
    `USE` does NOT survive across requests — each POST is its own session — so
    every later script carries the prelude rather than relying on this call.
    """
    setup = ["DEFINE NAMESPACE conformance", "USE NAMESPACE conformance", "DEFINE DATABASE conformance",
             "USE DATABASE conformance"]
    setup += [f"DEFINE COLLECTION {name}" for name in NAMES.values()]
    status, answer = run_script(node, "; ".join(setup) + ";")
    if status != 200:
        raise SystemExit(f"setup failed on a fresh node ({status}): {answer}")


def verify(corpus: dict, node: str) -> list[dict]:
    rows: list[dict] = []
    for case in corpus["cases"]:
        name, expected_absent = case["name"], case.get("omitted", False)
        try:
            source = literal(case["value"])
        except Unreachable as why:
            rows.append({"case": name, "verdict": "unreachable", "why": str(why)})
            continue

        # An omitted value is not a value the node can return on its own: its
        # whole claim is that the KEY is absent. Put it in an object, where
        # absence is observable.
        body = f"RETURN {{ f: {source} }};" if expected_absent else f"RETURN {source};"
        script = PRELUDE + body
        expected = {} if expected_absent else case.get("json")

        status, answer = run_script(node, script)
        if status != 200:
            rows.append(
                {"case": name, "verdict": "refused", "why": answer.get("error", answer), "sent": script}
            )
            continue
        # The prelude contributes its own `done` outcomes; the value is the last.
        results = answer.get("results") or []
        if not results or results[-1].get("kind") != "value":
            rows.append({"case": name, "verdict": "shape", "why": answer, "sent": script})
            continue

        got = results[-1]["value"]
        if expected_absent:
            got_compare, expected_compare = got, {}
        else:
            got_compare, expected_compare = got, expected

        verdict = "verified" if got_compare == expected_compare else "MISMATCH"
        row = {"case": name, "verdict": verdict, "sent": script}
        if verdict == "MISMATCH":
            row["corpus"] = expected_compare
            row["node"] = got_compare
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", default="127.0.0.1:47901", help="host:port of a running node")
    parser.add_argument(
        "--corpus", default=str(Path(__file__).with_name("json-v1.json")), help="path to json-v1.json"
    )
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    NAMES.update(corpus.get("names") or {})
    prepare(args.node, corpus)
    rows = verify(corpus, args.node)

    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        for row in rows:
            mark = {"verified": "  ok", "MISMATCH": "FAIL", "unreachable": "  --"}.get(row["verdict"], "  ??")
            print(f"{mark}  {row['case']:44} {row['verdict']}")
            if row["verdict"] == "MISMATCH":
                print(f"        sent   {row['sent']}")
                print(f"        corpus {json.dumps(row['corpus'])}")
                print(f"        node   {json.dumps(row['node'])}")
            elif row["verdict"] in ("unreachable", "refused", "shape"):
                print(f"        {row['why']}")

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    total = len(rows)
    print(
        f"\nunits: source={total} | "
        + " | ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f"\nchecked={total - counts.get('unreachable', 0)} of {total}; "
        "an unreachable case is neither a pass nor a failure — see the module docstring"
    )
    # A mismatch is the finding this script exists to produce, so it exits
    # non-zero; an unreachable case is a known, enumerated limit and does not.
    return 1 if counts.get("MISMATCH") or counts.get("refused") or counts.get("shape") else 0


if __name__ == "__main__":
    sys.exit(main())
