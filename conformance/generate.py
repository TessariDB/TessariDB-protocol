#!/usr/bin/env python3
"""Generate the value-codec conformance corpus for protocol version 1.

This is a **second, independent implementation** of the value codec, written from
`spec/protocol-v1.md` alone. It exists so the corpus is evidence that the
specification is sufficient — a corpus dumped out of the reference client would
only record that client's beliefs, including any place it and the document
disagree.

Deliberately no dependency on anything but the standard library, and no reference
to any other implementation while it was written.

Usage:
    python3 generate.py > values-v1.json     regenerate the corpus
    python3 generate.py --check              fail if the committed corpus differs
"""

import argparse
import json
import pathlib
import struct
import sys

CORPUS = pathlib.Path(__file__).with_name("values-v1.json")

# --- §4.1 type tags -----------------------------------------------------------
TAG_NONE = 0x01
TAG_NULL = 0x02
TAG_BOOL = 0x03
TAG_NUMBER = 0x04
TAG_STRING = 0x05
TAG_BYTES = 0x06
TAG_DURATION = 0x07
TAG_DATETIME = 0x08
TAG_UUID = 0x09
TAG_TABLE = 0x0A
TAG_RECORD = 0x0B
TAG_ARRAY = 0x0C
TAG_OBJECT = 0x0D
TAG_RANGE = 0x0E
TAG_SET = 0x0F
TAG_GEOMETRY = 0x10
TAG_REGEX = 0x11

# --- §4.6 geometry shape kinds ------------------------------------------------
SHAPE_POINT = 0x01
SHAPE_LINE = 0x02
SHAPE_POLYGON = 0x03
SHAPE_MULTI_POINT = 0x04
SHAPE_MULTI_LINE = 0x05
SHAPE_MULTI_POLYGON = 0x06
SHAPE_COLLECTION = 0x07

# --- §4.3 number kinds --------------------------------------------------------
NUM_INTEGER = 0x01
NUM_FLOAT = 0x02
NUM_DECIMAL = 0x03

# --- §4.4 bound kinds ---------------------------------------------------------
BOUND_UNBOUNDED = 0x01
BOUND_INCLUDED = 0x02
BOUND_EXCLUDED = 0x03

# --- §4.5 record id discriminants --------------------------------------------
RID_INT = 0x01
RID_TEXT = 0x02
RID_UUID = 0x03
RID_BYTES = 0x04

# --- §4.2 escaping ------------------------------------------------------------
ESCAPE = 0x00
TERMINATOR = 0x01
ESCAPED_ZERO = 0xFF


# --- §2.2 value-layer primitives ---------------------------------------------
def put_u32(value: int) -> bytes:
    """Plain big-endian. No inversion at this width (§2.2)."""
    return struct.pack(">I", value)


def put_i64(value: int) -> bytes:
    """Big-endian with the top bit of the first byte inverted (§2.2).

    NOT two's-complement big-endian. This is the detail the specification calls
    the single most important one in the document, and it is written here from
    that rule rather than copied from anywhere.
    """
    raw = bytearray(struct.pack(">q", value))
    raw[0] ^= 0x80
    return bytes(raw)


def put_lenbytes(raw: bytes) -> bytes:
    return put_u32(len(raw)) + raw


def put_varbytes(raw: bytes) -> bytes:
    """Escaped and terminated (§4.2): 0x00 -> 0x00 0xFF, then 0x00 0x01."""
    out = bytearray()
    for byte in raw:
        if byte == ESCAPE:
            out.append(ESCAPE)
            out.append(ESCAPED_ZERO)
        else:
            out.append(byte)
    out.append(ESCAPE)
    out.append(TERMINATOR)
    return bytes(out)


