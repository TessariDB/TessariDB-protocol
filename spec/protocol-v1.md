# TessariDB protocol — specification for client implementers

**Protocol version 1.0.** Drafted 2026-08-24.

Status: **draft, authoritative.** This document is the source of truth for every
client, in every language. A client is written from this document, not from the
server's source. A behaviour a client needs and this document does not state is a
**defect in this document** — it is fixed here first, then in the client.

Licence of the clients: **Apache-2.0**. The server is licensed separately —
its licence is a distinct decision from the clients', and the two are not
described as sharing one.

**Why the first version is 1.0 and not 4.** During development the protocol
carried a single version byte that moved whenever a body layout changed: it went
to 2 when a records answer began carrying table names, and to 3 when a request
began carrying bound parameter values. Both moves were correct by that rule and
pointless by purpose. **A version exists to refuse a mismatch between builds that
are deployed**, and nothing was deployed — so the bumps protected nothing, and
left behind a protocol whose first public version would be 3 with two
predecessors no client author will ever have seen.

So the count starts where the audience starts. Section 2.3 states the policy the
version now follows.

---

## 1. Two transports, and which carries what

A node serves two surfaces and **neither carries everything**.

| | wire | HTTP |
|---|---|---|
| statements and parameters | yes | yes |
| change subscription | yes | separate socket protocol |
| objects and files | — | yes |
| backup | — | yes |
| health, readiness, metrics | — | yes |

**The choice is forced, not a preference.** HTTP answers are JSON, which carries
six types; the store's value model has **seventeen**. An HTTP-only client would
reach every route and silently narrow every statement result — invisibly, because
a value that has been through JSON is still a valid value and nothing at the call
site shows what was lost. A wire-only client keeps every type and cannot store a
file.

So a conforming client uses both, and the mapping is fixed:

- **statements and subscriptions → wire**, for type fidelity;
- **objects, files, backup, health, readiness, metrics → HTTP**, because nothing
  else serves them.

A caller never picks a transport per call. The two connections stay separately
configurable and separately reportable, because they are two ports: a firewall
rule or a partial bind can leave one reachable and the other not, and "files work
but queries do not" must be diagnosable.

---

## 2. Two primitive sets — read this before section 3 or 4

The protocol has **two layers with different integer encodings**. Conflating them
does not fail to parse; it returns wrong values.

### 2.1 Frame-layer primitives (sections 3 and 5)

| primitive | encoding |
|---|---|
| `u8` | one byte |
| `u32` | 4 bytes, **big-endian, plain** |
| `u64` | 8 bytes, **big-endian, plain** |
| `text` | `u32` byte-length, then that many UTF-8 bytes. **Not** NUL-terminated |
| `bytes` | `u32` length, then that many bytes |

### 2.2 Value-layer primitives (section 4)

| primitive | encoding |
|---|---|
| `u8` | one byte |
| `u32` | 4 bytes, big-endian, plain |
| `i64` | 8 bytes, big-endian, **with the top bit of the first byte inverted** (`b[0] ^= 0x80`) |
| `fixed(n)` | `n` bytes verbatim; the width is implied by what precedes |
| `lenbytes` | `u32` length, then that many bytes |
| `varbytes` | escaped, terminated — see 4.2 |

> **The `i64` inversion is the single most important detail in this document.**
> It is not two's-complement big-endian. `Number::Integer(1)` encodes as
> `80 00 00 00 00 00 00 01`, not `00 00 00 00 00 00 00 01`. A client that writes
> plain big-endian gets **every** integer, duration, datetime and integer record
> id wrong. Encode: take the two's-complement big-endian bytes, then XOR the
> first byte with `0x80`. Decode: XOR the first byte with `0x80`, then read as
> two's-complement big-endian.
>
> The inversion exists because these primitives are shared with an
> order-preserving key encoder, where a set sign bit would sort negatives above
> positives. The value payload does not need that ordering, but it uses the same
> writer, so the bytes carry it. A client must reproduce the bytes, not the
> rationale.

### 2.3 Versioning — what the two numbers promise

The greeting carries **`major` then `minor`**, one byte each (3.1).

| numbers differ in | what a client does |
|---|---|
| `major` | **refuse at the greeting.** The bodies are not the same protocol and no conversation is possible. |
| `minor` | **proceed.** Each side learns the other's minor. A newer node must not assume a feature the older peer's minor lacks; an older client must be able to *skip* what a newer node sends. |

**The skip is what makes `minor` mean anything**, and it is why every outcome
carries its own length (3.5). Without a length, a client meeting an outcome tag
it did not know could not find where that outcome ended, so it could not read the
next one — which would make every new outcome kind breaking, which is precisely
what `minor` is supposed not to be. Four bytes per outcome buys the whole
guarantee.

**A new value type is `major`; a new outcome kind is `minor`.** The asymmetry is
a limit of the encoding rather than a policy preference, and is stated here so
that nobody later assumes otherwise:

- A value at the **top** of an outcome is length-delimited by that outcome, so a
  client that cannot decode it still knows how long it was.
- A value **nested** inside an array, object, set or range is not. Tags follow one
  another with no lengths, so an unknown tag inside a collection ends the parse
  and there is nowhere to resume from.

Length-prefixing nested values would remove that limit and cost four bytes **per
element** — on a 1536-dimension embedding stored as an array, six kilobytes of
overhead per record, against no benefit any caller has named. Refused
deliberately, and revisitable only if nested values acquire lengths for some
other reason.

**Before the first release the format changes freely and the version stands
still.** Bumping begins when a client exists that a refusal is addressed to. The
rule "the version moves when the layout moves" is right after release and is pure
ceremony before it.

---

## 3. The wire protocol

### 3.1 Connection and greeting

TCP. On connect, **both sides send** the greeting before anything else:

```
"TESS"   4 bytes, ASCII, literally 0x54 0x45 0x53 0x53
major    1 byte, currently 1
minor    1 byte, currently 0
```

Six bytes.

A client that reads something other than `TESS` reports *not this protocol* and
closes. A client that reads a **major** it does not implement reports *wrong
version*, carrying both the version found and the version supported, and closes.

**The magic is judged on its own four bytes, before the version bytes are read.**
A peer that is not a node owes nothing: it may send three bytes of an HTTP
request line and hang up. A client that waits for all six first reports that as a
truncated stream, which sends whoever reads the error to the network — when the
answer is that the address is wrong.

**A differing minor is not a refusal.** The client keeps the peer's minor, and
uses it for exactly one thing: deciding not to send what an older peer cannot
read. It never gates decoding, because decoding is already safe — an unknown
outcome is skipped by its length (3.5) and an unknown frame kind closes the
connection (3.3).

Both refusals happen **at the greeting**, never mid-conversation. A version
mismatch discovered later arrives as a decode failure that reads like corruption.

### 3.2 Frame format

Every frame:

```
kind    1 byte
length  4 bytes, big-endian, plain — the body length, not including this header
body    `length` bytes
```

The header is exactly **5 bytes**.

**Ceiling: 16 MiB** (`16 * 1024 * 1024`). A declared length above the ceiling is
refused **before anything is allocated**, and the connection closes. A length
from a stranger is not a promise, and allocating on one is the oldest denial of
service there is.

The ceiling applies **on the way out as well as in**. A client that would refuse
to read a frame that size must not send one either; a peer that emits what it
would refuse to read is running two protocols.

Reading zero bytes **between** frames is a clean goodbye. Reading zero bytes
**inside** a header or body is truncation and is an error.

### 3.3 Frame kinds

