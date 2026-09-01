"""The §3 wire protocol, as much of it as a conformance harness needs.

Its own module because two harnesses use it: `verify_values_against_node.py`
speaks it to check the byte corpus, and `verify_json_against_node.py` speaks it
to reach values that have no TessariQL literal — a value a script cannot write
can still be bound as a parameter, because a parameter travels in the value
codec rather than as source (§3.4).

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import socket
import struct

MAGIC = b"TESS"
MAJOR, MINOR = 1, 0

FRAME_REQUEST = 1
FRAME_ANSWER = 2
FRAME_REFUSAL = 3

OUTCOME_DONE = 0
OUTCOME_RECORDS = 1
OUTCOME_VALUE = 2
OUTCOME_KEYS = 3
OUTCOME_REMOVED = 4

CEILING = 16 * 1024 * 1024


class Refused(Exception):
    """The store said no, in its own words (§3.6)."""


class Malformed(Exception):
    """A body is not the shape its header claims (§3.11)."""


# --- §2.1 frame-layer primitives ----------------------------------------------


def u32(n: int) -> bytes:
    return struct.pack(">I", n)


def text(s: str) -> bytes:
    raw = s.encode("utf-8")
    return u32(len(raw)) + raw


class Reader:
    """A cursor over one body. Every read is bounded by the body's own length."""

    def __init__(self, raw: bytes) -> None:
        self.raw, self.at = raw, 0

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.raw):
            raise Malformed(f"wanted {n} bytes at offset {self.at}, body holds {len(self.raw)}")
        chunk = self.raw[self.at : self.at + n]
        self.at += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]

    def text(self) -> str:
        return self.take(self.u32()).decode("utf-8")

    def lenbytes(self) -> bytes:
        return self.take(self.u32())


# --- §3 the connection --------------------------------------------------------


class Node:
    """One connection, which is one session (§3.10)."""

    def __init__(self, host: str, port: int, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.sendall(MAGIC + bytes([MAJOR, MINOR]))
        greeting = self.exactly(6)
        if greeting[:4] != MAGIC:
            raise SystemExit(f"not this protocol: peer opened with {greeting[:4]!r}, not {MAGIC!r}")
        self.major, self.minor = greeting[4], greeting[5]
        if self.major != MAJOR:
            raise SystemExit(f"wrong version: node speaks major {self.major}, this speaks {MAJOR}")

    def exactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise Malformed(f"stream ended after {len(out)} of {n} bytes")
            out += chunk
        return out

    def send_frame(self, kind: int, body: bytes) -> None:
        if len(body) > CEILING:
            raise Malformed(f"body of {len(body)} bytes exceeds the 16 MiB ceiling")
        self.sock.sendall(bytes([kind]) + u32(len(body)) + body)

    def read_frame(self) -> tuple[int, bytes]:
        header = self.exactly(5)
        kind, length = header[0], struct.unpack(">I", header[1:])[0]
        if length > CEILING:
            raise Malformed(f"declared length {length} exceeds the 16 MiB ceiling")
        return kind, self.exactly(length)

    def request(self, script: str, params: list[tuple[str, bytes]] | None = None) -> list[dict]:
        """§3.4 out, §3.5 back. Returns one dict per outcome, in order."""
        params = params or []
        body = text(script) + b"\x00" + u32(len(params))
        for name, encoded in params:
            body += text(name) + u32(len(encoded)) + encoded
        self.send_frame(FRAME_REQUEST, body)

        kind, answer = self.read_frame()
        if kind == FRAME_REFUSAL:
            # §3.6: the body is the store's own message, whole, unprefixed.
            raise Refused(answer.decode("utf-8", "replace"))
        if kind != FRAME_ANSWER:
            raise Malformed(f"expected an Answer frame, got kind {kind}")
        return self.read_outcomes(Reader(answer))

    @staticmethod
    def read_outcomes(body: Reader) -> list[dict]:
        outcomes = []
        for _ in range(body.u32()):
            length = body.u32()
            inner = Reader(body.take(length))  # the length is a bound, not just a cursor
            outcomes.append(Node.read_outcome(inner))
        return outcomes

    @staticmethod
    def read_outcome(inner: Reader) -> dict:
        tag = inner.u8()
        if tag == OUTCOME_DONE:
            return {"kind": "done"}
        if tag == OUTCOME_VALUE:
            names = Node.read_names(inner)
            return {"kind": "value", "names": names, "bytes": inner.lenbytes()}
        if tag == OUTCOME_KEYS:
            return {"kind": "keys", "keys": [inner.text() for _ in range(inner.u32())]}
        if tag == OUTCOME_REMOVED:
            return {"kind": "removed", "count": inner.u64()}
        if tag == OUTCOME_RECORDS:
            inner.u8()  # access path
            names = Node.read_names(inner)
            records = [(inner.text(), inner.lenbytes()) for _ in range(inner.u32())]
            return {"kind": "records", "names": names, "records": records}
        # §3.5: an unrecognised tag is reported, its bytes stepped over, and the
        # read carries on. That is the whole point of the per-outcome length.
        return {"kind": "unknown", "tag": tag}

    @staticmethod
    def read_names(inner: Reader) -> dict[int, str]:
        return {inner.u32(): inner.text() for _ in range(inner.u32())}

    def close(self) -> None:
        self.sock.close()


def prepare(node: Node) -> None:
    """Select a namespace and a database, once.

    One connection is one session (§3.10), so unlike the HTTP harness this is a
    single call rather than a prelude on every script. Each `DEFINE` tolerates
    its own refusal so the harness may be re-run against a node that is still
    up — a `--serve` process outlives one run of this file.
    """
    for statement in (
        "DEFINE NAMESPACE conformance;",
        "USE NAMESPACE conformance;",
        "DEFINE DATABASE conformance;",
        "USE DATABASE conformance;",
    ):
        try:
            node.request(statement)
        except Refused as why:
            if "already in use" not in str(why):
                raise
