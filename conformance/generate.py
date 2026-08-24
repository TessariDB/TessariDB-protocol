#!/usr/bin/env python3
"""Generate the value-codec conformance corpus for protocol version 3.

This is a **second, independent implementation** of the value codec, written from
`spec/protocol-v3.md` alone. It exists so the corpus is evidence that the
specification is sufficient — a corpus dumped out of the reference client would
only record that client's beliefs, including any place it and the document
disagree.

Deliberately no dependency on anything but the standard library, and no reference
to any other implementation while it was written.

Usage:
    python3 generate.py > values-v3.json
"""

import json
import struct
import sys

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
    raise ValueError(f"no such value kind: {kind}")


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
]


def main() -> int:
    cases = []
    for name, note, value in CASES:
        entry = {"name": name, "value": value, "bytes": encode(value).hex()}
        if note:
            entry["note"] = note
        cases.append(entry)

    document = {
        "protocol_version": 3,
        "what_this_is": (
            "Value-codec test vectors. `bytes` is authoritative: a client MUST "
            "encode `value` to exactly these bytes, and MUST decode these bytes "
            "to exactly this value. Integers are written as JSON strings because "
            "the range exceeds what some languages parse safely, and floats as "
            "their IEEE-754 bits for the same reason."
        ),
        "generated_by": "conformance/generate.py, a second implementation written from spec/protocol-v3.md alone",
        "not_yet_verified_against": "a running node",
        "cases": cases,
    }
    json.dump(document, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