Numbered explicitly and **never renumbered**. A byte that once meant one thing
cannot be asked about after the fact by a client of a different build.

| tag | kind | direction | body |
|---|---|---|---|
| 1 | Request | client → node | 3.4 |
| 2 | Answer | node → client | 3.5 |
| 3 | Refusal | node → client | 3.6 |
| 4 | Subscribe | client → node | 3.7 |
| 5 | Change | node → client | 3.8 |

An **unknown frame kind closes the connection**. It is not skipped. A protocol
that ignores what it does not understand is one where a version mismatch looks
like silence.

A client that receives `Request`, `Subscribe`, or — on a connection that has not
subscribed — `Change`, treats it as an unknown frame.

### 3.4 Request body

```
text    the script
u8      credentials flag: 0 = none, 1 = present
        if 1:
text      user name
text      password
u32     parameter count
        repeated `count` times:
text      parameter name
bytes     the value, encoded per section 4
```

Any other credentials flag byte is malformed.

**Parameter values travel in the value codec, never as text.** This is the reason
the wire protocol exists. A value the server has to *parse* is a value that can be
parsed as something else, and binding after parsing exists precisely to make that
impossible. A client that formats parameters into the script destroys the property
invisibly, because the resulting script still looks correct.

Credentials are optional because a store with no users declared is **open** and
runs anything, which is what keeps an empty one usable. A closed store's refusal
comes from the session, not from a second rule in the client.

> There is **no TLS** on this protocol. Credentials travel as given. This belongs
> on a protected network or behind something that terminates TLS. A client must
> say so in its own documentation rather than leave it to be discovered.

### 3.5 Answer body

```
u32     outcome count
        repeated `count` times: one outcome
```

One outcome per statement in the script, in order.

Each outcome is:

```
u32     length — the bytes that follow, tag included
u8      tag
...     the rest, per the tag
```

| tag | outcome | rest of the outcome |
|---|---|---|
| 0 | Done | nothing |
| 1 | Records | `u8` access path · names (3.9) · `u32` record count · repeated: `text` identity, `bytes` value (§4) · `u32` note count · repeated: `text` kind, `text` message · `u8` only |
| 2 | Value | names (3.9) · `bytes` value (§4) |
| 3 | Keys | `u32` count · repeated `text` |
| 4 | Removed | `u64` count of records a conditional delete removed |
| 255 | Unknown | nothing |

**The length is what makes an unknown outcome survivable.** A client that does
not recognise a tag reads the length, yields `Unknown`, **steps over the
remaining bytes**, and carries on with the next outcome — so a newer node may
introduce an outcome kind anywhere in an answer without breaking an older client.

A client MUST expose an `Unknown` to its caller rather than dropping it or
erroring the whole answer: a newer node may answer with something this client has
never seen, and saying so is honest where guessing at its content is not. A
client MUST NOT stop reading at the first `Unknown`.

The length is also a bound, not just a cursor. A client MUST NOT read past it
while decoding a **recognised** outcome either: a tag whose body claims more than
its length allows is malformed, and treating the length as advisory turns one
corrupt outcome into a mis-parse of every outcome after it.

Bytes left over **inside** an outcome, after a client has decoded everything its
build knows about that tag, MUST be skipped rather than treated as an error. This
is the opposite of §4.8's rule for a *value* payload, and deliberately so: it lets
a later minor append a field to an outcome kind that already exists, and an older
client keeps reading the part it understands. A value payload has no such
allowance because it has no length to resume from.

Tag `255` is reserved for the client's own report of an unrecognised tag and is
never sent by a node.

The **access path** byte in a Records outcome names how the store found them:

| byte | name |
|---|---|
| 0 | `record` |
| 1 | `index` |
| 2 | `scan` |
| 3 | `ordered` |
| 4 | `approximate` |
| 5 | `graph` |
| 6 | `join` |
| 7 | `materialised` |
| any other | `scan` |

An unrecognised path reads as `scan`, which is the honest answer for a path this
build has no name for: it is the one path that promises nothing.

**The notes are the newest field and sit last**, which is what makes them a minor
addition rather than a breaking one: a client built before they existed reads the
records, finds bytes it has no field for, and skips them by the rule above. A
client built after them and talking to a node built before them finds the body
**ends** after the records — and MUST read that as a node with nothing to say,
not as a truncation. Both directions follow from the length in front, and neither
needs the minor to be consulted; the minor gates features that cannot be skipped,
and this one can.

A note is a **kind and a message**, not a structure. A client's two uses are to
group by the first and show the second, and a typed note would put the node's
whole note vocabulary into every client's build for no gain. A client MUST NOT
treat an unfamiliar kind as an error: kinds are added the same way outcome tags
are.

The kinds a node sends today are listed here so a client can group them
deliberately rather than by string comparison against whatever it has seen. The
list is **informative and open** — it is not a closed set, and a client that
refuses an unlisted kind is non-conforming by the paragraph above.

| kind | what the read is saying |
|---|---|
| `fell-back` | an index could have served this and did not; the read was a scan |
| `approximate` | the answer may omit a record that belonged in it |
| `compared-across-kinds` | a comparison held values of two different kinds |
| `cursor-walked` | a paged read reached its anchor by walking rather than by seeking |
| `subquery-ceiling` | an inner read hit its record ceiling, so the outer answer is built on a truncated one |

**The `only` flag sits after the notes and is the newest field.** It is `1` when
the statement wrote `ONLY` — an assertion by its author that at most one record
answers — and a client SHOULD render such an answer as the record itself rather
than as a list holding it.

It follows the notes for the same reason the notes follow the records: a client
that stops before it reads absent, and **absent MUST be read as `0`**. That is
the truth about every read written by somebody who has never heard of the clause,
and about every answer from a node built before it existed.

A client that ignores the flag is conforming but will render every `ONLY` read as
a one-element array with nothing to say it was asked for differently, so a client
that offers the clause in its query surface SHOULD read it.

**Exactness sits after the `only` flag and is the newest field — and it is the
one field on this wire whose absence is NOT its default.** It is a byte, `0` when
the node calls the answer provably the records the question names and `1` when it
does not, followed by a length-prefixed reason: the node's own words when the
byte is `1`, and empty when it is `0`.

```
+--------+------------------+
| exact  | reason (text)    |
| 1 byte | 4-byte len + utf8|
+--------+------------------+
```

Every other appended field in this body follows the rule that absent means the
default, because the default is what an older node's read actually *was*: a node
that never heard of `ONLY` served reads that were not `ONLY` reads. **This field
breaks that pattern deliberately.** A node that predates it did not serve exact
answers and forget to say so — it made no claim at all. Reading an absent
exactness as `0` would put a promise into the mouth of a node that never made
one, on the property whose entire purpose is that a caller never has to infer it.

So a client MUST distinguish **three** states and MUST NOT collapse them into
two:

| state | what the node said |
|---|---|
| byte `0` | the answer is provably the records the question names |
| byte `1` + reason | it is not, and here is why |
| field absent | the node said nothing; this is not the first state |

A client whose type for this is a boolean has already lost the distinction, and
the value it invents at the first `unwrap_or` is a promise nobody sent. A client
that cannot represent the third state SHOULD surface the answer as unverified
rather than as exact.

The reason is carried on the wire rather than derived from the access path by the
client. A client that phrased it itself would be describing a read it did not
perform, and would go on describing it after the node's own wording changed.

A client that ignores the field entirely is conforming — but it is exactly the
client this field exists for, since an approximate answer and an exact one are
the same shape, the same length, and frequently the same records.

