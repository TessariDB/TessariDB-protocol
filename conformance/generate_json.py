#!/usr/bin/env python3
"""Generate the JSON-rendering conformance corpus for protocol version 1.

This is a **second implementation** of sections 5.6 and 5.7 of
`spec/protocol-v1.md`, written from that document alone. It renders a value in
the same tagged notation the other two corpora use into the JSON the HTTP
surface writes for it, and renders each outcome kind into its object.

**This corpus is decode-only, and that is a property rather than an oversight.**
`values-v1.json` requires both directions and says at length why one is not
enough. Here there is only one: a client never encodes a JSON value on this
surface, because a `POST /script` parameter carries **TessariQL source** rather
than JSON (§5.5). Nothing a client sends is a value from the §5.7 table, so
there is no encode direction to check.

Where the document does not say enough to write a case, this generator raises
`Silence` and the case is omitted and listed in the corpus's own `gaps` field.
That is the corpus doing its job: LR-PROTO-004 asks it to disagree when the
document is unclear, and a gap recorded in the artifact is that disagreement in
its cheapest form. Filling one in from the server's behaviour would destroy the
only property this file has.

Deliberately no dependency on anything but the standard library, and no engine or
client source was opened while it was written.

Usage:
    python3 generate_json.py > json-v1.json     regenerate the corpus
    python3 generate_json.py --check            fail if the committed corpus differs
"""

import argparse
import json
import pathlib
import struct
import sys

CORPUS = pathlib.Path(__file__).with_name("json-v1.json")


class Silence(Exception):
    """The document does not specify this rendering.

    Carries the question rather than a guess. Every one raised here is recorded
    in `02_questions.md` of the governance scope as well as in the corpus.
    """


# A value whose JSON encoding is the *absence* of its key (§5.7, `none`).
OMITTED = object()

# --- §5.7, row by row ---------------------------------------------------------


def spell_decimal(mantissa, scale):
    """`decimal` → a **string**, so a parser cannot turn money into a double."""
    digits = str(abs(int(mantissa)))
    sign = "-" if int(mantissa) < 0 else ""
    if scale == 0:
        return sign + digits
    if len(digits) <= scale:
        digits = digits.rjust(scale + 1, "0")
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"


def spell_float(bits):
    """`float` → a number, except non-finite, which is **quoted** (§5.7)."""
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    if value != value:
        return "NaN"
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    # §5.7.1: there is no negative zero to write. It is not a rendering choice —
    # a float is normalised when the value is built, so this and `0.0` are one
    # value.
    if value == 0.0:
        return 0
    # §5.7.1: a float with no fractional part writes as an integer, which is
    # what makes the row lossy. Exponent form is a separate question and does
    # not arise here: this file is compared as JSON VALUES, and `1e-05` and
    # `0.00001` are one value.
    if value.is_integer():
        return int(value)
    return value


def spell_duration(seconds, nanos):
    """`duration` → the decomposition §5.7.1 states.

    Six units, largest first, zero counts omitted, `0s` for an empty span, and a
    leading `-` before the MAGNITUDE for a span below zero.

    The hour is the largest unit: `d` and `w` are readable on input and are never
    written, so a week is `168h`. An earlier draft of this file decomposed into
    weeks and days, which no case happened to reach.

    `seconds` and `nanos` are a floor pair — the seconds may be negative while
    the remainder is a positive addend — so the magnitude is taken over the total
    rather than field by field. Doing it field by field spells a span one second
    longer than itself.
    """
    total = int(seconds) * 1_000_000_000 + int(nanos)
    out = "-" if total < 0 else ""
    remaining = abs(total)
    if remaining == 0:
        return "0s"
    for unit, size in (
        ("h", 3_600_000_000_000),
        ("m", 60_000_000_000),
        ("s", 1_000_000_000),
        ("ms", 1_000_000),
        ("us", 1_000),
        ("ns", 1),
    ):
        count, remaining = divmod(remaining, size)
        if count:
            out += f"{count}{unit}"
    return out