# --- the value encoder --------------------------------------------------------
def encode(value: dict) -> bytes:
    """Encode one value from the corpus's JSON notation."""
    if len(value) != 1:
        raise ValueError(f"a value is one tagged key, got {sorted(value)}")
    kind, held = next(iter(value.items()))

    if kind == "none":
        return bytes([TAG_NONE])
    if kind == "null":
        return bytes([TAG_NULL])
    if kind == "bool":
        return bytes([TAG_BOOL, 1 if held else 0])
    if kind == "integer":
        return bytes([TAG_NUMBER, NUM_INTEGER]) + put_i64(int(held))
    if kind == "float_bits":
        # The bits, plain big-endian — not inverted. Three numeric shapes, two
        # conventions, and this is the asymmetry (§4.3).
        return bytes([TAG_NUMBER, NUM_FLOAT]) + bytes.fromhex(held)
    if kind == "decimal":
        mantissa = int(held["mantissa"])
        return (
            bytes([TAG_NUMBER, NUM_DECIMAL])
            + mantissa.to_bytes(16, "big", signed=True)
            + put_u32(held["scale"])
        )
    if kind == "string":
        return bytes([TAG_STRING]) + put_lenbytes(held.encode("utf-8"))
    if kind == "bytes":
        return bytes([TAG_BYTES]) + put_lenbytes(bytes.fromhex(held))
    if kind == "duration":
        return (
            bytes([TAG_DURATION])
            + put_i64(int(held["seconds"]))
            + put_u32(held["nanos"])
        )
    if kind == "datetime":
        return (
            bytes([TAG_DATETIME])
            + put_i64(int(held["seconds"]))
            + put_u32(held["nanos"])
        )
    if kind == "uuid":
        raw = bytes.fromhex(held)
        if len(raw) != 16:
            raise ValueError("a uuid is sixteen bytes")
        return bytes([TAG_UUID]) + raw
    if kind == "table":
        return bytes([TAG_TABLE]) + put_u32(held)
    if kind == "record":
        return bytes([TAG_RECORD]) + put_u32(held["table"]) + encode_record_id(held["id"])
    if kind == "array":
        out = bytes([TAG_ARRAY]) + put_u32(len(held))
        return out + b"".join(encode(item) for item in held)
    if kind == "object":
        # Emitted in name order. The node re-normalises on decode either way, so
        # this makes equal objects encode to equal bytes rather than satisfying a
        # requirement (§4.1).
        out = bytes([TAG_OBJECT]) + put_u32(len(held))
        for name in sorted(held):
            out += put_lenbytes(name.encode("utf-8")) + encode(held[name])
        return out
    if kind == "range":
        return (
            bytes([TAG_RANGE])
            + encode_bound(held["start"])
            + encode_bound(held["end"])
        )
    if kind == "set":
        out = bytes([TAG_SET]) + put_u32(len(held))
        return out + b"".join(encode(item) for item in held)
    if kind == "geometry":
        return bytes([TAG_GEOMETRY]) + encode_geometry(held)
    if kind == "regex":
        # The pattern as written. Not compiled here, and §7 item 15 says not
        # compiled in a client either — dialects disagree about what is valid,
        # so a validating client rejects patterns the node would have accepted.
        return bytes([TAG_REGEX]) + put_lenbytes(held.encode("utf-8"))
    raise ValueError(f"no such value kind: {kind}")


# --- §4.6 geometry ------------------------------------------------------------
def encode_position(held: dict) -> bytes:
    """Longitude first, each as the double's bits, plain big-endian (§4.6).

    Bits rather than a decimal literal, for the reason the corpus writes floats
    as bits everywhere else: a coordinate that goes through decimal text is a
    different coordinate, and the difference survives every round trip a single
    implementation can perform on itself.
    """
    longitude = bytes.fromhex(held["lon"])
    latitude = bytes.fromhex(held["lat"])
    if len(longitude) != 8 or len(latitude) != 8:
        raise ValueError("a coordinate is eight bytes of IEEE-754 bits")
    return longitude + latitude


def encode_positions(held: list) -> bytes:
    return put_u32(len(held)) + b"".join(encode_position(one) for one in held)


def encode_polygon(held: dict) -> bytes:
    out = encode_positions(held["exterior"])
    interiors = held.get("interiors", [])
    out += put_u32(len(interiors))
    return out + b"".join(encode_positions(ring) for ring in interiors)