**Record identities are text**, exactly as the store spells them — not a parsed
structure. A client that wants to name a record writes that text into its next
script. A client that re-parses identities into a typed value has created a
second spelling authority that can disagree with the store's.

### 3.6 Refusal body

The body is the store's own message, as UTF-8 text, with **no length prefix** —
it is the whole body.

Carried through **verbatim**. The session already writes messages that name the
place in the script, and a client rewording them becomes a second author for one
error.

A refusal **does not close the connection**. A client that mistyped a statement
has not stopped being a client.

### 3.7 Subscribe body

```
u64     from — the first log position to read, INCLUSIVE
u8      table flag: 0 = every table in the session's database, 1 = one table
        if 1:
text      the table name
```

Any other flag byte is malformed.

**`from` is inclusive, and the arithmetic is the client's to own.** A subscriber
stores the `sequence` of the last change it handled and resumes with **that plus
one**. Resuming with the position already handled delivers it twice; resuming with
a position not yet reached reports being caught up. Both are silent. `0` means
everything the log still holds.

A client library SHOULD own this arithmetic rather than document it.

### 3.8 Change body

```
u64     sequence — the commit this change was part of
text    the table, by name
text    the record's identity, as the store spells it
u8      what became of it: 0 = written, 1 = removed
        if 0:
bytes     the new value, encoded per section 4
```

The `sequence` is shared by every change of one commit, which is what lets a
subscriber apply them as the unit they were written as, and what it stores to
resume from.

The table is **named, not identified**: an id is meaningless outside the process
that minted it, and the catalog is on the node.

### 3.9 The names block

Appears inside Records and Value outcomes.

```
u32     count
        repeated `count` times:
u32       table id
text      table name
```

It covers the table references the outcome's values carry, and nothing else. A
reference holds a table id and the name lives in the catalog on the server;
without this block a client can only render an opaque reference. The point of the
protocol is that a client decides nothing.

### 3.10 Session semantics

- **A connection holds one session.** `USE NAMESPACE prod;` is still in force in
  the next statement on that connection. That is what a connection means.
- **Two connections are two sessions** and share nothing but the store.
- **Subscribing consumes the connection.** After a Subscribe frame, the socket
  delivers changes and no longer answers statements. A client that wants both
  opens two connections. A client API that hides this is promising a
  multiplexing the protocol does not perform.
- **The node drops a subscriber that stops reading.** When a client stops
  reading, its socket fills and the node's write blocks; rather than hold a
  thread indefinitely the node ends the connection after **30 seconds**. Nothing
  is lost — the log is the buffer and the client resumes from its stored
  sequence — but **a reconnect path is not optional**, and 3.7's arithmetic is
  what makes reconnection correct.
- A subscriber that is busy is **behind**, never lossy.

### 3.11 Errors a client must keep apart

Ten classes. A client that collapses them into one transport error has thrown
away what the caller needs to act.

| class | meaning | what the caller should do |
|---|---|---|
| Io | the socket failed | retry the transport |
| Encoding | a value could not be decoded | report; do not retry |
| NotThisProtocol | the peer is not a node | fix the address |
| WrongVersion | a node of a version this client does not speak — carries *found* and *supported* | upgrade one side |
| UnknownFrame | a frame kind this build lacks — carries the tag; **connection closes** | upgrade the client |
| TooLarge | a declared length above the ceiling — carries the length; refused before allocating | investigate the peer |
| Truncated | the stream ended mid-frame | retry the transport |
| Malformed | a body is not the shape its header claims | report; do not retry |
| NoWritablePeer | this node takes no writes and knows of no peer that may | **run a `DEFINE REPLICA … ROLES writable`** — not a network problem |
| Refused | the store said no, in its own words | read the message; the statement was wrong, not the connection |

**`NoWritablePeer` is the one most likely to be flattened and the one that must
not be.** Its remedy is a statement nobody ran. Reporting it as a connection
failure sends the operator to the network, where there is nothing to find.

**Retry boundary.** Transport failures may be retried. A statement that reached
the store and failed **there** must not be retried automatically: the client
cannot know it was safe to repeat.

---

## 4. Value encoding

The seventeen types the store carries. This is what `bytes` fields in sections
3.4, 3.5 and 3.8 contain.

Uses the **value-layer primitives** of section 2.2 — note the inverted `i64`.

### 4.1 Type tags

**Permanent.** A tag is never reused for a different type and never renumbered;
data already written carries them.

| tag | type | payload |
|---|---|---|
| `0x01` | none | — (the field is not present) |
| `0x02` | null | — (present, holds nothing) |
| `0x03` | bool | `u8`, 0 = false, anything else = true |
| `0x04` | number | 4.3 |
| `0x05` | string | `lenbytes`, UTF-8 — invalid UTF-8 is an error |
| `0x06` | bytes | `lenbytes` |
| `0x07` | duration | `i64` seconds (**inverted**) · `u32` nanoseconds |
| `0x08` | datetime | `i64` seconds (**inverted**) · `u32` nanoseconds |
| `0x09` | uuid | `fixed(16)` |
| `0x0a` | table | `u32` table id |
| `0x0b` | record | `u32` table id · record id (4.5) |
| `0x0c` | array | `u32` count · that many values, recursively |
| `0x0d` | object | `u32` count · repeated: `lenbytes` field name (UTF-8) · value |
| `0x0e` | range | start bound (4.4) · end bound (4.4) |
| `0x0f` | set | `u32` count · that many values, recursively |
| `0x10` | geometry | 4.6 |
| `0x11` | regex | `lenbytes`, UTF-8 — the pattern as written, uncompiled |

**An unknown type tag is an error, never a guess.** A codec that infers the type
from what follows reads a newer format as a plausible wrong value, and nothing
downstream can tell.

`none` and `null` are different and both exist: *the field is not present* versus
*the field is present and holds nothing*.

**Order in objects and sets.** The node stores an object as a name-ordered map
and a set as an ordered set, so it **re-normalises both on decode**. A client may
therefore send fields and members in any order; the node's stored form is the
same either way, and a client is **not** required to implement a total order
across mixed value types in order to encode one.

A client SHOULD still emit object fields in name order, because that makes two
equal values encode to equal bytes, which is what lets a client compare or cache
encodings of its own. It is a client-side convenience, not a protocol
requirement, and nothing on the node depends on it.

A decoder MUST NOT rely on receiving either in any particular order.

Nanoseconds outside the valid sub-second range are an error, not a wrap.

### 4.2 `varbytes` — escaped, terminated

Used only by record-id text and bytes variants (4.5).

- Encode: for each byte, `0x00` becomes `0x00 0xFF`; every other byte is written
  as-is. Then append the terminator `0x00 0x01`.
- Decode: read bytes until `0x00`; then read the next byte — `0x01` ends the
  component, `0xFF` yields a literal `0x00`, anything else is an invalid escape.
- An end of input before the terminator is an unterminated component.

The escape is byte-local, which is what makes the encoding of a prefix a byte
prefix of the encoding of the whole.

### 4.3 Number

One kind byte, then the payload:

| kind | payload |
|---|---|
| `0x01` integer | `i64`, **inverted** |
| `0x02` float | `fixed(8)` — IEEE-754 double, its **bits** as plain big-endian |
| `0x03` decimal | `fixed(16)` — `i128` mantissa, plain big-endian · `u32` scale (fractional digits) |

An unknown number kind is an error.

Note the asymmetry, and it is deliberate: the **integer** is inverted (it goes
through the sign-flipping `i64` primitive) while the **float bits** and the
**decimal mantissa** are written plain via `fixed`. Three numeric encodings, two
conventions.