def spell_datetime(seconds, nanos):
    """`datetime` → RFC 3339 (§5.7).

    Rendered in UTC with a `Z` offset. RFC 3339 permits `+00:00` for the same
    instant and permits any sub-second precision, and §5.7 chooses neither — so
    a sub-second value is a silence rather than a rounding decision.
    """
    import datetime as dt

    moment = dt.datetime.fromtimestamp(int(seconds), dt.timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    # §5.7.1: up to nine digits, trailing zeros trimmed, and no `.` at all when
    # there is no remainder. Always `Z`: the instant carries no zone, so an
    # offset in the input was applied and discarded before it got here.
    if int(nanos):
        text += "." + f"{int(nanos):09d}".rstrip("0")
    return text + "Z"


def spell_uuid(hexadecimal):
    """`uuid` → hyphenated lowercase hex (§5.7)."""
    h = hexadecimal.lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def spell_table(table_id, names):
    """`table` → the name, or `"<table 7>"` when the answer cannot resolve it."""
    if table_id in names:
        return names[table_id]
    return f"<table {table_id}>"


def spell_record_id(identity):
    """The `id` half of `"table:id"`.

    §5.7.1: the id's own text, with NO quoting and NO escaping — this half is
    written in the identity syntax rather than the value syntax, which is why a
    uuid loses its hyphens and a bytes id keeps its `0x` while the standalone
    rows for those types do the opposite.
    """
    kind, payload = next(iter(identity.items()))
    if kind == "int":
        return str(int(payload))
    if kind == "text":
        return payload
    if kind == "uuid":
        return payload.lower().replace("-", "")
    if kind == "bytes":
        return "0x" + payload.lower()
    raise Silence(f"§5.7.1 does not spell a {kind} record id inside \"table:id\"")


def spell_record(record, names):
    """`record` → `"table:id"`; unresolvable is `"<record …>"` (§5.7)."""
    identity = spell_record_id(record["id"])
    table_id = record["table"]
    if table_id not in names:
        # §5.7.1 resolves the ellipsis: the table's NUMERIC id, so the whole
        # thing reads `<record 7:1>`. The brackets are what make it detectable —
        # `<` cannot begin a table name.
        return f"<record {table_id}:{identity}>"
    return f"{names[table_id]}:{identity}"


def spell_coordinate(bits):
    """A geometry coordinate: a number, or `null` when non-finite (§5.7)."""
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def spell_position(position):
    """RFC 7946: **longitude first**. The silent geospatial bug lives here."""
    return [spell_coordinate(position["lon"]), spell_coordinate(position["lat"])]


def spell_geometry(shape):
    """`geometry` → GeoJSON, RFC 7946 (§5.7)."""
    kind, payload = next(iter(shape.items()))
    if kind == "point":
        return {"type": "Point", "coordinates": spell_position(payload)}
    if kind == "line":
        return {"type": "LineString", "coordinates": [spell_position(p) for p in payload]}
    if kind == "polygon":
        return {
            "type": "Polygon",
            "coordinates": [[spell_position(p) for p in ring] for ring in payload],
        }
    if kind == "multipoint":
        return {"type": "MultiPoint", "coordinates": [spell_position(p) for p in payload]}
    if kind == "multiline":
        return {
            "type": "MultiLineString",
            "coordinates": [[spell_position(p) for p in line] for line in payload],
        }
    if kind == "multipolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [[spell_position(p) for p in ring] for ring in polygon]
                for polygon in payload
            ],
        }
    if kind == "collection":
        return {
            "type": "GeometryCollection",
            "geometries": [spell_geometry(member) for member in payload],
        }
    raise Silence(f"no GeoJSON mapping stated for geometry kind {kind}")


def spell_bound(bound):
    """A range endpoint: `bound`, plus `value` unless unbounded (§5.7)."""
    kind, payload = next(iter(bound.items()))
    if kind == "unbounded":
        # An unbounded end carries no `value` key at all — the same rule `none`
        # uses, and what keeps an open end distinct from an end holding null.
        return {"bound": "unbounded"}
    return {"bound": kind, "value": spell(payload, {})}


