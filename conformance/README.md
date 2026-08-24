# Conformance corpus

Test vectors for the value codec of protocol version 1.0.

## What a vector claims

```json
{
  "name": "integer-one",
  "value": { "integer": "1" },
  "bytes": "04018000000000000001"
}
```

**`bytes` is authoritative.** A conforming client must

- encode `value` to **exactly** those bytes, and
- decode those bytes to **exactly** that value.

Both directions, every vector. The forward check does not imply the backward one:
an encoder and a decoder can agree with each other and both disagree with the
specification, which is the single most common way a client of this protocol goes
wrong.

## Why the notation looks the way it does

- **Integers are JSON strings.** `i64` and `i128` exceed what several languages
  parse from JSON without loss, and a corpus that quietly rounded its own
  expectations would be worse than none.
- **Floats are their IEEE-754 bits, in hex.** For the same reason, and because
  negative zero and NaN are distinct bit patterns that must survive the wire.
  A client comparing decoded NaNs should compare bits, not values.
- **A value is exactly one tagged key.** `{"integer": "1"}`, `{"null": null}`.
  There is no untagged form, because `none` and `null` are different values and
  an untagged notation could not tell them apart — which is precisely the
  distinction the protocol keeps and JSON does not.
- **Bytes and uuids are hex.**

## Running it

Point your client's test suite at `values-v1.json`.

The reference Rust client reads it from a sibling checkout by default, or from
`TESSARI_PROTOCOL_CORPUS`:

```sh
TESSARI_PROTOCOL_CORPUS=/path/to/values-v1.json cargo test --test conformance
```

**A missing corpus must fail, not skip.** A conformance suite that passes having
found nothing to check reports coverage it does not have, and that is worse than
having no suite — someone will read the green and believe it.

## Regenerating

```sh
python3 generate.py > values-v1.json   # rewrite it
python3 generate.py --check            # exit 1 if the committed file differs
```

`--check` is what stops the corpus and its generator from drifting apart, and it
is the reason the file must never be hand-edited: a vector corrected by hand is a
vector no implementation produces.

`generate.py` is a **second implementation** of the codec, written from
`../spec/protocol-v1.md` alone and depending on nothing but the standard library.
That is the point of it: a corpus dumped out of the reference client would record
that client's beliefs, including anywhere it and the document disagree. Two
independent readings agreeing byte for byte is evidence that the document says
enough.

If you change the codec, change the specification first, then this generator,
then the clients — in that order (LR-SDK-008: a behaviour the document does not
state is a defect in the document).

## What this corpus does **not** prove

That the bytes are what a **node** actually sends and accepts.

Two readers of one document can both misread it the same way, and nothing here
would notice. Verification against a running node is owed, and until it lands
this corpus proves agreement between implementations rather than agreement with
the database.

## Coverage

54 vectors as of the current generation, spanning all seventeen value types, all
seven geometry shapes, all three number shapes, all four record-id discriminants,
all three bound kinds, the escape sequences, and one value nested through every
container.

Deliberately included because they are where implementations diverge:

| vector | what it catches |
|---|---|
| `integer-one` | plain big-endian instead of the inverted form — the trap |
| `integer-min`, `integer-max` | the inversion's endpoints |
| `float-negative-zero`, `float-nan` | bit patterns an equality check would lose |
| `decimal-negative` | a signed `i128` mantissa, written plain unlike an integer |
| `none-is-not-null` | two values JSON cannot tell apart |
| `record-bytes-id-with-zeros` | escaping, including a trailing zero |
| `string-unicode` | a length in bytes, not characters |
| `geometry-point` | latitude written first — the silent geospatial bug |
| `geometry-point-negative-zero` | a coordinate compared by value rather than by bits |
| `geometry-polygon-with-a-hole` | an interior-ring count skipped when there are no holes |
| `geometry-collection-nested` | a collection member read as a shape rather than a whole geometry |
| `geometry-line-empty` | a decoder that refuses what the codec permits |
| `regex-with-a-backslash` | a client that compiles the pattern instead of carrying it |