An exact decimal is written as its unscaled value and its number of fractional
digits — the two numbers that define it — rather than as an arithmetic library's
in-memory layout. A client implements decimals from those two numbers.

A mantissa and scale that do not describe a representable decimal are an error.

### 4.4 Range bounds

One kind byte per bound:

| kind | payload |
|---|---|
| `0x01` unbounded | — |
| `0x02` included | a value, recursively |
| `0x03` excluded | a value, recursively |

A range holds two bounds, start then end. A bound may hold a value that is itself
a range.

### 4.5 Record id

One discriminant byte, then the payload. **Fixed forever.**

| tag | variant | payload |
|---|---|---|
| `0x01` | integer | `i64`, **inverted** |
| `0x02` | text | `varbytes`, UTF-8 |
| `0x03` | uuid | `fixed(16)` |
| `0x04` | bytes | `varbytes` |

Fixed-width variants carry no terminator, because the discriminant declares the
width. Variable-width ones are terminated per 4.2.

### 4.6 Geometry

Shapes on a sphere. The set is RFC 7946's, so a shape stored here can leave
without translation and one arriving from a client needs no dialect.

One shape-kind byte, then the payload. **Permanent and never renumbered**, for
the reason the value tags are: a shape already written carries this byte.

| kind | shape | payload |
|---|---|---|
| `0x01` | point | one `position` |
| `0x02` | line | `positions` |
| `0x03` | polygon | one `polygon` |
| `0x04` | multipoint | `positions` |
| `0x05` | multiline | `u32` count · that many `positions` |
| `0x06` | multipolygon | `u32` count · that many `polygon` |
| `0x07` | collection | `u32` count · that many **geometries**, recursively (kind byte included) |

The three composites:

```
position   fixed(8) longitude · fixed(8) latitude
           each is the IEEE-754 double's *bits*, plain big-endian — not inverted
positions  u32 count · that many `position`
polygon    `positions` (the exterior ring)
           u32 interior-ring count · that many `positions`
```

**A coordinate is longitude first.** RFC 7946 §3.1.1 fixes it, and the opposite
order is the most common bug in geospatial code precisely because it is silent: a
point in Paris becomes a point in the Indian Ocean, which is a perfectly valid
place. A client that stores latitude first produces shapes that encode, decode,
index and render without complaint, and are wrong.

**Coordinates are bits, not text.** A client that formats a coordinate through a
decimal string and parses it back has changed the value, and the change survives
every round trip it can perform on its own. Read and write the eight bytes.

**Altitude is not carried.** Two dimensions is what an index covers and what every
predicate the store answers needs; a third would be stored, never queried, and
would have to be preserved by every operation that touches a shape.

**Validity is not enforced by the codec.** A decoder reports what the bytes said:
a polygon ring that does not close, a coordinate off the sphere, and a line with
one position all decode. A client MAY check a shape before sending it and SHOULD
say which check it applies; the node applies its own on acceptance, and a client
that refuses locally what the node would accept has invented a second rule.

The consequences of the bit-level treatment are stated rather than left to be
found: `0.0` and `-0.0` are **different** coordinates, and a NaN coordinate is
equal to itself. Neither arises from a measurement; both would otherwise make a
set of shapes misbehave in a way nothing reports.

### 4.7 Regex

A pattern is carried as **text, uncompiled** — the characters as written, in a
`lenbytes` UTF-8 payload.

Nothing in the protocol says which dialect the pattern is in, and a client MUST
NOT compile it to decide whether it is valid: dialects disagree about what is
valid, so a client that validates rejects patterns the node would have accepted
and does it for a whole class of users at once. A pattern that the node cannot
compile comes back as a refusal (3.6) in the store's own words.

### 4.8 Trailing bytes

After decoding one value from a payload, **the buffer must be exhausted**. Bytes
remaining are an error, not something to ignore.

---

## 5. The HTTP surface

**18 callable routes and 13 refusal behaviours.** Authentication is HTTP Basic and
nothing else. A store with no users declared is **open** and runs anything; the
first `DEFINE USER` closes it, and from then on a request without a credential is
answered `401` with a `WWW-Authenticate: Basic realm="TessariDB"` header.

There is no TLS here either.

### 5.1 Routes

`auth: open` means answered without a credential by design. `auth: session` means
the request runs through a session — where **presenting no credential is not
itself a refusal**; on an open store the statement runs, and the refusal, when
there is one, comes from the statement's own permission check.

| method | path | auth | success | media |
|---|---|---|---|---|
| GET | `/health` | open | 200 / 503 | json |
| GET | `/ready` | open | 200 / 503 | json |
| GET | `/metrics` | open | 200 | `text/plain; version=0.0.4` |
| GET | `/backup` | session | 200 | octet-stream |
| GET | `/backup?from=<u64>` | session | 200 | octet-stream |
| POST | `/script` (plain body) | session | 200 | json |
| POST | `/script` (JSON envelope) | session | 200 | json |
| GET | `/watch` | session | 101 (upgrade) | — |
| PUT | `/files/{ns}/{db}/{bucket}/{path…}` | session | 201 | json |
| POST | `/files/{ns}/{db}/{bucket}/{path…}` | session | 201 | json |
| GET | `/files/{ns}/{db}/{bucket}/{path…}` | session | 200 / 404 | octet-stream |
| GET | `/files/{ns}/{db}/{bucket}` | session | 200 | json (bucket listing) |
| HEAD | `/files/{ns}/{db}/{bucket}/{path…}` | session | 200 / 404 | octet-stream |
| HEAD | `/files/{ns}/{db}/{bucket}` | session | 200 | json |
| DELETE | `/files/{ns}/{db}/{bucket}/{path…}` | session | 204 | — (no body) |
| GET | `/` | open | 200 | `text/html` — console, build-conditional |
| GET | `/console.css` | open | 200 | `text/css` — build-conditional |
| GET | `/console.js` | open | 200 | `text/javascript` — build-conditional |

Notes a client implementer needs:

- **`POST /files/…` is a synonym for `PUT`.** A client SHOULD offer `PUT` only; a
  second verb for one action widens the surface for nothing.
- **`HEAD` costs what `GET` costs.** The node reads the whole object and discards
  the body. A client MUST NOT present a `HEAD`-backed `exists()` as a cheap probe.
- **A trailing slash names a file, not a bucket.** `/files/ns/db/bucket/` reads a
  file whose name is `/`; `/files/ns/db/bucket` lists the bucket. A client SHOULD
  normalise a trailing slash away, because "list the bucket" and "read the file
  named `/`" must not be one keystroke apart.
- **The console owns `GET /`**, and in a build without the console the same
  request is a `404`. A client MUST NOT probe `/`; use `/health`, which exists in
  every build.
- **`GET /backup` on a store with no `DEFINE USER` is unauthenticated** and
  returns the whole log. This is the open-store rule at its loudest, not a defect.
  A client's documentation should say so.
- The three operational routes take no credential deliberately: a probe or a
  scraper that needs one is a probe nobody configures.
- Path segments `{ns}`, `{db}`, `{bucket}` must match `[A-Za-z0-9_]+`. They are
  **names**, interpolated into a statement, and the check is what makes that safe.
  The `{path…}` is a **value**, carried as a parameter — a file may be named
  anything, and slashes inside it are part of the name, not directories.
