#!/usr/bin/env python3
"""Generate the query-builder conformance corpus for builder contract version 1.

This is a **second implementation** of the rendering rules, written from
`spec/query-builder-v1.md` alone. It exists so that every language's builder
compares against something other than the reference client — two builders that
disagree about a rendering disagree here, in a diff, rather than in a user's
report that the same query behaves differently depending on which client wrote
it.

Read the provenance note at the top of the contract before treating agreement
here as evidence about the *document*: version 1 of that document describes a
rendering the reference builder already emitted, so what this corpus establishes
is that independent implementations of the contract agree, and — when a client
runs point 4 of §6 — that a node accepts and executes the result.

Deliberately no dependency on anything but the standard library, and no reference
to any client's source while it was written.

Usage:
    python3 generate_queries.py > queries-v1.json     regenerate the corpus
    python3 generate_queries.py --check               fail if the committed corpus differs
"""

import argparse
import json
import pathlib
import sys

CORPUS = pathlib.Path(__file__).with_name("queries-v1.json")

# --- §3 names -----------------------------------------------------------------

ALPHA = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
DIGIT = set("0123456789")


def is_name(text):
    """§3: ( ALPHA / "_" ) *( ALPHA / DIGIT / "_" ), ASCII only."""
    if not text:
        return False
    if text[0] not in ALPHA and text[0] != "_":
        return False
    return all(character in ALPHA or character in DIGIT or character == "_" for character in text[1:])


class Refused(Exception):
    """A builder refusal, §5."""

    def __init__(self, reason, what=None, name=None):
        super().__init__(reason)
        self.reason = reason
        self.what = what
        self.name = name


def checked(what, name):
    if not is_name(name):
        raise Refused("not-a-name", what=what, name=name)
    return name


# --- §4.1 parameters ----------------------------------------------------------


class Binder:
    """Names parameters p0, p1, … in binding order, per statement."""

    def __init__(self):
        self.parameters = {}
        self.next = 0

    def bind(self, value):
        name = f"p{self.next}"
        self.next += 1
        self.parameters[name] = value
        return f"${name}"


# --- §4.3 filters -------------------------------------------------------------

OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}


def render_filter(node, binder):
    if "compare" in node:
        comparison = node["compare"]
        field = checked("a field", comparison["field"])
        operator = OPERATORS[comparison["op"]]
        reference = binder.bind(comparison["value"])
        return f"{field} {operator} {reference}"
    if "and" in node:
        left, right = node["and"]
        # Depth-first, left to right: the left subtree binds before the right.
        return f"({render_filter(left, binder)} AND {render_filter(right, binder)})"
    if "or" in node:
        left, right = node["or"]
        return f"({render_filter(left, binder)} OR {render_filter(right, binder)})"
    raise ValueError(f"not a filter: {node}")


# --- §4.7 / §4.8 objects ------------------------------------------------------


def render_object(fields, binder):
    parts = []
    # §4.8: ascending order of the names, compared as UTF-8 bytes.
    for name in sorted(fields, key=lambda text: text.encode("utf-8")):
        checked("a field", name)
        parts.append(f"{name}: {binder.bind(fields[name])}")
    return "{ " + ", ".join(parts) + " }"


def render_assignments(fields, binder):
    parts = []
    for name in sorted(fields, key=lambda text: text.encode("utf-8")):
        checked("a field", name)
        parts.append(f"{name} = {binder.bind(fields[name])}")
    return ", ".join(parts)


# --- §4.2 / §4.4 / §4.5 / §4.6 statements -------------------------------------


def render_projection_item(item):
    """One entry of §4.2's projection: a field name, or a line window.

    A window's two counts are literals rather than parameters, for the reason
    `START` and `LIMIT` are: they are the statement's shape, not data, and they
    arrive as integers, so there is nothing to smuggle syntax through. The alias
    is the field's own name, so the field comes back under the name it always
    had, holding less.
    """
    if isinstance(item, str):
        return checked("a field", item)
    window = item["lines"]
    field = checked("a field", window["field"])
    return f"string::lines({field}, {window['start']}, {window['count']}) AS {field}"


def render_select(spec, binder):
    table = checked("a table", spec["from"])
    named = spec.get("fields", [])
    if named:
        projection = ", ".join(render_projection_item(item) for item in named)
    else:
        projection = "*"

    script = f"SELECT {projection} FROM {table}"

    if "where" in spec:
        script += " WHERE " + render_filter(spec["where"], binder)

    ordering = spec.get("order", [])
    if ordering:
        parts = []
        for name, direction in ordering:
            parts.append(f"{checked('a field', name)} {direction.upper()}")
        script += " ORDER BY " + ", ".join(parts)

    # §4.2: START precedes LIMIT, and both are literals rather than parameters.
    if "start" in spec:
        script += f" START {spec['start']}"
    if "limit" in spec:
        script += f" LIMIT {spec['limit']}"

    return script + ";"