def spell(value, names):
    """§5.7's table, as one function. Returns OMITTED for `none`."""
    kind, payload = next(iter(value.items()))

    if kind == "none":
        return OMITTED
    if kind == "null":
        return None
    if kind == "bool":
        return payload
    if kind == "integer":
        return int(payload)
    if kind == "decimal":
        return spell_decimal(payload["mantissa"], payload["scale"])
    if kind == "float_bits":
        return spell_float(payload)
    if kind == "string":
        return payload
    if kind == "bytes":
        return payload.lower()
    if kind == "duration":
        return spell_duration(payload["seconds"], payload["nanos"])
    if kind == "datetime":
        return spell_datetime(payload["seconds"], payload["nanos"])
    if kind == "uuid":
        return spell_uuid(payload)
    if kind == "table":
        return spell_table(payload, names)
    if kind == "record":
        return spell_record(payload, names)
    if kind == "array":
        return [element for element in (spell(e, names) for e in payload)]
    if kind == "set":
        # A set arrives as an array and the collection type is not recoverable.
        return [element for element in (spell(e, names) for e in payload)]
    if kind == "object":
        # A field holding `none` is omitted, per 5.6.
        written = {}
        for field, held in payload.items():
            rendered = spell(held, names)
            if rendered is not OMITTED:
                written[field] = rendered
        return written
    if kind == "range":
        return {"start": spell_bound(payload["start"]), "end": spell_bound(payload["end"])}
    if kind == "geometry":
        return spell_geometry(payload)
    if kind == "regex":
        # The pattern's source. The server does not execute it and a client must
        # not compile it and present the result as this store's semantics.
        return payload
    raise Silence(f"§5.7 has no row for value kind {kind}")


# --- §5.6, the outcome objects ------------------------------------------------


def spell_outcome(outcome, names):
    """One outcome object. `kind` is always present; the rest is per kind."""
    kind, payload = next(iter(outcome.items()))

    if kind == "done":
        return {"kind": "done"}
    if kind == "value":
        rendered = spell(payload, names)
        if rendered is OMITTED:
            # `none`: the key is absent, which is NOT the same as a null value.
            return {"kind": "value"}
        return {"kind": "value", "value": rendered}
    if kind == "removed":
        return {"kind": "removed", "count": payload}
    if kind == "unknown":
        return {"kind": "unknown"}
    if kind == "keys":
        # Record identities as strings: an array of strings, never of objects —
        # there is no value beside the identity, which is the point of the
        # outcome.
        #
        # The ID HALF ALONE, with no `table:` prefix. This read `spell(...)`,
        # the reference form, until W55 exercised a running node: `KEYS FROM
        # notes` answers `["42","42"]` and not `["notes:42", …]`, because the
        # statement already named the table. A client written to the old corpus
        # split on a colon that is never there.
        return {
            "kind": "keys",
            "keys": [spell_record_id(key["record"]["id"]) for key in payload],
        }
    if kind == "records":
        plan = payload["plan"]
        if "access" not in plan:
            raise Silence("§5.6 requires `access` in every plan")
        out = {"kind": "records", "path": plan["access"], "plan": dict(plan)}
        # Written only when non-empty / only when true, so a response with
        # nothing to report is identical to what it was before either existed.
        if payload.get("notes"):
            out["notes"] = [
                {"kind": note["kind"], "message": note["message"]}
                for note in payload["notes"]
            ]
        if payload.get("only"):
            out["only"] = True
        # NOT `.get()`, and that is the whole point of the field. Present-and-
        # empty says a dictionary was asked and holds every term; absent says
        # nothing was looked for. Reading this with `.get(...)` would fold the
        # claim into the absence of one, which is the client bug §5.6 warns
        # about, written into the generator that is supposed to catch it.
        if "suggestion" in payload:
            out["suggestion"] = {
                "corrections": [
                    {"typed": pair["typed"], "instead": pair["instead"]}
                    for pair in payload["suggestion"]
                ]
            }
        # An element is a PAIR, not a value: a client that types this array as
        # the records reads one level too high.
        #
        # `id` is the ID HALF ALONE, for the same reason `keys` is: every row of
        # one outcome comes from the table `plan.table` names, and the grammar
        # has no two-table source to make that ambiguous. A record REFERENCE
        # stored in a field keeps the qualified `table:id` form — measured on a
        # running node in W55, where a row read back as
        # `{"id":"9","value":{"by":"users:1"}}`.
        out["records"] = [
            {
                "id": spell_record_id(row["id"]["record"]["id"]),
                "value": spell(row["value"], names),
            }
            for row in payload["rows"]
        ]
        return out
    raise Silence(f"§5.6 has no object for outcome kind {kind}")