- **The server percent-decodes `{path…}`, and a client MUST therefore encode
  it.** Every byte outside the unreserved set `A-Za-z0-9-._~` is percent-encoded,
  and `/` is left as itself because a slash in a file name reaches the server as a
  slash. This is not optional and it is not cosmetic: an unencoded space makes the
  request **line** unparseable rather than merely wrong, and an unencoded `%` asks
  the server to decode an escape the caller never wrote — so `100% done.txt` is
  sent as `100%25%20done.txt` and stored under the name the caller gave. Encoding
  more than the minimum is safe, since the server decodes what arrives; encoding
  less is not.
- **A bucket is declared with `DEFINE BUCKET` and is not a table.** `PUT`, `GET`
  and `DELETE` against a name that is a table refuse with *"… is not a bucket"*.
  A client does not check this — the catalog is the server's — but a client's
  documentation should say it, because the first attempt otherwise looks like a
  routing fault rather than a missing declaration.
- `POST /script` branches on `Content-Type` containing `application/json`: a JSON
  body is an envelope carrying a script and parameters; any other body **is** the
  script. The shape is decided by what the caller declares, never by sniffing.

### 5.2 Refusals

| trigger | answer |
|---|---|
| wrong method on `/script` `/health` `/ready` `/metrics` `/watch` | 405 json |
| method other than PUT/POST/GET/HEAD/DELETE on `/files/…` | 405 json |
| a path no route claims | 404 json |
| `/backup?` with any query that is not `from=<u64>` | 400 json |
| a `/files/…` segment that is not `[A-Za-z0-9_]+` | 400 json |
| PUT or DELETE on a bucket with no file path | 400 json |
| `/watch` without upgrade headers, or another websocket version | 426 json |
| `/watch` upgrade with no `Sec-WebSocket-Key` | 400 json |
| no credential against a closed store, or one refused | 401 + `WWW-Authenticate` |
| authenticated but not permitted (role, tenancy, grant) | 403 json |
| a script the parser refuses | 400 json |
| a store-level conflict — retriable after a change | 409 json |
| encoding or substrate failure | 500 json |

`401` and `403` are different and a client must keep them apart: `401` means sign
in, `403` means the grants do not cover this and signing in again will never help.

### 5.3 Message framing

Every response carries `Content-Length`. A server implementing this protocol
**MUST** declare the length of every response body, and **MUST NOT** use
`Transfer-Encoding: chunked` on any route. This includes `GET /backup`, the one
route whose body has no small upper bound: the log is materialised and its length
declared, rather than streamed.

A client **MUST** refuse a response whose framing it does not recognise, with a
named error, rather than attempting to read it. Guessing at an unrecognised
framing turns a protocol change into a silently truncated body, which is a wrong
answer that looks like a short one.

**A `HEAD` response carries the `Content-Length` its `GET` would carry, and no
body.** A client **MUST** decide whether to read a body from the **method it
sent**, never from the presence of `Content-Length` in the response. A reader
that trusts the header on a `HEAD` waits for bytes that are never coming — a
permanent hang rather than a slow read, and the failure is indistinguishable from
an unresponsive server.

Two consequences an implementer should plan for rather than discover:

- `GET /backup` returns the whole log in one response, so a client's memory
  ceiling for that route is the size of the log. There is no resumption and no
  range support in this version.
- `HEAD` costs what `GET` costs on the server (section 5.1), and now also costs
  the caller a decision: it saves transfer, not work.

### 5.4 JSON bodies

Where section 5.1 says `json`, the shapes are these. All are objects.

**Every refusal** — every row of section 5.2 answered `json` — carries a single
field:

```json
{"error": "a sentence naming what was refused"}
```

The sentence is meant for a person. It is **not** a stable identifier and a
client **MUST NOT** branch on its text; branch on the status code, which is what
section 5.2 enumerates. The sentence frequently embeds the caller's own input —
a name, a path, a byte range — and is therefore arbitrary text.

**`GET /health` and `GET /ready`** answer one of three shapes, and the field sets
differ:

```json
{"status": "ok",      "committed": 41}
{"status": "unwell",  "committed": 41, "background_errors": 2, "complaint": "…"}
{"status": "leaving"}
```

`ok` is `200`; `unwell` and `leaving` are `503`. A client **MUST** treat `503` on
these two routes as an **answer** rather than a transport failure — the node has
replied to the question it was asked — and **MUST** refuse a `status` it does not
know rather than mapping it onto the nearest one it does.

`leaving` carries **no** `committed` field. A client that models this as one
record with optional fields will offer a caller a commit position that is absent
for a reason it cannot express; three variants with distinct field sets is the
shape that matches.

**The two routes are not synonyms, and a client MUST NOT implement one in terms
of the other.** They answer identically on a well node. They diverge during a
staged shutdown, where `/ready` reports `leaving` while `/health` still reports
`ok` — and that window is the whole reason both exist. A supervisor reads
*not ready* as **stop sending traffic here** and *not healthy* as **restart
this**; a client that reports one for the other inverts an operational decision.

**The object routes** answer these, and the distinctions matter more than the
shapes do:

```json
{"written": true}          // PUT, 201
{"error": "no such file"}  // GET or HEAD, 404
```

- `PUT` answers `201` with a body that carries nothing the status does not. A
  client need not read it.
- `404` on `GET` is an **answer**: the file is not there. It carries a fixed
  sentence with none of the caller's input in it, and it is not the same as a
  file that exists and is empty — that answers `200` with `Content-Length: 0`. A
  client that reported both as "no bytes" would erase a distinction the server
  draws.
- `DELETE` answers `204` with **no body at all**, and answers `204` whether or
  not the file was there. Deletion is therefore **idempotent**, and a client may
  say so; the server reports no difference between removing a file and finding
  none, so a client that claimed to know which had happened would be inventing it.

**Escaping is real and must not be hand-parsed.** These strings carry arbitrary
user input through JSON escaping — a namespace named with a quote arrives as
`\"`. A reader that scans for the text between quotation marks returns a truncated
string and reports success. Use a JSON parser.

### 5.5 `POST /script` — the request

With a `Content-Type` containing `application/json`, the body is an envelope:

```json
{"script": "SELECT * FROM t WHERE n = $n;", "parameters": {"n": "3"}}
```

- `script` is **required**. A body with no `script` answers `400`
  `{"error":"the envelope needs a `script`"}`.
- `parameters` is optional, and every value in it **MUST** be a JSON string. A
  number, boolean, array or object is a `400`.
- Trailing content after the closing brace is a `400`. The body is one object,
  not a stream.

**A parameter's JSON string carries TessariQL source for a value, not the value
itself.** This is the single most important thing on this route and it is
counter-intuitive, because every other parameter API a client author has used
takes a value.

The server reads the string as a TessariQL literal, in isolation, and the
consequences are measurable:

| `parameters` | `$x` becomes |
|---|---|
| `{"x": "3"}` | the **number** 3 — `RETURN $x + 1;` answers `4` |
| `{"x": "'3'"}` | the **string** `3` |
| `{"x": "hello"}` | a `400` — *"`hello` is not a value TessariQL can read on its own"* |
| `{"x": "'a\\'b'"}` | the string `a'b` — TessariQL's own escaping applies inside |

Three obligations follow, and a client that ignores them is wrong in a way its
tests will not show:

1. A client **MUST NOT** pass a caller's string through as a parameter. A
   caller's `"3"` becomes a number and their `"hello"` becomes a `400`; both are
   silent about what went wrong at the API's own boundary.
2. A client that offers a typed parameter API over this route **MUST** render
   each value as TessariQL source and is responsible for quoting and escaping
   it — which is the burden parameters normally exist to remove.
3. A client that cannot do that correctly **SHOULD** use the wire protocol
   (section 3.4), where a parameter is an encoded value and none of this arises,
   or interpolate nothing and send a plain-body script.

