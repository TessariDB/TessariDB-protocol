#!/usr/bin/env python3
"""Execute the query corpus against a running node — the check that reaches the parser.

`generate_queries.py --check` proves the builder renders the text `queries-v1.json`
states. It cannot prove that text PARSES. §6 point 4 of the builder contract says
so itself, and the corpus repeats it in its own header:

    Rendering agreement is the offline half. Section 6 point 4 of the contract
    asks a client that can reach a node to execute every rendered case with its
    parameters bound: that is the only check that reaches the parser.

This is the third harness against a node and the first that sends STATEMENTS.
`verify_json_against_node.py` and `verify_values_against_node.py` both send
values; a value that renders is a value the node held, whereas a statement that
renders is only a string until something tries to run it.

Four cases put a bound parameter in the IDENTITY position — `CREATE memories:$p0
= { … }`, `UPDATE memories:$p0 SET …`, `DELETE memories:$p0;`. Whether a node
accepts a parameter as a record id is a parser fact, and no rendering test can
reach it.

WHY THE WIRE AND NOT HTTP
-------------------------
Over `POST /script` a parameter carries TessariQL **source** (§5.5), and what
that source is for a typed value is undecided — `Q-PROTO-4`, blocked on Q-339.
Running these cases over HTTP would mean inventing the mapping this repository
has deliberately refused to invent. Over §3.4 a parameter travels in the value
codec, so `generate.encode` supplies the bytes and the question does not arise.

THE EIGHT CASES WITH NO SCRIPT
------------------------------
Eight of the 27 are cases the BUILDER must refuse, so there is nothing to send
and a node run cannot speak to them. They get their own verdict, counted apart —
a run reporting 19 of 27 as the whole corpus would be reporting coverage it does
not have.

But five of those refusals make a claim ABOUT the node: that the naive rendering
would mean something else. `SELECT * FROM "memories; DROP COLLECTION memories;
--"` is refused rather than quoted for exactly that reason. That claim is
testable, and it is the one thing a node can settle about these cases. So each is
also sent the way a client that did NOT refuse would have written it — hand-
rolled below, because no conforming builder will produce it. `NODE-ACCEPTS` there
is not a defect: it is the measurement that shows the refusal is load-bearing.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate import encode  # a second implementation of the codec
from wire import Malformed, Node, Refused

# --- the naive renderings ------------------------------------------------------
#
# A conforming builder refuses to produce any of these, which is the whole point
# of the eight cases; they are written by hand so the node can be asked what the
# refusal is protecting against. Keyed by case name, and every script-less case
# must appear here or its inversion reports `no-naive-rendering` rather than
# quietly not happening.

NAIVE: dict[str, tuple[str, dict]] = {
    "a-table-that-is-not-a-name-is-refused": (
        "SELECT * FROM memories; DROP COLLECTION memories; --;",
        {},
    ),
    "a-field-that-is-not-a-name-is-refused": ("SELECT body-text FROM memories;", {}),
    "a-filter-field-that-is-not-a-name-is-refused": (
        "SELECT * FROM memories WHERE 1st = $p0;",
        {"p0": {"integer": "1"}},
    ),
    "an-ordering-field-that-is-not-a-name-is-refused": (
        "SELECT * FROM memories ORDER BY created at ASC;",
        {},
    ),
    "a-delete-of-a-table-that-is-not-a-name-is-refused": (
        "DELETE 1memories:$p0;",
        {"p0": {"string": "x"}},
    ),
    "a-create-with-no-fields-is-refused": (
        "CREATE memories:$p0 = {  };",
        {"p0": {"string": "x"}},
    ),
    "a-create-in-a-table-with-no-fields-is-refused": ("CREATE memories = {  };", {}),
    "an-update-with-no-fields-is-refused": (
        "UPDATE memories:$p0 SET ;",
        {"p0": {"string": "x"}},
    ),
}


# --- the run ------------------------------------------------------------------


def bind(parameters: dict) -> list[tuple[str, bytes]]:
    return [(name, encode(value)) for name, value in parameters.items()]


def fresh_database(node: Node, index: int) -> None:
    """One database per case, so no case can see what another one wrote.

    Case 17's `needs` creates `memories:note-1` and case 14 creates it too. In
    one database the second `CREATE` meets a record that already exists and the
    run reports a collision the corpus never claimed.
    """
    name = f"case_{index:02d}"
    node.request(f"DEFINE DATABASE {name};")
    node.request(f"USE DATABASE {name};")
    node.request("DEFINE COLLECTION memories;")


def describe(refused: dict) -> str:
    reason = refused["reason"]
    if "what" in refused:
        return f"{reason}: {refused['what']} `{refused['name']}`"
    return reason


def inversion(node: Node, index: int, case: dict) -> dict:
    """Send the statement the builder refused to render, and record the answer."""
    if case["name"] not in NAIVE:
        return {"verdict": "no-naive-rendering"}
    script, parameters = NAIVE[case["name"]]
    try:
        fresh_database(node, 100 + index)
        node.request(script, bind(parameters))
    except Refused as why:
        return {"verdict": "node-refuses", "why": str(why), "script": script}
    except Malformed as why:
        return {"verdict": "shape", "why": str(why), "script": script}
    return {"verdict": "NODE-ACCEPTS", "script": script}


def run_case(node: Node, index: int, case: dict) -> dict:
    row = {"case": case["name"]}

    if "script" not in case:
        row["verdict"] = "builder-refused"
        row["why"] = describe(case["refused"])
        row["inversion"] = inversion(node, index, case)
        return row

    try:
        fresh_database(node, index)
        for need in case.get("needs", []):
            node.request(need["script"], bind(need["parameters"]))
    except (Refused, Malformed) as why:
        row["verdict"] = "setup"
        row["why"] = str(why)
        return row

    try:
        outcomes = node.request(case["script"], bind(case["parameters"]))
    except Refused as why:
        row["verdict"] = "REFUSED"
        row["why"] = str(why)
        row["script"] = case["script"]
        return row
    except Malformed as why:
        row["verdict"] = "shape"
        row["why"] = str(why)
        return row

    row["verdict"] = "ok"
    row["outcomes"] = [outcome["kind"] for outcome in outcomes]
    return row


def verify(corpus: dict, node: Node) -> list[dict]:
    return [run_case(node, index, case) for index, case in enumerate(corpus["cases"])]


MARKS = {
    "ok": "  ok",
    "REFUSED": "FAIL",
    "setup": " set",
    "shape": " shp",
    "builder-refused": "  --",
}

INVERSION_MARKS = {
    "node-refuses": "node refuses it too",
    "NODE-ACCEPTS": "NODE ACCEPTS IT — the builder's refusal is the only guard",
    "no-naive-rendering": "no naive rendering written for this case",
    "shape": "malformed answer",
}


def report(rows: list[dict]) -> int:
    for row in rows:
        print(f"{MARKS.get(row['verdict'], '  ??')}  {row['case']}")
        if row["verdict"] == "REFUSED":
            print(f"        sent    {row['script']}")
            print(f"        refused {row['why']}")
        elif row["verdict"] in ("setup", "shape"):
            print(f"        {row['why']}")
        elif row["verdict"] == "builder-refused":
            print(f"        builder: {row['why']}")
            inverted = row["inversion"]
            print(f"        naive:   {inverted.get('script', '—')}")
            print(f"        node:    {INVERSION_MARKS.get(inverted['verdict'], '?')}")
            if "why" in inverted:
                print(f"                 {inverted['why']}")

    counts: dict[str, int] = {}
    inverted_counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        if "inversion" in row:
            verdict = row["inversion"]["verdict"]
            inverted_counts[verdict] = inverted_counts.get(verdict, 0) + 1

    print(f"\nunits: source={len(rows)} | " + " | ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("inversion: " + " | ".join(f"{k}={v}" for k, v in sorted(inverted_counts.items())))
    print(
        "\nA `builder-refused` case carries no script, so a node cannot pass or fail\n"
        "it — counted apart rather than dropped from the total. Its inversion sends\n"
        "the statement a non-refusing builder would have produced; NODE-ACCEPTS there\n"
        "is the measurement that the refusal is load-bearing, not a defect."
    )
    bad = sum(v for k, v in counts.items() if k in ("REFUSED", "setup", "shape"))
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", default="127.0.0.1:47901", help="host:port of a node serving --serve")
    parser.add_argument(
        "--corpus", default=str(Path(__file__).with_name("queries-v1.json")), help="path to queries-v1.json"
    )
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))

    host, _, port = args.node.rpartition(":")
    node = Node(host, int(port))
    try:
        node.request("DEFINE NAMESPACE queries;")
        node.request("USE NAMESPACE queries;")
        rows = verify(corpus, node)
    finally:
        node.close()

    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    return report(rows)


if __name__ == "__main__":
    sys.exit(main())