# --- the cases ----------------------------------------------------------------

NAMES = {3: "users", 7: "memories"}

# (name, note, value) — the JSON is COMPUTED, never typed in.
VALUE_CASES = [
    ("none-is-omitted", "Absence is the encoding. Not the same as null.", {"none": None}),
    ("null-is-written", "A present field holding nothing.", {"null": None}),
    ("bool-true", None, {"bool": True}),
    ("bool-false", None, {"bool": False}),
    ("integer-small", "Exact in every JSON parser.", {"integer": "42"}),
    (
        "integer-beyond-double",
        "Exact in this document and NOT in a parser that reads every number as a "
        "double. A client whose JSON reader loses this should fail here.",
        {"integer": "9223372036854775807"},
    ),
    ("decimal-is-a-string", "A JSON number is a double; dec exists so money is not.", {"decimal": {"mantissa": "1234", "scale": 2}}),
    ("decimal-negative", None, {"decimal": {"mantissa": "-1234", "scale": 2}}),
    ("decimal-scale-zero", None, {"decimal": {"mantissa": "500", "scale": 0}}),
    ("decimal-leading-zero", "Scale wider than the mantissa.", {"decimal": {"mantissa": "7", "scale": 3}}),
    ("float-one-and-a-half", "Exactly representable, so the number is safe.", {"float_bits": "3ff8000000000000"}),
    ("float-infinity", "Quoted: JSON has no spelling for it.", {"float_bits": "7ff0000000000000"}),
    ("float-negative-infinity", None, {"float_bits": "fff0000000000000"}),
    ("float-nan", "Quoted for the same reason.", {"float_bits": "7ff8000000000000"}),
    ("string-plain", None, {"string": "hello"}),
    ("string-unicode", "A length in bytes, not characters.", {"string": "héllo"}),
    ("string-with-quote-and-backslash", "JSON escaping, not TessariQL's.", {"string": 'a"b\\c'}),
    ("bytes-lowercase-hex", "No prefix, no separators.", {"bytes": "00ff10"}),
    ("bytes-empty", None, {"bytes": ""}),
    ("duration-the-documents-own-example", "1h30m — the one rendering §5.7 shows.", {"duration": {"seconds": "5400", "nanos": 0}}),
    ("duration-seconds-only", None, {"duration": {"seconds": "45", "nanos": 0}}),
    ("datetime-epoch", "RFC 3339, UTC.", {"datetime": {"seconds": "0", "nanos": 0}}),
    ("datetime-a-real-instant", None, {"datetime": {"seconds": "1756700000", "nanos": 0}}),
    ("uuid-hyphenated-lowercase", None, {"uuid": "0102030405060708090a0b0c0d0e0f10"}),
    ("table-resolved-to-its-name", None, {"table": 7}),
    ("table-unresolvable-is-visibly-not-a-name", "The brackets are the point.", {"table": 99}),
    ("record-integer-id", None, {"record": {"table": 3, "id": {"int": "7"}}}),
    ("record-name-id", None, {"record": {"table": 3, "id": {"text": "alice"}}}),
    ("array-of-mixed", "Elements recurse through the same table.", {"array": [{"integer": "1"}, {"string": "two"}, {"null": None}]}),
    ("array-empty", None, {"array": []}),
    ("set-arrives-as-an-array", "The collection type is not recoverable.", {"set": [{"bool": True}, {"bool": False}]}),
    (
        "object-omits-a-none-field",
        "The rule that makes none and null distinguishable in an object.",
        {"object": {"present": {"integer": "1"}, "absent": {"none": None}, "explicit": {"null": None}}},
    ),
    ("object-empty", None, {"object": {}}),
    ("object-nested", None, {"object": {"inner": {"object": {"deep": {"bool": True}}}}}),
    ("range-included-to-excluded", None, {"range": {"start": {"included": {"integer": "1"}}, "end": {"excluded": {"integer": "5"}}}}),
    (
        "range-unbounded-end-carries-no-value-key",
        "1.. is not 1..null. The key's absence is what keeps them apart.",
        {"range": {"start": {"included": {"integer": "1"}}, "end": {"unbounded": None}}},
    ),
    ("range-both-unbounded", None, {"range": {"start": {"unbounded": None}, "end": {"unbounded": None}}}),
    (
        "range-decimal-endpoint-is-quoted",
        "Each endpoint is written by the same table, so it is lossy the same way.",
        {"range": {"start": {"included": {"decimal": {"mantissa": "150", "scale": 2}}}, "end": {"excluded": {"decimal": {"mantissa": "250", "scale": 2}}}}},
    ),
    (
        "geometry-point-longitude-first",
        "The silent geospatial bug: swapping these produces a valid document.",
        {"geometry": {"point": {"lon": "4002d14e3bcd35a8", "lat": "40486da5119ce076"}}},
    ),
    (
        "geometry-point-non-finite-coordinate-is-null",
        "Cannot arise from a well-formed shape; can arise from bytes.",
        {"geometry": {"point": {"lon": "7ff8000000000000", "lat": "40486da5119ce076"}}},
    ),
    (
        "geometry-polygon-with-a-hole",
        "An interior ring, skipped by a decoder that assumes there are none.",
        {"geometry": {"polygon": [
            [{"lon": "0000000000000000", "lat": "0000000000000000"},
             {"lon": "4024000000000000", "lat": "0000000000000000"},
             {"lon": "4024000000000000", "lat": "4024000000000000"},
             {"lon": "0000000000000000", "lat": "0000000000000000"}],
            [{"lon": "3ff0000000000000", "lat": "3ff0000000000000"},
             {"lon": "4000000000000000", "lat": "3ff0000000000000"},
             {"lon": "4000000000000000", "lat": "4000000000000000"},
             {"lon": "3ff0000000000000", "lat": "3ff0000000000000"}],
        ]}},
    ),
    (
        "geometry-collection-nested",
        "A member is a whole geometry, not a bare shape.",
        {"geometry": {"collection": [
            {"point": {"lon": "3ff0000000000000", "lat": "4000000000000000"}},
            {"line": [{"lon": "0000000000000000", "lat": "0000000000000000"},
                      {"lon": "3ff0000000000000", "lat": "3ff0000000000000"}]},
        ]}},
    ),
    ("geometry-line-empty", "Permitted by the codec; a decoder must not refuse it.", {"geometry": {"line": []}}),
    ("regex-is-carried-not-compiled", "A client MUST NOT compile it.", {"regex": "^a.*z$"}),
    ("regex-with-a-backslash", None, {"regex": "\\d+\\s"}),
    # --- Deliberately included to reach a silence -----------------------------
    # These are values the store holds and the document does not say how to
    # write. Leaving them out of the case list would have made the corpus look
    # complete; each one is here so that the gap is recorded rather than avoided.
    ("float-negative-zero", None, {"float_bits": "8000000000000000"}),
    ("datetime-with-nanoseconds", None, {"datetime": {"seconds": "0", "nanos": 500000000}}),
    ("duration-zero", None, {"duration": {"seconds": "0", "nanos": 0}}),
    ("duration-negative-whole", None, {"duration": {"seconds": "-5", "nanos": 0}}),
    (
        "duration-negative",
        "The floor pair: seconds BELOW zero with a POSITIVE remainder, which is "
        "-500ms and not -1s500ms. Written field by field it spells a span one "
        "second longer than itself, and reading that back adds another second "
        "every time.",
        {"duration": {"seconds": "-1", "nanos": 500000000}},
    ),
    (
        "duration-negative-smallest",
        "The smallest span below zero. Same carry, reaching every unit.",
        {"duration": {"seconds": "-1", "nanos": 999999999}},
    ),
    ("duration-sub-second", None, {"duration": {"seconds": "0", "nanos": 1500}}),
    (
        "duration-a-day-is-written-in-hours",
        "The hour is the largest unit written. `1d` and `1w` are readable on "
        "input and never come back — a week is 168h.",
        {"duration": {"seconds": "86400", "nanos": 0}},
    ),
    (
        "float-with-no-fractional-part",
        "2.0, and the reason the float row is lossy: it is written `2` and is "
        "not distinguishable from the integer.",
        {"float_bits": "4000000000000000"},
    ),
    (
        "record-text-id-that-looks-like-an-integer",
        "Identical to the integer id 7. A client MUST NOT parse this half back "
        "into a typed id.",
        {"record": {"table": 3, "id": {"text": "7"}}},
    ),
    ("record-uuid-id", None, {"record": {"table": 3, "id": {"uuid": "0102030405060708090a0b0c0d0e0f10"}}}),
    ("record-bytes-id", None, {"record": {"table": 3, "id": {"bytes": "00ff"}}}),
    ("record-text-id-needing-quotes", None, {"record": {"table": 3, "id": {"text": "a b:c"}}}),
    ("record-unresolvable-table", None, {"record": {"table": 99, "id": {"int": "1"}}}),
]