Read clause 7 of section 7 with this section beside it. On the wire a parameter
is an encoded value and rendering one to text is forbidden; here there is no slot
for an encoded value, so rendering it is the only way to pass one. The clause
says both, and a client implementing both transports **MUST NOT** let this route's
requirement travel back to the other one.

This is **not** a statement-injection hazard: the string is parsed as a value on
its own, so a parameter cannot add a clause — the fourth row above is a string
containing a quote, not an escape into the statement. It is a **type-confusion**
hazard, which is quieter and reaches production more often.

With any other `Content-Type`, the **whole body is the script** and there are no
parameters. The branch is on what the caller declares, never on sniffing the
body's shape: a script that happens to begin with `{` is a script.

### 5.6 `POST /script` — the answer

`200` with one object:

```json
{"results": [ … ]}
```

`results` holds **one outcome object per statement in the script, in order**, and
is the HTTP rendering of the outcome list section 3.5 specifies for the wire. The
same statements produce the same outcomes on both transports; only the encoding
differs.

**A script is all-or-nothing on this route.** If any statement fails — the first
or the last — the response is a refusal per section 5.2, carrying that
statement's error, and the outcomes of the statements that already succeeded are
**not** reported. `RETURN 1; SELECT * FROM nosuchtable;` answers `400` naming the
second statement; the `1` is nowhere in the response. A client **MUST NOT** offer
a caller partial results from a failed script, because it never receives any.

**All-or-nothing describes the response, not the store.** A script is not
atomic. Statements that ran before the failing one **have taken effect and are
durable**, and the response says nothing about which they were:

```
CREATE u:9 = { n: 9 }; SELECT * FROM nosuchtable;
→ 400 {"error":"no table named \"nosuchtable\" (at 70..81)"}
   and afterwards, u:9 is in the store
```

Two obligations for a client, both of which are easy to get wrong precisely
because the response looks like a clean rejection:

- A client **MUST NOT** describe a failed multi-statement script as *"nothing
  happened"*. It does not know what happened, and neither does its caller.
- A client **MUST NOT** retry a failed script automatically. Re-running it
  re-executes the statements that already succeeded, which turns one create into
  two and one increment into two.

A caller that needs all-or-nothing wraps the statements in `BEGIN` … `COMMIT`.
The response shape does not change — both answer `done` like any other statement
— but the store's behaviour does: a failure inside the transaction rolls the
whole thing back, and the records written before it are **not** in the store
afterwards. That is the difference between the two paragraphs above, and it is
the only way to get it.

Each outcome object carries a `kind`. There are six:

| `kind` | wire tag (3.5) | shape |
|---|---|---|
| `done` | 0 | `{"kind":"done"}` |
| `records` | 1 | `{"kind":"records","path":…,"plan":…,"records":[…]}`, with optional `notes` and `only` |
| `value` | 2 | `{"kind":"value","value":…}`, or `{"kind":"value"}` — see below |
| `keys` | 3 | `{"kind":"keys","keys":["…"]}` |
| `removed` | 4 | `{"kind":"removed","count":2}` |
| `unknown` | 255 | `{"kind":"unknown"}` |

**`{"kind":"value"}` with no `value` key is not the same as
`{"kind":"value","value":null}`.** The first is the language's `none` — nothing
there — and the second is a stored `null`. JSON has one word for both, so the
distinction is carried by **the presence of the key**. A client whose value type
cannot hold both **MUST** say which it collapses them onto; a client that maps
both to its language's `null` has erased a distinction the store maintains, and
`SELECT` over a field that is absent versus one that is explicitly null will read
identically to its caller.

**`unknown` means a kind this client's build has never seen**, and it exists so
that a newer node can add an outcome kind without breaking an older client — the
same forward-compatibility contract section 3.5 states for the wire, where the
outcome's length is what makes it skippable. A client **MUST** expose an
`unknown` to its caller rather than dropping it, and **MUST NOT** stop reading
the list at the first one.

A client **MUST NOT** treat `unknown` as *an outcome carrying nothing*. It is an
outcome this build cannot read, which is a different statement and points at a
different remedy — upgrade, not shrug.

**`records` in detail.** `path` is one word naming how the read reached its
records; `plan` is the same information as a structure, and the two are rendered
from one field so they cannot disagree. `notes` is written **only when non-empty**
and `only` **only when true**, so a response with nothing to report is identical
to what it would have been before either existed. A client **MUST** treat both as
absent-by-default rather than requiring them. `records` is always an array, and
holds at most one element when `only` is true — the key's type does not change.

**Exactness rides inside `plan` on this transport, and it is the one key of a
plan that is always present.** `plan.exact` is a JSON boolean written on every
plan, and `plan.inexact` is a string carrying the reason, written only when
`exact` is `false`:

```json
{"kind":"records","path":"approximate",
 "plan":{"access":"approximate","exact":false,"index":"by_at",
         "inexact":"an approximate index answered this, so a nearer record may exist",
         "table":"points"},
 "records":[…]}
```

Every other key of a plan is written only when the read had an answer for it, so
a scan's plan is the two keys it knows. `exact` deliberately does not follow that
rule: a key present only when interesting teaches a client that its absence means
the dull value, and here the dull value is a claim.

The three states of section 3.5 survive on this transport with a different shape.
`plan.exact` present and `true` is the first, `false` with `inexact` the second,
and **`plan` carrying no `exact` key at all** is the third — a node that predates
the field. A client **MUST NOT** read the third as the first.

**An element of `records` is a pair, not a value.**

```json
{"id": "users:1", "value": {"name": "ada"}}
```

`id` is the record's identity written as §5.7 writes a `record`, and `value` is
the record itself. A client that types this array as *the records* has the wrong
shape, and will read `name` one level too high.

**`path`** is one of eight words. The set is closed and a client may switch on it
exhaustively:

| word | the read |
|---|---|
| `record` | reached by identity |
| `index` | an index answered it |
| `ordered` | an ordered index answered it, in its order |
| `scan` | every record was read |
| `approximate` | an approximate index answered it — see the note of the same name |
| `graph` | adjacency answered it |
| `join` | more than one source was combined |
| `materialised` | a materialised source answered it |

**`plan`** is an object. `access` is always present and always equal to `path`.
Every other key is **absent when the read had no answer for it**, rather than
present holding null — so a scan's plan is the two keys it knows, not eight of
which six say nothing:

| key | type | meaning |
|---|---|---|
| `access` | string | the `path` word; **always present** |
| `source` | string | what the records came from |
| `table` | string | the table read |
| `index` | string | the index used |
| `shape` | string | the shape of the bound the index served |
| `columns` | integer | columns considered |
| `cells` | integer | cells read |
| `at_most` | integer | the ceiling the read was given |

`source` and `shape` are **open word sets**: a client **MUST** render an
unrecognised one rather than switching on it exhaustively, and **MUST NOT** treat
one it does not know as an error. `path` is the closed set above; these two are
not, and the difference matters because they look alike in the same object.

**A note** is an object with two keys:

```json
{"kind": "fell-back", "message": "the index path could not fill the bound, so the read took the scan path instead"}
```

`kind` is a stable word a client may group on; `message` is a sentence for a
person and **MUST NOT** be branched on — the same rule the refusal body follows
in 5.4. Five kinds exist: `fell-back`, `approximate`, `compared-across-kinds`,
`cursor-walked`, `subquery-ceiling`. A client **MUST** carry an unrecognised kind
through to its caller rather than dropping it, because a note it does not know is
still the store reporting that the answer is qualified.