def encode_geometry(held: dict) -> bytes:
    kind, shape = next(iter(held.items()))
    if kind == "point":
        return bytes([SHAPE_POINT]) + encode_position(shape)
    if kind == "line":
        return bytes([SHAPE_LINE]) + encode_positions(shape)
    if kind == "polygon":
        return bytes([SHAPE_POLYGON]) + encode_polygon(shape)
    if kind == "multipoint":
        return bytes([SHAPE_MULTI_POINT]) + encode_positions(shape)
    if kind == "multiline":
        out = bytes([SHAPE_MULTI_LINE]) + put_u32(len(shape))
        return out + b"".join(encode_positions(line) for line in shape)
    if kind == "multipolygon":
        out = bytes([SHAPE_MULTI_POLYGON]) + put_u32(len(shape))
        return out + b"".join(encode_polygon(one) for one in shape)
    if kind == "collection":
        out = bytes([SHAPE_COLLECTION]) + put_u32(len(shape))
        # Recursive, and each member carries its own kind byte — which is what
        # lets a collection hold a collection.
        return out + b"".join(encode_geometry(one) for one in shape)
    raise ValueError(f"no such shape: {kind}")


def encode_record_id(held: dict) -> bytes:
    kind, value = next(iter(held.items()))
    if kind == "int":
        return bytes([RID_INT]) + put_i64(int(value))
    if kind == "text":
        return bytes([RID_TEXT]) + put_varbytes(value.encode("utf-8"))
    if kind == "uuid":
        return bytes([RID_UUID]) + bytes.fromhex(value)
    if kind == "bytes":
        return bytes([RID_BYTES]) + put_varbytes(bytes.fromhex(value))
    raise ValueError(f"no such record id kind: {kind}")


def encode_bound(held) -> bytes:
    if held == "unbounded":
        return bytes([BOUND_UNBOUNDED])
    kind, value = next(iter(held.items()))
    if kind == "included":
        return bytes([BOUND_INCLUDED]) + encode(value)
    if kind == "excluded":
        return bytes([BOUND_EXCLUDED]) + encode(value)
    raise ValueError(f"no such bound: {kind}")


# --- the cases ----------------------------------------------------------------
def bits(number: float) -> str:
    """The double's bits as hex, for writing a coordinate readably below.

    The literal is converted once, here, by the language's own decimal-to-double
    rule; what reaches the corpus is the bit pattern. A client never sees the
    decimal and so never has to agree about how to parse it.
    """
    return struct.pack(">d", number).hex()


def at(longitude: float, latitude: float) -> dict:
    """A position, longitude first — the argument order the bytes are in."""
    return {"lon": bits(longitude), "lat": bits(latitude)}


# A square, closed: the first and last position are the same one (§4.6).
UNIT_SQUARE = [at(0.0, 0.0), at(1.0, 0.0), at(1.0, 1.0), at(0.0, 1.0), at(0.0, 0.0)]
INNER_SQUARE = [
    at(0.25, 0.25),
    at(0.75, 0.25),
    at(0.75, 0.75),
    at(0.25, 0.75),
    at(0.25, 0.25),
]

