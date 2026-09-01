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

Point your client's test suite at this directory.

The reference Rust client reads it from a sibling checkout by default, or from
`TESSARI_PROTOCOL_CONFORMANCE`:

```sh
TESSARI_PROTOCOL_CONFORMANCE=/path/to/conformance cargo test --test conformance
```

The variable names the **directory** rather than one file, because there is now
more than one corpus in it.

**A missing corpus must fail, not skip.** A conformance suite that passes having
found nothing to check reports coverage it does not have, and that is worse than
having no suite — someone will read the green and believe it.

## The second corpus: `queries-v1.json`

`values-v1.json` is the value codec. `queries-v1.json` is the **query builder**,
and it belongs to `../spec/query-builder-v1.md` rather than to the protocol
document — §6 of the protocol puts the query language deliberately outside the
protocol, and this corpus sits beside the value corpus rather than inside it.

A case names a query in a language-neutral notation and states the text a builder
must render it to, with the parameters it must bind:

```json
{
  "name": "select-one-comparison",
  "build": { "select": { "from": "memories",
             "where": { "compare": { "field": "session", "op": "eq",
                                     "value": { "string": "abc" } } } } },
  "script": "SELECT * FROM memories WHERE session = $p0;",
  "parameters": { "p0": { "string": "abc" } }
}
```

Values use the same tagged notation as the value corpus, so a client reads both
with one reader.

Two case shapes differ from the value corpus:

- a case carrying **`refused`** instead of `script` must be refused by the
  builder, with the named reason, and never rendered;
- a case carrying **`needs`** names statements that must be executed before it —
  an `UPDATE` needs the record it changes. Offline rendering ignores them; a run
  against a node does not.

**Rendering agreement is the offline half only.** It says nothing about whether
the node's parser accepts the text. §6 point 4 of the builder contract asks any
client that can reach a node to execute every rendered case with its parameters
bound, and that is the only check that reaches the parser.

## The third corpus: `json-v1.json`

`values-v1.json` is the binary codec of §4. `json-v1.json` is the **JSON
rendering** of §5.6 and §5.7 — the outcome objects and the value spellings a
`POST /script` answer carries. A case states a value in the same tagged notation
and the JSON the surface writes for it:

```json
{
  "name": "decimal-is-a-string",
  "value": { "decimal": { "mantissa": "1234", "scale": 2 } },
  "json": "12.34"
}
```

**This one is decode-only, and that is a property rather than an oversight.**
Everything above insists a corpus runs in both directions. Here there is only
one: a client never *encodes* a JSON value on this surface, because a `/script`
parameter carries **TessariQL source** rather than JSON (§5.5), so nothing a
client sends is a value from the §5.7 table. It is weaker than the value corpus
by construction and says so rather than letting the stronger requirement be
assumed.

Three things differ from the other two corpora:

- **`json` is a JSON value, not JSON text.** Neither §5.6 nor §5.7 makes key
  order or whitespace normative, so a client parses its own output and compares
  structurally. A textual comparison would fail a conforming client for a
  property the document does not state.
- A case carrying **`omitted`** instead of `json` states that the value's encoding
  is the **absence** of its key. That is `none`, and it is what keeps `none` and
  `null` apart in a format with one word for both.
- The file has a **`gaps`** list, and it is the most useful thing in it.

### `gaps` — where the document does not say enough

**One entry.** The list is produced by writing an implementation from §5.6 and
§5.7 alone and recording every place it cannot proceed. It began at twelve — the
two outcome kinds §5.6 named without shaping, and the ten value renderings §5.7
left to the reader — and the document has since answered eleven of them.

The one that remains is `set-order-across-types`, and it is a different kind of
entry from the eleven that closed. §5.7.1 states that a set's array is ascending,
deduplicated and deterministic, and deliberately does **not** publish the rank
between values of different types, because §4 says a client is not required to
implement a total order over mixed values in order to encode one — publishing the
node's rank would make that a requirement by the back door. So it is a stated
non-guarantee that no vector can pin, rather than something the document forgot.