OUTCOME_CASES = [
    ("outcome-done", "A statement with nothing to report.", {"done": None}),
    ("outcome-value", None, {"value": {"integer": "4"}}),
    (
        "outcome-value-none-has-no-value-key",
        "NOT the same as {\"kind\":\"value\",\"value\":null}. The language's none.",
        {"value": {"none": None}},
    ),
    (
        "outcome-value-null-has-one",
        "A stored null. JSON has one word for both; the key's presence separates them.",
        {"value": {"null": None}},
    ),
    ("outcome-removed", "The count a conditional delete removed.", {"removed": 12043}),
    ("outcome-removed-zero", "Nothing matched — still a count, not `done`.", {"removed": 0}),
    (
        "outcome-unknown",
        "A kind this build has never seen. A client MUST expose it and MUST NOT "
        "stop reading the list at it — it is not an outcome carrying nothing.",
        {"unknown": None},
    ),
    (
        "outcome-keys-is-an-array-of-strings",
        "Never of objects: there is no value beside the identity.",
        {"keys": [
            {"record": {"table": 3, "id": {"int": "1"}}},
            {"record": {"table": 3, "id": {"text": "ada"}}},
        ]},
    ),
    ("outcome-keys-empty", None, {"keys": []}),
    (
        "outcome-records-element-is-a-pair",
        "A client that types this array as the records reads `name` one level too high.",
        {"records": {
            "plan": {"access": "record", "exact": True, "table": "users"},
            "rows": [{"id": {"record": {"table": 3, "id": {"int": "1"}}},
                      "value": {"object": {"name": {"string": "ada"}}}}],
        }},
    ),
    (
        "outcome-records-nothing-to-report",
        "No notes and not ONLY, so neither key appears — byte-identical to what "
        "this response was before either clause existed.",
        {"records": {
            "plan": {"access": "scan", "exact": True, "table": "users", "cells": 40},
            "rows": [],
        }},
    ),
    (
        "outcome-records-with-a-note",
        "`kind` may be grouped on; `message` is for a person and MUST NOT be branched on.",
        {"records": {
            "plan": {"access": "scan", "exact": True, "table": "users", "cells": 40},
            "notes": [{"kind": "fell-back",
                       "message": "the index path could not fill the bound, so the read took the scan path instead"}],
            "rows": [],
        }},
    ),
    (
        "outcome-records-only",
        "`records` stays an array holding at most one — the key's type does not change.",
        {"records": {
            "plan": {"access": "index", "exact": True, "table": "users", "index": "by_name", "shape": "point"},
            "only": True,
            "rows": [{"id": {"record": {"table": 3, "id": {"int": "1"}}},
                      "value": {"object": {"name": {"string": "ada"}}}}],
        }},
    ),
    (
        "outcome-records-plan-omits-what-it-does-not-know",
        "A scan's plan is the keys it knows, not eight of which six say nothing — "
        "except `exact`, which is written even when it is dull.",
        {"records": {"plan": {"access": "scan", "exact": True}, "rows": []}},
    ),
    (
        "outcome-records-plan-says-why-it-is-not-exact",
        "`inexact` accompanies a false `exact` and carries the node's own words; a "
        "client MUST NOT phrase the reason itself, having not performed the read.",
        {"records": {
            "plan": {"access": "approximate", "exact": False, "table": "points",
                     "index": "by_at",
                     "inexact": "an approximate index answered this, so a nearer record may exist"},
            "rows": [],
        }},
    ),
    (
        "outcome-records-plan-without-exactness-said-nothing",
        "The third state of \u00a73.5 on this transport: a plan with no `exact` key "
        "at all is a node that predates the field and made NO claim. A client MUST "
        "NOT read it as `exact: true`.",
        {"records": {"plan": {"access": "scan", "table": "users"}, "rows": []}},
    ),
    (
        "outcome-records-suggestion-not-sought",
        "No `suggestion` key: no term dictionary was consulted, which is nearly "
        "every read on this transport. This is the ABSENCE of a claim and MUST "
        "NOT be read as `nothing is near`.",
        {"records": {
            "plan": {"access": "scan", "exact": True, "table": "users"},
            "rows": [],
        }},
    ),
    (
        "outcome-records-suggestion-nothing-nearer",
        "Present and EMPTY is the claim: a dictionary was asked and holds every "
        "term the query named. A client that renders this identically to the "
        "case above has reported a negative the node never checked.",
        {"records": {
            "plan": {"access": "index", "exact": True, "table": "users", "index": "by_body"},
            "suggestion": [],
            "rows": [],
        }},
    ),
    (
        "outcome-records-suggestion-did-you-mean",
        "`typed` is the term AFTER the field's analyzer ran, not the substring "
        "the reader wrote. The records are the ones the query as typed returns — "
        "a correction is advice about a different question and is never "
        "substituted into the executed read.",
        {"records": {
            "plan": {"access": "index", "exact": True, "table": "notes", "index": "by_body"},
            "suggestion": [{"typed": "vecter", "instead": "vector"}],
            "rows": [],
        }},
    ),
    (
        "outcome-records-suggestion-beside-records-that-answered",
        "A query that gets one word right and one wrong returns records AND earns "
        "a correction. A client that only surfaces suggestions on an empty answer "
        "stays silent exactly where a reader most needs it.",
        {"records": {
            "plan": {"access": "index", "exact": True, "table": "notes", "index": "by_body"},
            "suggestion": [{"typed": "vecter", "instead": "vector"}],
            "rows": [{"id": {"record": {"table": 7, "id": {"int": "3"}}},
                      "value": {"object": {"body": {"string": "the engine stores every vector"}}}}],
        }},
    ),
]