CASES = [
    ("none-is-not-null", "An absent field. Distinct from null, and the distinction is the point.", {"none": None}),
    ("null-is-not-none", "A present field holding nothing.", {"null": None}),
    ("bool-true", None, {"bool": True}),
    ("bool-false", None, {"bool": False}),
    ("integer-zero", "Under the inversion, zero is 0x80 followed by seven zero bytes.", {"integer": "0"}),
    ("integer-one", "The canonical trap: 80 00 00 00 00 00 00 01, not 00 .. 01.", {"integer": "1"}),
    ("integer-minus-one", "Just below zero under the inversion.", {"integer": "-1"}),
    ("integer-min", "The inversion maps i64::MIN to all-zero.", {"integer": "-9223372036854775808"}),
    ("integer-max", "And i64::MAX to all-ones.", {"integer": "9223372036854775807"}),
    ("float-one", "1.0 as its bits, plain big-endian.", {"float_bits": "3ff0000000000000"}),
    ("float-negative-zero", "Negative zero is a distinct bit pattern and must survive.", {"float_bits": "8000000000000000"}),
    ("float-nan", "A quiet NaN. Its bits travel; comparing decoded NaNs for equality is the caller's problem.", {"float_bits": "7ff8000000000000"}),
    ("decimal-12-34", "Mantissa 1234, scale 2. The two numbers that define an exact decimal.", {"decimal": {"mantissa": "1234", "scale": 2}}),
    ("decimal-negative", "A negative mantissa is a signed i128, plain big-endian.", {"decimal": {"mantissa": "-1", "scale": 0}}),
    ("string-empty", None, {"string": ""}),
    ("string-ascii", None, {"string": "hello"}),
    ("string-unicode", "Length is in BYTES, not characters.", {"string": "привет"}),
    ("bytes-empty", None, {"bytes": ""}),
    ("bytes-with-zero", "A zero inside a length-prefixed block needs no escaping.", {"bytes": "0001ff00"}),
    ("duration-negative", "A span may run backwards.", {"duration": {"seconds": "-5", "nanos": 999999999}}),
    ("duration-zero", None, {"duration": {"seconds": "0", "nanos": 0}}),
    ("datetime-epoch", None, {"datetime": {"seconds": "0", "nanos": 0}}),
    ("datetime-before-epoch", "A negative second, which the inversion handles.", {"datetime": {"seconds": "-1", "nanos": 1}}),
    ("uuid", None, {"uuid": "0102030405060708090a0b0c0d0e0f10"}),
    ("table", None, {"table": 42}),
    ("record-int-id", None, {"record": {"table": 3, "id": {"int": "-1"}}}),
    ("record-text-id", "A text id is escaped and terminated, not length-prefixed.", {"record": {"table": 1, "id": {"text": "alice"}}}),
    ("record-text-id-with-slash", "A slash is an ordinary byte in an identity.", {"record": {"table": 1, "id": {"text": "a/b"}}}),
    ("record-uuid-id", None, {"record": {"table": 7, "id": {"uuid": "ffffffffffffffffffffffffffffffff"}}}),
    ("record-bytes-id-with-zeros", "The escape earns its keep: zeros inside and at the end.", {"record": {"table": 2, "id": {"bytes": "000100ff00"}}}),
    ("array-empty", None, {"array": []}),
    ("array-mixed", None, {"array": [{"null": None}, {"bool": True}, {"integer": "7"}]}),
    ("object-empty", None, {"object": {}}),
    ("object-two-fields", "Emitted in name order, so equal objects encode equal.", {"object": {"b": {"null": None}, "a": {"bool": False}}}),
    ("set-single", None, {"set": [{"bool": True}]}),
    ("range-closed-open", None, {"range": {"start": {"included": {"integer": "1"}}, "end": {"excluded": {"integer": "9"}}}}),
    ("range-unbounded-start", None, {"range": {"start": "unbounded", "end": {"included": {"string": "end"}}}}),
    ("nested-through-every-container", "An array holding an object holding a set holding a range.", {
        "array": [{"object": {"inner": {"set": [{"range": {"start": "unbounded", "end": {"included": {"string": "end"}}}}]}}}]
    }),
    ("geometry-point", "Longitude first. Swapping the pair puts Paris in the Indian Ocean, and nothing reports it.", {
        "geometry": {"point": at(2.3522, 48.8566)}
    }),
    ("geometry-point-negative-zero", "-0.0 and 0.0 are different coordinates here, because the comparison is on bits.", {
        "geometry": {"point": at(-0.0, 0.0)}
    }),
    ("geometry-point-at-the-limits", "The corners of the valid range; the codec does not enforce them.", {
        "geometry": {"point": at(-180.0, -90.0)}
    }),
    ("geometry-line", "Two positions, count-prefixed.", {
        "geometry": {"line": [at(0.0, 0.0), at(1.0, 1.0)]}
    }),
    ("geometry-polygon-no-holes", "An interior-ring count of zero is still written.", {
        "geometry": {"polygon": {"exterior": UNIT_SQUARE, "interiors": []}}
    }),
    ("geometry-polygon-with-a-hole", "Exterior ring, then the count, then each hole as its own ring.", {
        "geometry": {"polygon": {"exterior": UNIT_SQUARE, "interiors": [INNER_SQUARE]}}
    }),
    ("geometry-multipoint", None, {
        "geometry": {"multipoint": [at(0.0, 0.0), at(2.0, 3.0)]}
    }),
    ("geometry-multiline", "A count of lines, each of which is itself a count of positions.", {
        "geometry": {"multiline": [[at(0.0, 0.0), at(1.0, 0.0)], [at(0.0, 1.0), at(1.0, 1.0)]]}
    }),
    ("geometry-multipolygon", None, {
        "geometry": {"multipolygon": [{"exterior": UNIT_SQUARE, "interiors": []}]}
    }),
    ("geometry-collection-of-mixed-shapes", "Each member carries its own kind byte.", {
        "geometry": {"collection": [{"point": at(1.0, 2.0)}, {"line": [at(0.0, 0.0), at(1.0, 1.0)]}]}
    }),
    ("geometry-collection-nested", "A collection may hold a collection, which is why the member is a whole geometry.", {
        "geometry": {"collection": [{"collection": [{"point": at(0.0, 0.0)}]}]}
    }),
    ("geometry-line-empty", "Zero positions. Not a well-formed line; the codec still carries it.", {
        "geometry": {"line": []}
    }),
    ("regex-simple", "The pattern as written, uncompiled.", {"regex": "^a.*z$"}),
    ("regex-with-a-backslash", "A backslash is an ordinary byte to the codec.", {"regex": "\\d{3}-\\d{4}"}),
    ("regex-empty", "Empty is a pattern; whether it is a useful one is the store's opinion, not the codec's.", {"regex": ""}),
    ("regex-unicode", "Length is in bytes, as everywhere else.", {"regex": "привет+"}),
]


