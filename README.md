# bgv-db protocol

The wire and HTTP protocols a bgv-db node speaks, specified so that a client can
be written in any language **without reading the server's source**.

Licence: **Apache-2.0**. The specification and the conformance corpus are
permissively licensed on purpose — a protocol nobody may implement freely is not
a protocol, it is an API.

---

## Why this repository exists separately

A client library is written *against* a protocol. If the only description of that
protocol is one implementation, then every other language's client is written by
reading that implementation — and everything it never wrote down is discovered
independently, and encoded slightly differently, by each of them. The document
cannot be wrong, because there is no document; the clients disagree instead, and
the disagreement reaches a user as a bug nobody can locate.

So the specification comes first and lives on its own:

- it belongs to no language, so it sits in no language's repository;
- **a behaviour a client needs and this document does not state is a defect in
  this document** — fixed here first, then in the client;
- a client that must consult the server's source to answer a question has found a
  gap, and the gap is filed here.

## What is here

| path | what it is |
|---|---|
| `spec/protocol-v1.md` | the normative specification of protocol version 1.0 |
| `conformance/` | executable test vectors every client is checked against |
| `conformance/README.md` | how to run the corpus against a client |

## The version, and what it promises

The greeting carries **two numbers**. A differing **major** is refused at the
greeting; a differing **minor** is not — each side simply learns the other's and
declines to send what the older one cannot read.

The skip is what makes that promise real: every outcome carries its own length,
so a client that meets an outcome kind it has never seen steps over it and
carries on. That is why **a new outcome kind is `minor` and a new value type is
`major`** — a value nested inside an array carries no length of its own, so an
unknown tag there ends the parse and there is nowhere to resume from.

**Why the first version is 1.0 and not 4.** During development a single version
byte moved twice, correctly by its own rule and pointlessly by purpose: a version
exists to refuse a mismatch between builds that are *deployed*, and nothing was.
Before the first release the format changes freely and the version stands still.

The value encoding is **not frozen**.

## The one detail that catches every new client

The protocol has **two primitive sets**, and they disagree about signed integers.
The frame layer writes plain big-endian. The value layer writes an `i64`
big-endian **with the top bit of the first byte inverted**.

A client that writes plain big-endian in the value layer gets every integer,
duration, datetime and integer record id wrong — and a round-trip test will not
notice, because its encoder and decoder are wrong in the same way. The corpus
pins the bytes for exactly this reason.

See `spec/protocol-v1.md` §2.2.

## Status

**Draft, authoritative.** The corpus is generated from the specification alone by
a second, independent implementation — which is what makes it evidence that the
document is sufficient, rather than a dump of one client's beliefs.

The document is **upstream of the implementations**: a layout is decided here,
then built in the node, then in the clients. So the specification may briefly
describe something the node does not yet do, and where it does, the entry below
says so.

Still owed: verification of every vector against a **running node**. Until that
lands, the corpus proves that two independent readings of this document agree,
which is strong but is not the same claim.

## Contributing a client

1. Read `spec/protocol-v1.md`. Do not read the server.
2. Implement the value codec and the frame codec.
3. Run `conformance/` against your implementation. Every vector, both directions.
4. Where the document did not answer a question you had, open an issue here. That
   is the contribution that matters most — it is how the document stops being
   sufficient only for the person who wrote it.