**`keys`** holds record identities as strings. It is an array of strings and
never of objects; unlike `records`, there is no value beside the identity, which
is the point of the outcome.

**Each string is the id half alone — there is no `table:` prefix and no colon.**
`KEYS FROM notes` over a store holding two records answers
`{"kind":"keys","keys":["42","42"]}`, not `["notes:42", …]`. The statement named
the table, so the answer does not repeat it. This sentence previously said the
keys were written *"the way §5.7 writes a `record`"*, which is the `table:id`
form of §5.7.1 — a client implementing that literally splits on a colon that is
never there.

Within the id half the spelling and its consequences are exactly §5.7.1's: the
integer `42` and the text `'42'` are **written identically**, and a client
**MUST NOT** parse the string back into a typed record id. A caller that needs
the type reads the identity from section 4, where it carries its tag.

### 5.7 `POST /script` — how a value is spelled

JSON has six types and this store has seventeen, so this table is a decision
rather than a translation, and a client author needs all of it. The type of a
value is **not** recoverable from the JSON alone for the rows marked *lossy*: a
caller who needs the type reads it from the field's declared kind in the catalog,
or uses the wire protocol (section 4), where every value carries its tag.

| value | JSON | lossy | note |
|---|---|---|---|
| `none` | *the key is absent* | — | see 5.6; absence is the encoding |
| `null` | `null` | — | |
| `bool` | `true` / `false` | — | |
| integer | number | — | exact to 2⁵³; larger integers are exact in the document but not in a parser that reads every number as a double |
| decimal | **string** | yes | `"12.34"` — a JSON number is a double in every parser that matters, and `dec` exists to keep an amount of money from being one |
| float | number | yes | except non-finite: `"inf"`, `"-inf"`, `"NaN"` are **quoted**, because JSON has no spelling for them and an unquoted one produces a document most parsers reject. Lossy because a float with no fractional part writes as an integer — see below |
| `string` | string | — | |
| `bytes` | string | yes | lowercase hex, no prefix, no separators |
| `duration` | string | yes | the literal this language writes, e.g. `"1h30m"` |
| `datetime` | string | yes | RFC 3339 |
| `uuid` | string | yes | hyphenated lowercase hex |
| table | string | yes | the table's name; a table whose name the answer cannot resolve is written `"<table 7>"`, with the brackets, so it is visibly not a name |
| record | string | yes | `"table:id"`; unresolvable is `"<record …>"` |
| `array` | array | — | elements recurse through this table |
| `set` | **array** | yes | a set arrives as an array; the collection type is not recoverable |
| `object` | object | — | a field holding `none` **is omitted**, per 5.6 |
| `range` | object | — | see below |
| `geometry` | object | — | GeoJSON, RFC 7946 |
| `regex` | string | yes | the pattern's source. The server does not execute it; a client **MUST NOT** compile it with its own engine and present the result as this store's semantics |

**A range** is an object, because there is no text form to render it back from:

```json
{"start": {"bound": "included", "value": 1},
 "end":   {"bound": "excluded", "value": 5}}
```

`bound` is `included`, `excluded` or `unbounded`. An **unbounded end carries no
`value` key at all** — the same rule `none` uses in 5.6 — which is what keeps an
open end distinct from an end holding `null`. Each endpoint is written by this
same table, so a decimal endpoint is quoted and a datetime endpoint is RFC 3339.

A client **MUST NOT** model a range as a pair of endpoints without their bound
kinds: `1..5`, `1..=5` and `1..` are three different spans, and a two-field model
collapses them onto one.

**Geometry** is GeoJSON. The one rule worth restating at the boundary:
coordinates are `[longitude, latitude]`. A non-finite coordinate is written
`null`, which cannot arise from a well-formed shape and can arise from bytes —
the surface reports what it holds rather than inventing a number.

**Two ambiguities are inherent and are stated rather than hidden.** A quoted
decimal is indistinguishable from a string that looks like a number, and a range
object is indistinguishable from a stored object carrying `start` and `end` keys.
This surface is lossy by construction; a client that needs types uses section 4.

#### 5.7.1 The spellings the table above is too small to hold

A one-line cell says which JSON type a value takes and cannot say how the value
is written inside it. Five rows need that second half, and a client that guesses
it guesses wrong in ways nothing reports.

**A float is written positionally and drops an empty fraction.** `1.5` is `1.5`,
but `2.0` is `2` and `1e10` is `10000000000` — there is no exponent form and no
trailing `.0`, so a float that happens to hold a whole number is **not
distinguishable from an integer**, which is why the row is marked lossy. The
consequence at the far end of the range is worth planning for rather than
discovering: the largest finite double writes as **309 digits**, and the smallest
subnormal as a `0.` followed by 323 zeros and a `5`. A client that reads numbers
into a fixed-width buffer, or that assumes a JSON number is short, meets this on
a value the store considers ordinary.

**There is no negative zero to write.** It is not a rendering choice: a float is
normalised when the value is built, so `-0.0` and `0.0` are one value and it
writes as `0`. A client MUST NOT expect the sign of a zero to survive, here or
on the wire.

**A datetime carries up to nine fractional digits, and no more than it has.**
Trailing zeros are trimmed, so `…:56.100Z` writes as `…:56.1Z` and a whole second
carries no fraction at all. The instant is **always UTC and always ends in `Z`**:
an offset supplied in the input is applied and then discarded, so
`2026-09-01T12:34:56+03:00` comes back as `2026-09-01T09:34:56Z`. **The original
offset is not recoverable** — this type is a point in time and carries no zone,
and a client that needs the caller's zone stores it in a field of its own.

**A duration is a decomposition, not the text the caller wrote.** It is written
largest unit first over exactly six units — `h`, `m`, `s`, `ms`, `us`, `ns` — and
a unit whose count is zero is omitted entirely; a zero span is `0s`, because an
empty string is not a literal. So `1500ms` comes back as `1s500ms` and `1h30m`
comes back unchanged. **The hour is the largest unit written**, even though the
language reads a day and a week on input: `1d` comes back as `24h` and `1w` as
`168h`. A client that renders a duration for a person, or that compares one
against text it wrote itself, cannot assume it gets its own units back. A
span below zero is written as a leading `-` followed by the magnitude in that
same decomposition: −0.5 s is `-500ms`, and the smallest span below zero is
`-1ns`. **The text a span writes reads back as that span**, in this language's
own duration syntax, in both directions.

**A record id is written plainly, and is NOT a TessariQL literal.** The string is
`table:id`, and the id half is the id's own text with no quoting and no escaping:

| id | as a value elsewhere | inside `table:id` |
|---|---|---|
| integer `1` | `1` | `users:1` |
| text `'ada'` | `"ada"` | `users:ada` |
| text `'has space'` | `"has space"` | `users:has space` |
| text `'7'` | `"7"` | `users:7` |
| uuid | `"0195e0a1-1111-7000-8000-000000000000"` | `sessions:0195e0a1111170008000000000000000` |
| bytes `0x0a1b` | `"0a1b"` | `files:0x0a1b` |

Two of those rows disagree with the value table above **on purpose and by
consequence**: a uuid id loses its hyphens and a bytes id gains a `0x`, because
the id half is written in the identity syntax rather than the value syntax. A
client that reuses its uuid parser on the id half will not find hyphens where it
expects them.