def build() -> str:
    cases = []
    seen = set()
    for name, note, value in CASES:
        if name in seen:
            # A duplicate name would silently shadow a vector in any client that
            # keys its results by name, and the shadowed one would look like it
            # had passed.
            raise ValueError(f"two cases named {name}")
        seen.add(name)
        entry = {"name": name, "value": value, "bytes": encode(value).hex()}
        if note:
            entry["note"] = note
        cases.append(entry)

    document = {
        "protocol_major": 1,
        "protocol_minor": 0,
        "what_this_is": (
            "Value-codec test vectors. `bytes` is authoritative: a client MUST "
            "encode `value` to exactly these bytes, and MUST decode these bytes "
            "to exactly this value. Integers are written as JSON strings because "
            "the range exceeds what some languages parse safely, and floats and "
            "geometry coordinates as their IEEE-754 bits for the same reason."
        ),
        "run_it_both_ways": (
            "Decoding alone does not check a codec. A codec that is wrong in the "
            "same way on both sides round-trips perfectly: mutating both halves "
            "of a client's inverted-i64 handling to plain big-endian left "
            "thirteen of fourteen round-trip tests passing, and only a "
            "byte-level comparison against these vectors caught it."
        ),
        "generated_by": "conformance/generate.py, a second implementation written from spec/protocol-v1.md alone",
        "not_yet_verified_against": "a running node",
        "cases": cases,
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
    committed = CORPUS.read_text(encoding="utf-8")
    if committed == fresh:
        sys.stderr.write(f"{CORPUS.name} matches the generator ({len(CASES)} cases)\n")
        return 0
    sys.stderr.write(
        f"{CORPUS.name} DIFFERS from the generator. "
        "Either the generator changed and the corpus was not regenerated, or the "
        "corpus was hand-edited — which it must never be.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