Entries are recorded, never filled in from the server's behaviour. A corpus
written from the document is only worth having because it **disagrees** where the
document is unclear, and a case invented to remove a gap would destroy the one
property the file has.

Read the count from the file rather than from this paragraph: `gaps` is a JSON
array and this sentence is prose that has already been wrong once.

## Regenerating

```sh
python3 generate.py > values-v1.json            # rewrite the value corpus
python3 generate.py --check                     # exit 1 if the committed file differs

python3 generate_queries.py > queries-v1.json   # rewrite the query corpus
python3 generate_queries.py --check             # exit 1 if the committed file differs

python3 generate_json.py > json-v1.json         # rewrite the JSON-rendering corpus
python3 generate_json.py --check                # exit 1 if the committed file differs
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
would notice. Until a corpus is checked against a running node it proves
agreement between implementations rather than agreement with the database.

**That check now exists for all three.**

```
python3 verify_json_against_node.py    --node 127.0.0.1:47901   # the HTTP surface
python3 verify_values_against_node.py  --node 127.0.0.1:47901   # the wire protocol
python3 verify_queries_against_node.py --node 127.0.0.1:47901   # the parser
```

Start the node for the second one with `tessaridb --serve 127.0.0.1:47901`. The
two harnesses are separate programs rather than one with a flag, because the §4
value codec never appears on the HTTP surface at all: §5.5 says a `/script`
parameter carries TessariQL **source**, so the bytes this corpus is about are
only observable over §3.

It writes each case's value as TessariQL, asks a node to render it, and compares
the answer to the corpus structurally. First run, 2026-09-01: **47 verified,
1 disagreement, 11 unreachable of 59.**

A case is `unreachable` when the value has no source that produces it — there is
no literal for `inf`, `NaN`, empty `bytes` or `regex`. **`unreachable` is a third
verdict, not a skip**, because a run that quietly passed over those cases would
print the same "all verified" as a run that checked every one of them.

**`--wire` reaches ten of the eleven.** A value with no literal can still be
*encoded*, because a wire parameter travels in the value codec rather than as
source. Point the harness at a node serving both transports over one store and a
case the language cannot write is stored over the wire and read back over HTTP:

```
tessaridb --serve 127.0.0.1:47901 --http 127.0.0.1:47902
python3 verify_json_against_node.py --node 127.0.0.1:47902 --wire 127.0.0.1:47901
```

```
units: source=59 | verified=47 | verified-via-store=10 | MISMATCH=1 | unreachable=1
```

`verified-via-store` is deliberately **not** `verified`: the value passes through
storage on the way, so a disagreement could belong to the store rather than to
the rendering. Two claims of different strength, counted apart.

Two things this settled that the file had asserted. An unresolvable table was
called unreachable *by construction* — it is not: a table reference is an **id**
in the codec, so id 99 encodes, stores and renders exactly as the corpus says.
The wall was the parser's, not the store's. And the one case still unreachable is
now unreachable for a better reason: the node **refuses** a NaN coordinate on
acceptance — *"a longitude of NaN is not a finite coordinate"* — which §4.6
explicitly permits it to do. So the corpus states the rendering of a value no
node will ever hold (`Q-PROTO-12`).

The one disagreement is set ordering: the node sorts by value, the corpus asserts
insertion order, and §5.7.1 promises only that the order is deterministic. It is
left standing rather than edited away — a corpus adjusted until it agrees with
the engine is no longer evidence about the document.

### The byte corpus against a node

`verify_values_against_node.py` runs **two checks per case**, and the reason is
the warning at the top of this file: a round trip alone passes even when a
decoder and an encoder are wrong in the same way.

| check | what is sent | what it puts on trial |
|---|---|---|
| `encode` | `RETURN <literal>;` — source, no parameter | the node's **encoder alone**, against bytes `generate.py` derived from the document |
| `roundtrip` | `RETURN $p0;` with `p0` carrying the corpus bytes | the decoder and encoder together |

`encode` is the one that cannot be satisfied by symmetric wrongness, because its
expectation never saw the node. `roundtrip` is kept because it reaches cases
`encode` cannot — a value with no literal still has bytes.

First run, 2026-09-01:

```
units: source=54 | encode-ok=38 | encode-MISMATCH=2 | encode-unreachable=14
                 | roundtrip-ok=53 | roundtrip-MISMATCH=1