def build():
    cases = []
    outcomes = []
    gaps = []
    seen = set()

    for name, note, value in VALUE_CASES:
        if name in seen:
            raise ValueError(f"two cases named {name}")
        seen.add(name)
        try:
            rendered = spell(value, NAMES)
        except Silence as why:
            gaps.append({"case": name, "unit": "value", "silence": str(why)})
            continue
        entry = {"name": name, "value": value}
        if rendered is OMITTED:
            entry["omitted"] = True
        else:
            entry["json"] = rendered
        if note:
            entry["note"] = note
        cases.append(entry)

    for name, note, outcome in OUTCOME_CASES:
        if name in seen:
            raise ValueError(f"two cases named {name}")
        seen.add(name)
        try:
            rendered = spell_outcome(outcome, NAMES)
        except Silence as why:
            gaps.append({"case": name, "unit": "outcome", "silence": str(why)})
            continue
        entry = {"name": name, "outcome": outcome, "json": rendered}
        if note:
            entry["note"] = note
        outcomes.append(entry)

    # A silence no single case can reach, because it is about a property of the
    # rendering rather than about one value. §5.7.1 now states the half that is
    # publishable — the order is ascending, deduplicated and deterministic — and
    # deliberately withholds the cross-type rank, since section 4 says a client
    # need not implement one. So what remains is a stated non-guarantee rather
    # than a gap, and a case would only pin an order clients are told not to
    # depend on.
    gaps.append(
        {
            "case": "set-order-across-types",
            "unit": "value",
            "silence": "§5.7.1 states the order is deterministic and deliberately "
            "does not publish the rank BETWEEN types; a client must not depend "
            "on it, so no vector fixes it",
        }
    )

    document = {
        "protocol_major": 1,
        "protocol_minor": 0,
        "what_this_is": (
            "JSON-rendering vectors for sections 5.6 and 5.7 of "
            "spec/protocol-v1.md. A conforming client, given the answer JSON, "
            "MUST read each `json` back to the stated `value`, and given an "
            "outcome object MUST read it as the stated outcome. A case carrying "
            "`omitted` states that the value's encoding is the ABSENCE of its "
            "key, which is what keeps `none` and `null` apart."
        ),
        "this_one_is_decode_only": (
            "values-v1.json requires both directions and explains why one is not "
            "enough. Here there is only one direction. A client never encodes a "
            "JSON value on this surface: a POST /script parameter carries "
            "TessariQL source rather than JSON (5.5), so nothing a client sends "
            "is a value from the 5.7 table. This corpus is weaker than the value "
            "corpus by construction, and says so rather than letting the "
            "stronger requirement be assumed."
        ),
        "structural_comparison_not_textual": (
            "`json` is a JSON VALUE, not JSON text. Neither 5.6 nor 5.7 makes key "
            "order or whitespace normative, so a client parses its own output and "
            "compares structurally. Comparing rendered text would fail a "
            "conforming client for a property the document does not state."
        ),
        "names": {str(k): v for k, v in NAMES.items()},
        "names_note": (
            "The names block of 3.9, as a map from table id to name. A table or "
            "record reference whose id is absent from it is unresolvable, and is "
            "written in the bracketed form of 5.7 rather than guessed."
        ),
        "gaps_are_the_point": (
            "`gaps` lists what this document does not say precisely enough to "
            "render. They are recorded rather than filled in from the server's "
            "behaviour: LR-PROTO-004 asks a corpus written from the document "
            "alone to disagree where the document is unclear, and a case invented "
            "to remove a gap would destroy the only property this file has."
        ),
        "generated_by": "conformance/generate_json.py, a second implementation written from spec/protocol-v1.md alone",
        "not_yet_verified_against": "a running node",
        "cases": cases,
        "outcomes": outcomes,
        "gaps": gaps,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate and compare against the committed corpus; do not write",
    )
    arguments = parser.parse_args()
    fresh = build()

    if not arguments.check:
        sys.stdout.write(fresh)
        return 0

    if not CORPUS.exists():
        sys.stderr.write(f"{CORPUS.name} is missing; run without --check to write it\n")
        return 1
    if CORPUS.read_text(encoding="utf-8") == fresh:
        counted = json.loads(fresh)
        sys.stderr.write(
            f"{CORPUS.name} matches the generator "
            f"({len(counted['cases'])} values, {len(counted['outcomes'])} outcomes, "
            f"{len(counted['gaps'])} gaps)\n"
        )
        return 0
    sys.stderr.write(
        f"{CORPUS.name} DIFFERS from the generator. "
        "Either the generator changed and the corpus was not regenerated, or the "
        "corpus was hand-edited — which it must never be.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