def render_create_record(spec, binder):
    table = checked("a table", spec["table"])
    fields = spec["set"]
    if not fields:
        raise Refused("incomplete")
    # §4.4: the identity binds first, before any field.
    identity = binder.bind(spec["id"])
    return f"CREATE {table}:{identity} = {render_object(fields, binder)};"


def render_create_in_table(spec, binder):
    table = checked("a table", spec["table"])
    fields = spec["set"]
    if not fields:
        raise Refused("incomplete")
    return f"CREATE {table} = {render_object(fields, binder)};"


def render_update_record(spec, binder):
    table = checked("a table", spec["table"])
    fields = spec["set"]
    if not fields:
        raise Refused("incomplete")
    identity = binder.bind(spec["id"])
    return f"UPDATE {table}:{identity} SET {render_assignments(fields, binder)};"


def render_delete_record(spec, binder):
    table = checked("a table", spec["table"])
    identity = binder.bind(spec["id"])
    return f"DELETE {table}:{identity};"


STATEMENTS = {
    "select": render_select,
    "create_record": render_create_record,
    "create_in_table": render_create_in_table,
    "update_record": render_update_record,
    "delete_record": render_delete_record,
}


def render(build):
    (kind,) = build.keys()
    binder = Binder()
    script = STATEMENTS[kind](build[kind], binder)
    return script, binder.parameters


# --- the cases ----------------------------------------------------------------
#
# Values use the tagged notation of values-v1.json — exactly one tagged key —
# because `none` and `null` are different values and an untagged notation could
# not tell them apart.

TEXT = lambda held: {"string": held}
INT = lambda held: {"integer": str(held)}


def case(name, build, note=None, needs=None):
    entry = {"name": name, "build": build}
    if needs:
        # Statements a runner must execute first for this case to have something
        # to act on. Rendered like any other case, so a runner replays them
        # through the same builder rather than needing a second path.
        entry["needs"] = needs
    if note:
        entry["note"] = note
    return entry


NOTE_ONE = {"create_record": {"table": "memories", "id": TEXT("note-1"), "set": {"body": TEXT("as it was")}}}


CASES = [
    # --- §4.2 SELECT ---------------------------------------------------------
    case("select-everything", {"select": {"from": "memories"}}),
    case(
        "select-named-fields-in-the-order-named",
        {"select": {"from": "memories", "fields": ["weight", "body"]}},
        "A projection keeps call order — unlike an object body, which sorts (§4.8).",
    ),
    case(
        "select-a-line-window-instead-of-the-whole-field",
        {"select": {"from": "memories", "fields": [{"lines": {"field": "body", "start": 0, "count": 40}}]}},
        "A long body comes back a window at a time; the alias is the field's own name (§4.2).",
    ),
    case(
        "select-a-line-window-beside-a-plain-field-keeps-call-order",
        {
            "select": {
                "from": "memories",
                "fields": ["weight", {"lines": {"field": "body", "start": 40, "count": 40}}],
            }
        },
        "A window is one projection item, so it orders with the plain names (§4.2).",
    ),
    case(
        "a-line-window-on-a-field-that-is-not-a-name-is-refused",
        {"select": {"from": "memories", "fields": [{"lines": {"field": "body-text", "start": 0, "count": 40}}]}},
        "The field inside a window is checked exactly as a bare field name is (§3).",
    ),
    case(
        "select-one-comparison",
        {"select": {"from": "memories", "where": {"compare": {"field": "session", "op": "eq", "value": TEXT("abc")}}}},
    ),
    # Every operator gets its own case: a builder that spells `lt` as `>` renders
    # text a parser accepts and a reader believes, so only a case per operator
    # catches it.
    *[
        case(
            f"select-comparison-{operator}",
            {"select": {"from": "memories", "where": {"compare": {"field": "weight", "op": operator, "value": INT(3)}}}},
        )
        for operator in OPERATORS
    ],
    case(
        "select-nested-and-or-is-fully-parenthesised",
        {
            "select": {
                "from": "memories",
                "where": {
                    "or": [
                        {
                            "and": [
                                {"compare": {"field": "session", "op": "eq", "value": TEXT("abc")}},
                                {"compare": {"field": "weight", "op": "ge", "value": INT(2)}},
                            ]
                        },
                        {"compare": {"field": "pinned", "op": "eq", "value": {"bool": True}}},
                    ]
                },
            }
        },
        "Parameters bind depth-first, left to right, so $p0 is the leftmost comparison.",
    ),
    case(
        "select-ordering-writes-the-direction-out",
        {"select": {"from": "memories", "order": [["created", "desc"], ["weight", "asc"]]}},
    ),
    case("select-start-before-limit", {"select": {"from": "memories", "start": 20, "limit": 10}}),
    case(
        "select-every-clause-at-once",
        {
            "select": {
                "from": "memories",
                "fields": ["body", "weight"],
                "where": {"compare": {"field": "session", "op": "eq", "value": TEXT("abc")}},
                "order": [["created", "desc"]],
                "start": 0,
                "limit": 50,
            }
        },
    ),
    case(
        "select-a-hostile-value-stays-a-value",
        {
            "select": {
                "from": "memories",
                "where": {"compare": {"field": "body", "op": "eq", "value": TEXT("'; DROP COLLECTION memories; --")}},
            }
        },
        "Protocol §7 clause 7: values travel through the codec, never through the text.",
    ),
    # --- §4.4 CREATE ---------------------------------------------------------
    case(
        "create-a-record-the-caller-named",
        {"create_record": {"table": "memories", "id": TEXT("note-1"), "set": {"weight": INT(3), "body": TEXT("metric units")}}},
        "The identity binds first ($p0); the fields then bind in name order, so body precedes weight.",
    ),
    case(
        "create-a-record-the-store-names",
        {"create_in_table": {"table": "memories", "set": {"body": TEXT("metric units")}}},
    ),
    case(
        "create-with-an-integer-identity",
        {"create_record": {"table": "memories", "id": INT(7), "set": {"body": TEXT("seven")}}},
    ),
    # --- §4.5 UPDATE ---------------------------------------------------------
    case(
        "update-sets-named-fields",
        {"update_record": {"table": "memories", "id": TEXT("note-1"), "set": {"weight": INT(5), "body": TEXT("changed")}}},
        needs=[NOTE_ONE],
    ),
    # --- §4.6 DELETE ---------------------------------------------------------
    case("delete-one-record", {"delete_record": {"table": "memories", "id": TEXT("note-1")}}, needs=[NOTE_ONE]),
    # --- §5 refusals ---------------------------------------------------------
    case(
        "a-table-that-is-not-a-name-is-refused",
        {"select": {"from": "memories; DROP COLLECTION memories; --"}},
        "Refused rather than quoted: quoting turns a mistake into a statement that means something else.",
    ),
    case("a-field-that-is-not-a-name-is-refused", {"select": {"from": "memories", "fields": ["body-text"]}}),
    case(
        "a-filter-field-that-is-not-a-name-is-refused",
        {"select": {"from": "memories", "where": {"compare": {"field": "1st", "op": "eq", "value": INT(1)}}}},
    ),
    case(
        "an-ordering-field-that-is-not-a-name-is-refused",
        {"select": {"from": "memories", "order": [["created at", "asc"]]}},
    ),
    case("a-create-with-no-fields-is-refused", {"create_record": {"table": "memories", "id": TEXT("x"), "set": {}}}),
    case("a-create-in-a-table-with-no-fields-is-refused", {"create_in_table": {"table": "memories", "set": {}}}),
    case("an-update-with-no-fields-is-refused", {"update_record": {"table": "memories", "id": TEXT("x"), "set": {}}}),
    case("a-delete-of-a-table-that-is-not-a-name-is-refused", {"delete_record": {"table": "1memories", "id": TEXT("x")}}),
]