The rest of the table is the warning. `users:7` is the integer `7` and the text
`'7'` written identically; `users:a:b` cannot be split on its colon; and a text
id containing a quote arrives unescaped. **A client MUST NOT parse this string
back into a typed record id.** It is an identifier to display, log and pass
back — and a caller that needs the id's type reads it from section 4, where it
carries its tag. This is the same bargain the lossy column names, stated once
more because a record id is the value most likely to be round-tripped by a
client author who did not notice it was lossy.

**A record whose table the answer cannot name** is written `<record T:id>`, with
the brackets, where `T` is the table's numeric id — for example `<record 7:1>`.
This is the record-level form of the `"<table 7>"` rule above, and it arises
where a table has been dropped while a value still refers to it. The brackets are
what make it visibly not a name: `<` cannot begin a table name, so a client can
detect the form rather than mistaking it for a table called `<record 7`.

**A set's array order is defined, but only half of it is published.** A set is
written in the node's own ascending order with duplicates already removed, so
`set [3, 1, 2, 1]` is `[1,2,3]`. Within one type that order is the obvious one.
**Across types it is deliberately not stated here**, because section 4 goes out
of its way to say a client is *not* required to implement a total order over
mixed values in order to encode one, and publishing the node's rank would quietly
make that a requirement after all. So a client **MUST NOT** rely on a set
preserving insertion order — it has none to preserve — and **MUST NOT** depend on
where a number sorts relative to a string. It **MAY** rely on the order being
deterministic: two equal sets write the same array, which is what lets an answer
be compared without being sorted first.

---

## 6. What is deliberately **not** part of this protocol

Named so that absence reads as a decision rather than an omission, and so that no
client author reimplements something no client ever sees.

- **The storage key grammar** — record keys, secondary and unique index keys,
  postings, vector nodes, edges, search statistics. Roughly 2 800 lines of the
  server's encoding crate. A client never constructs or reads a storage key.
- **The key-kind tag space** (`0x01`…`0x38`) and the keyspaces. Unrelated to the
  value tags in 4.1, which live in their own space.
- **The log format, compaction, and the catalog's on-disk layout.**
- **Index selection and query planning.** The access-path byte in 3.5 reports
  what happened; it is not a control.
- **Schema validation.** The catalog is on the server. A client that checks that a
  table exists, a field is indexed, or types match is making a claim that fails in
  production rather than in a test.
- **A second query language.** Statements are TessariQL. A query builder produces the
  grammar the server's own parser accepts, and that is proven by round-tripping
  built queries through that parser — not by inspection.

  That proof lives in the server's workspace, which no client may read, so a
  builder's *rendering* is specified separately in
  [`query-builder-v1.md`](query-builder-v1.md) and checked by
  `conformance/queries-v1.json`. That document is language rather than protocol
  and is excluded from this one for the reason this section exists; it is named
  here so the exclusion does not read as an absence.

---

## 7. Conformance

A conforming client:

1. **MUST** send and verify the greeting before any frame, and refuse a **major**
   it does not implement at that point — and **MUST NOT** refuse on a differing
   minor.
2. **MUST** refuse a declared frame length above 16 MiB **before allocating**,
   and **MUST NOT** send one.
3. **MUST** close the connection on an unknown frame kind rather than skipping it.
4. **MUST** decode an unrecognised outcome tag as `Unknown`, **skip its remaining
   bytes using the outcome's length, and continue with the next outcome** —
   never stopping at it, erroring the whole answer, or dropping it silently. A
   recognised outcome **MUST NOT** be read past its length either.
5. **MUST** reject an unknown value type tag, an unknown number kind, an unknown
   bound kind, and an unknown record-id discriminant — never guess.
6. **MUST** apply the `i64` sign inversion (2.2) in the value layer and **MUST
   NOT** apply it in the frame layer.
7. **MUST** carry parameter values through the value codec on the wire protocol
   (3.4), and **MUST NOT** interpolate them into script text there. `POST /script`
   has no such channel: its parameters are JSON strings holding TessariQL source
   (5.5), so a client offering a typed parameter API over that route renders each
   value to source itself and owns the quoting. That is a property of the route,
   not a licence — a client implementing both **MUST NOT** carry the
   source-rendering habit back to the wire, where binding after parsing is the
   whole point.
8. **MUST** keep the ten error classes of 3.11 distinguishable to its caller, in
   particular `NoWritablePeer` and the `401` / `403` pair.
9. **MUST** treat `Subscribe`'s `from` as inclusive and own the `+1` arithmetic.
10. **MUST** provide a reconnect path for subscriptions, given the node's
    30-second drop of a non-reading subscriber.
11. **MUST NOT** retry a statement that reached the store and failed there.
12. **SHOULD** emit object fields in name order (4.1) — a client-side
    convenience, not a requirement the node depends on.
13. **MUST** treat trailing bytes after a decoded value as an error.
14. **MUST** write a geometry's coordinates **longitude first**, as IEEE-754
    bits, and **MUST NOT** round-trip them through a decimal string.
15. **MUST NOT** compile or validate a regex pattern before sending it.
16. **MUST NOT** depend on the server's repository, in any language.

Verification is against a **running node**, not a mock: a mock proves the client
agrees with its author's belief about the protocol, which is the belief most
likely to be wrong.

Where a public repository cannot run a private node, the **shared conformance
corpus** in `conformance/` is the substitute — weaker, and labelled as such. Its
one real property is that it is generated by an implementation written from this
document alone, so it disagrees when the document is unclear rather than when a
client is.

A client **MUST** run the corpus in **both directions**: decode each vector's
bytes to the stated value, *and* encode the stated value back to the same bytes.
One direction is not enough, and this is not a precaution — it is measured. A
codec that is consistently wrong round-trips perfectly: mutating both sides of a
client's `i64` handling to plain big-endian left thirteen of fourteen round-trip
tests passing, and only the byte-level comparison caught it.

---

## 8. Open

- **The codec is not frozen.** Version 1.0 is current, and 2.3's policy says the
  format may still change without the version moving until the first release.
  Whether the value encoding is revised before the clients are published is an
  open product decision.
- **Geometry and regex have no literal in the query language.** Both are storable
  and readable as parameters and results, and neither can be written into a
  script by hand. That is a language question rather than a protocol one, and it
  does not affect a client — but a client author who tries to build one into a
  statement will find out the hard way, so it is stated here.
- **`Content-Length` on every response constrains `GET /backup`.** Section 5.3
  requires a declared length on every route, and the node satisfies it by
  materialising the whole log — measured at a small store and again at a
  non-trivial one. That is a real ceiling: a store whose log outgrows a
  comfortable response would need either streaming, which this version forbids,
  or a range facility, which it does not have. Whether `/backup` gains one is an
  open product decision, and it is a **version** decision rather than a quiet
  one, which is why clients are required to refuse unrecognised framing loudly.

- **The bucket listing has no normative shape, and a client should not depend on
  its current one.** `GET /files/{ns}/{db}/{bucket}` answers `200`, and what it
  answers today is a statement result wrapped in a key — it carries the query
  plan that produced it and a storage-level chunk count, neither of which a client
  has any business reading. Section 5.4 deliberately does not write that down: a
  shape specified here binds every client in every language, and this one would
  bind them to an internal detail. The same route also answers `200` for a name
  that is a **table** rather than a bucket, where `PUT`, `GET` and `DELETE` all
  refuse — one route out of four disagreeing about what a bucket is. Both are open
  against the server. A client may list a bucket once the shape is settled; until
  then it is the one route on this surface with no contract.
- **There is no geospatial predicate yet.** A geometry is a value the store
  carries; `INSIDE`, `INTERSECTS` and distance are not part of this version, and
  the access-path byte will gain no new value for them until they exist.
