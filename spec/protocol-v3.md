# bgv-db protocol — specification for client implementers

**Protocol version 3.** Drafted 2026-08-24.

Status: **draft, authoritative.** This document is the source of truth for every
client, in every language. A client is written from this document, not from the
server's source. A behaviour a client needs and this document does not state is a
**defect in this document** — it is fixed here first, then in the client.

Licence of the clients: **Apache-2.0**. The server is licensed separately —
its licence is a distinct decision from the clients', and the two are not
described as sharing one.

**Version 3 is current, not final.** The protocol carries a version byte that
moves whenever a body layout changes — it went to 2 when a records answer began
carrying table names, and to 3 when a request began carrying bound parameter
values. This document specifies version 3. A client pins the version it
implements and refuses others at the greeting, which is where the protocol
already refuses them.

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
six types; the store's value model has **fifteen**. An HTTP-only client would
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

---

## 3. The wire protocol

### 3.1 Connection and greeting

TCP. On connect, **both sides send** the greeting before anything else:

```
"BGVW"   4 bytes, ASCII, literally 0x42 0x47 0x56 0x57
version  1 byte, currently 3
```

A client that reads something other than `BGVW` reports *not this protocol* and
closes. A client that reads a version it does not implement reports *wrong
version*, carrying both the version found and the version supported, and closes.

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

Each outcome begins with **one tag byte**:

| tag | outcome | rest of the outcome |
|---|---|---|
| 0 | Done | nothing |
| 1 | Records | `u8` access path · names (3.9) · `u32` record count · repeated: `text` identity, `bytes` value (§4) |
| 2 | Value | names (3.9) · `bytes` value (§4) |
| 3 | Keys | `u32` count · repeated `text` |
| 4 | Removed | `u64` count of records a conditional delete removed |
| 255 | Unknown | nothing |

**Any tag a client does not recognise decodes as `Unknown`.** A client MUST
expose it to its caller rather than dropping the outcome or erroring the whole
answer: a newer node may answer with something this client has never seen, and
saying so is honest where guessing at its content is not.

> **Known limitation — an unknown outcome cannot be skipped.** An outcome carries
> no length of its own, so there is no way to find where an unrecognised one ends
> and the next begins. A client MUST therefore stop reading outcomes at the first
> `Unknown` and return what it has, with the `Unknown` last; continuing would
> decode that outcome's payload as the next outcome's tag and produce plausible
> garbage.
>
> The consequence is that **forward compatibility across outcome kinds holds only
> while an unknown outcome is the last in its answer.** A node that introduces a
> new outcome kind in a non-final position breaks older clients in a way they
> cannot detect. Giving each outcome a length prefix would remove the limit
> entirely and is the obvious candidate for a version 4.

The **access path** byte in a Records outcome names how the store found them:

| byte | name |
|---|---|
| 0 | `record` |
| 1 | `index` |
| 2 | `scan` |
| 3 | `ordered` |
| any other | `scan` |

An unrecognised path reads as `scan`, which is the honest answer for a path this
build has no name for: it is the one path that promises nothing.

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

## 4. Value encoding, version 3

The fifteen types the store carries. This is what `bytes` fields in sections 3.4,
3.5 and 3.8 contain.

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

### 4.6 Trailing bytes

After decoding one value from a payload, **the buffer must be exhausted**. Bytes
remaining are an error, not something to ignore.

---

## 5. The HTTP surface

**18 callable routes and 13 refusal behaviours.** Authentication is HTTP Basic and
nothing else. A store with no users declared is **open** and runs anything; the
first `DEFINE USER` closes it, and from then on a request without a credential is
answered `401` with a `WWW-Authenticate: Basic realm="bgv-db"` header.

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
| DELETE | `/files/{ns}/{db}/{bucket}/{path…}` | session | 204 | json |
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
- **A second query language.** Statements are bgvQL. A query builder produces the
  grammar the server's own parser accepts, and that is proven by round-tripping
  built queries through that parser — not by inspection.

---

## 7. Conformance

A conforming client:

1. **MUST** send and verify the greeting before any frame, and refuse a version
   it does not implement at that point.
2. **MUST** refuse a declared frame length above 16 MiB **before allocating**,
   and **MUST NOT** send one.
3. **MUST** close the connection on an unknown frame kind rather than skipping it.
4. **MUST** decode an unrecognised outcome tag as `Unknown` and expose it, rather
   than erroring the whole answer or dropping the outcome.
5. **MUST** reject an unknown value type tag, an unknown number kind, an unknown
   bound kind, and an unknown record-id discriminant — never guess.
6. **MUST** apply the `i64` sign inversion (2.2) in the value layer and **MUST
   NOT** apply it in the frame layer.
7. **MUST** carry parameter values through the value codec and **MUST NOT**
   interpolate them into script text.
8. **MUST** keep the ten error classes of 3.11 distinguishable to its caller, in
   particular `NoWritablePeer` and the `401` / `403` pair.
9. **MUST** treat `Subscribe`'s `from` as inclusive and own the `+1` arithmetic.
10. **MUST** provide a reconnect path for subscriptions, given the node's
    30-second drop of a non-reading subscriber.
11. **MUST NOT** retry a statement that reached the store and failed there.
12. **MUST** sort object fields by name when encoding.
13. **MUST** treat trailing bytes after a decoded value as an error.
14. **MUST NOT** depend on the server's repository, in any language.

Verification is against a **running node**, not a mock: a mock proves the client
agrees with its author's belief about the protocol, which is the belief most
likely to be wrong. Where a public repository cannot run a private node, a shared
conformance corpus of encoded vectors and expected decodings is the weaker
substitute and must be labelled as such.

---

## 8. Open

- **The codec is not frozen.** Version 3 is current. Whether the value encoding
  is revised before the clients are published is an open product decision.
- **This document's public home** is undecided. A multi-language specification
  does not belong in any one language's client repository.
- **A shared conformance corpus** does not exist yet; §7 depends on it.