def build_corpus():
    cases = []
    for entry in CASES:
        rendered = dict(entry)
        try:
            script, parameters = render(entry["build"])
        except Refused as refusal:
            refused = {"reason": refusal.reason}
            if refusal.what is not None:
                refused["what"] = refusal.what
            if refusal.name is not None:
                refused["name"] = refusal.name
            rendered["refused"] = refused
        else:
            rendered["script"] = script
            rendered["parameters"] = parameters
        if "needs" in rendered:
            prepared = []
            for prerequisite in rendered["needs"]:
                script, parameters = render(prerequisite)
                prepared.append({"build": prerequisite, "script": script, "parameters": parameters})
            rendered["needs"] = prepared

        cases.append(rendered)

    return {
        "contract_major": 1,
        "contract_minor": 0,
        "what_this_is": (
            "Rendering vectors for the query builder contract in spec/query-builder-v1.md. "
            "A conforming builder renders each case's `build` to exactly its `script` with "
            "exactly its `parameters`, and refuses each case carrying `refused`."
        ),
        "this_is_language_not_protocol": (
            "Section 6 of protocol-v1.md puts the query language outside the protocol. "
            "This corpus sits beside the value corpus rather than inside it."
        ),
        "needs_run_first": (
            "A case carrying `needs` names statements that must be executed before it, "
            "in order, for it to have anything to act on — an UPDATE needs the record it "
            "changes. Offline rendering ignores them; a node run does not."
        ),
        "run_it_against_a_node_too": (
            "Rendering agreement is the offline half. Section 6 point 4 of the contract asks "
            "a client that can reach a node to execute every rendered case with its parameters "
            "bound: that is the only check that reaches the parser."
        ),
        "generated_by": "generate_queries.py",
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed corpus differs")
    arguments = parser.parse_args()

    text = json.dumps(build_corpus(), indent=2, ensure_ascii=False) + "\n"

    if arguments.check:
        if not CORPUS.exists():
            print(f"{CORPUS} does not exist", file=sys.stderr)
            return 1
        committed = CORPUS.read_text(encoding="utf-8")
        if committed != text:
            print(
                f"{CORPUS.name} differs from what this generator produces.\n"
                "Regenerate it in the same commit as the change that moved it.",
                file=sys.stderr,
            )
            return 1
        print(f"{CORPUS.name}: {len(build_corpus()['cases'])} cases, current")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