```

Both disagreements are **negative zero**, and they are left standing.

- `float-negative-zero` fails both checks. §5.7.1 states that a float is
  normalised when the value is built and that a client "MUST NOT expect the sign
  of a zero to survive, here or on the wire" — so the node is following the
  document, and this vector, written from §4.3, contradicts a normative MUST NOT
  in another section. The document has to say one thing before the corpus can be
  regenerated (`Q-PROTO-9`).
- `geometry-point-negative-zero` fails `encode` and **passes `roundtrip`**, which
  is the more interesting half. §4.6 says `0.0` and `-0.0` are *different*
  coordinates, and the codec honours that in both directions — the byte pattern
  survives a parameter round trip exactly. What no source can do is *produce*
  one: `coordinates: [-0.0, 0.0]` yields `+0.0`. A distinction the document
  states deliberately is unreachable from the language (`Q-PROTO-10`).

### The query corpus against a node

```
tessaridb --serve 127.0.0.1:47901
python3 verify_queries_against_node.py --node 127.0.0.1:47901
```

This is the one that reaches the **parser**. The other two send values; a value
that renders is a value the node held, whereas a statement that renders is only a
string until something runs it. First run, 2026-09-01:

```
units: source=27 | ok=19 | builder-refused=8
inversion: node-refuses=6 | NODE-ACCEPTS=2
```

**Every rendered case parses and executes**, including the four that put a bound
parameter in the identity position — `CREATE memories:$p0 = { … }`,
`UPDATE memories:$p0 SET …`, `DELETE memories:$p0;`. Whether a node accepts a
parameter as a record id is a parser fact and no rendering test can reach it, so
until this run it was an assumption.

Eight cases carry no `script` because the **builder** must refuse them, so a node
cannot pass or fail them. They are counted apart rather than dropped from the
total. But five of those refusals make a claim *about the node* — that the naive
rendering would mean something else — and that claim is testable. So each is also
sent the way a client that did not refuse would have written it. `NODE-ACCEPTS`
there is the measurement that the refusal is load-bearing.

**Two of the eight came back `NODE-ACCEPTS`, and one that came back
`node-refuses` was refused for the wrong reason.**

- `CREATE memories:'x' = {  };` is **accepted** and stores an empty record. The
  builder refuses it as `incomplete`, so the contract is stricter than the
  language, and two clients — one using the builder, one writing script — differ.
  Which of them is right is `Q-PROTO-15`.
- `a-table-that-is-not-a-name-is-refused` carries the hostile string
  `memories; DROP COLLECTION memories; --`, and the node refuses the naive
  interpolation of it. Not because the injection is blocked: **`DROP COLLECTION`
  is not a statement this language has.** `DEFINE COLLECTION` is, `DROP TABLE`
  is, `DROP COLLECTION` is not (`Q-PROTO-14`). Substituting the statement that
  does exist, the naive rendering is accepted, both statements execute, and the
  table is gone:

  ```
  SELECT * FROM memories; DROP TABLE memories; --;   -> records, done
  SELECT * FROM memories;                            -> no table named "memories"
  ```

  So the builder's refusal **is** the only guard, exactly as the case claims —
  and the case as written demonstrates it with a statement that cannot run, which
  would let a reader conclude the parser is a second line of defence when it is
  not. `Q-PROTO-13`. The corpus is left alone: adjusting a vector because a run
  embarrassed it is the move LR-PROTO-004 exists to prevent.

The refusal messages are worth reading even where they agree. `WHERE 1st = $p0`
is rejected with *"`1st` is not a duration this store can hold"* — the lexer
reaches a duration literal before it reaches a field name.

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
