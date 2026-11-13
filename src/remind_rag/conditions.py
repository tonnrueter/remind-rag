"""Conditions parsed once: every $ifthen condition, if() guard, abort condition and equation $-condition the index
stores is parsed at build time into a tree (table `conditions`), and every reader evaluates that tree instead of
re-reading the text. Evaluation is three-valued: True, False, or None when the settings don't decide it.

A tree is a JSON list:
  ["and", a, b, ...] / ["or", a, b, ...] / ["not", a]
  ["sw", name, op, value, "c" | "r"]   a switch test: compile time (`"%x%" == "on"`) or run time (`cm_x eq 2`);
                                       op as in Python (==, !=, >=, ...), value without quotes
  ["other", text]                      anything else (indexed symbols, sameas(), t.val, other comparisons):
                                       unknown to the settings
  ["?", text, sw, ...]                 the whole condition didn't parse: undecided; the switch tests found in it
                                       are kept (a guard that tests a switch counts as depending on settings)"""

from __future__ import annotations

import json
import re
import sqlite3

from .usage import OPS, STR_TEST_RE, TEST_RE, _compare

Tree = list


def parse(cond: str) -> Tree:
    """The tree of one condition.

    >>> parse("cm_emiscen eq 9 and not (%optimization% == 'negishi')")
    ['and', ['sw', 'cm_emiscen', '==', '9', 'r'], ['not', ['sw', 'optimization', '==', 'negishi', 'c']]]
    >>> parse("sameas(t, '2005') or cm_x gt 1 or p_y(t) gt 0")
    ['or', ['other', "sameas(t, '2005')"], ['sw', 'cm_x', '>', '1', 'r'], ['other', 'p_y(t) gt 0']]
    >>> parse("cm_x eq 1 xor cm_y eq 2")
    ['?', 'cm_x eq 1 xor cm_y eq 2', ['sw', 'cm_x', '==', '1', 'r'], ['sw', 'cm_y', '==', '2', 'r']]
    """
    # Switch tests first, then everything else that isn't and/or/not/parentheses becomes an "other" operand
    # (function calls and indexed symbols innermost first, then comparisons, then bare names). Each is replaced
    # by a placeholder __tN; the skeleton left is parsed as and/or/not with Python's precedence.
    leaves: list[Tree] = []
    texts: list[str] = []

    def leaf(node: Tree, text: str) -> str:
        leaves.append(node)
        texts.append(text)
        return f" __t{len(leaves) - 1} "

    def original(text: str) -> str:  # an operand's text with the placeholders inside it put back
        return " ".join(re.sub(r"\s*__t(\d+)\s*", lambda m: f" {texts[int(m.group(1))]} ", text).split())

    def string_test(m: re.Match) -> str:
        node = ["sw", m.group(2), OPS[m.group(3).lower()], m.group(4), "c"]
        return leaf(["not", node] if m.group(1) else node, m.group(0).strip())

    def run_test(m: re.Match) -> str:
        return leaf(["sw", m.group(1), OPS[m.group(2).lower()], m.group(3).strip("\"'"), "r"], m.group(0).strip())

    def other(m: re.Match) -> str:
        return leaf(["other", original(m.group(0))], original(m.group(0)))

    expr = STR_TEST_RE.sub(string_test, cond)
    expr = TEST_RE.sub(run_test, expr)
    for _ in range(5):  # nested calls (up to 5 deep) from the inside out
        new = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)[A-Za-z_][\w.]*\s*\([^()]*\)", other, expr, flags=re.I)
        if new == expr:
            break
        expr = new
    expr = re.sub(r"(?<!\w)[\w.\"']+\s*(?:eq|ne|gt|lt|ge|le|<>|<=|>=|<|>|=)\s*[-\w.\"']+", other, expr, flags=re.I)
    expr = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)(?!__t\d)[A-Za-z_]\w*", other, expr, flags=re.I)
    try:
        return _Skeleton(expr, leaves).tree()
    except ValueError:
        tests = [t for t in leaves if t[0] == "sw" or t[0] == "not" and t[1][0] == "sw"]
        return ["?", " ".join(cond.split()), *tests]


class _Skeleton:
    """Recursive descent over placeholders, and/or/not and parentheses (not binds tighter than and, and than or)."""

    def __init__(self, expr: str, leaves: list[Tree]):
        self.toks = [t.lower() for t in re.findall(r"\w+|\S", expr)]
        self.leaves, self.i = leaves, 0

    def tree(self) -> Tree:
        t = self._or()
        if self.i != len(self.toks):
            raise ValueError
        return t

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _chain(self, op: str, operand) -> Tree:
        items = [operand()]
        while self._peek() == op:
            self.i += 1
            items.append(operand())
        if len(items) == 1:
            return items[0]
        return [op, *(x for t in items for x in (t[1:] if t[0] == op else [t]))]

    def _or(self) -> Tree:
        return self._chain("or", self._and)

    def _and(self) -> Tree:
        return self._chain("and", self._not)

    def _not(self) -> Tree:
        if self._peek() == "not":
            self.i += 1
            return ["not", self._not()]
        tok = self._peek()
        self.i += 1
        if tok == "(":
            t = self._or()
            if self._peek() != ")":
                raise ValueError
            self.i += 1
            return t
        if tok and (m := re.fullmatch(r"__t(\d+)", tok)):
            return self.leaves[int(m.group(1))]
        raise ValueError


def lower(values: dict[str, str]) -> dict[str, str]:
    """Settings keyed by lower-case name, as holds() reads them; on a clash of spellings the first one counts."""
    out: dict[str, str] = {}
    for k, v in values.items():
        out.setdefault(k.lower(), v)
    return out


def holds(tree: Tree, values: dict[str, str]) -> bool | None:
    """The tree's value under the settings `values` (lower-case name -> value, see lower()): True, False, or None
    when they don't decide it. A test of a name that isn't a setting is undecided.

    >>> v = {"cm_emiscen": "9", "optimization": "nash"}
    >>> holds(parse("cm_emiscen eq 9 and not (%optimization% == 'negishi')"), v)
    True
    >>> holds(parse("cm_emiscen eq 6 and sameas(t, '2005')"), v)  # false whatever t is
    False
    >>> holds(parse("cm_emiscen eq 9 and sameas(t, '2005')"), v) is None  # depends on t
    True
    """
    # Kleene's three-valued logic: and is False if any part is, True if all are; or the other way round. Each
    # operand occurs once in the tree, so this is exactly "the same for every value of the unknowns".
    kind = tree[0]
    if kind in ("and", "or"):
        parts = [holds(t, values) for t in tree[1:]]
        stop = kind == "or"  # the value that decides: True for or, False for and
        if stop in parts:
            return stop
        return None if None in parts else not stop
    if kind == "not":
        v = holds(tree[1], values)
        return None if v is None else not v
    if kind == "sw":
        value = values.get(tree[1].lower())
        return None if value is None else _compare(value, tree[2], tree[3])
    return None


def tests_setting(tree: Tree, values: dict[str, str]) -> bool:
    """The tree contains a test of a name in `values` (lower-case keys)."""
    if tree[0] == "sw":
        return tree[1].lower() in values
    if tree[0] == "?":
        return any(tests_setting(t, values) for t in tree[2:])
    return tree[0] in ("and", "or", "not") and any(tests_setting(t, values) for t in tree[1:])


# ------------------------------------------------------------------ the conditions table


def store(db: sqlite3.Connection, texts) -> int:
    """Parse every distinct condition text into the conditions table; the number stored."""
    rows = [(t, json.dumps(parse(t))) for t in sorted(set(texts))]
    db.executemany("INSERT INTO conditions VALUES (?, ?)", rows)
    db.commit()
    return len(rows)


class Trees:
    """Condition text -> tree, read from the index once. A text the index doesn't have (an index built before the
    table existed) is parsed on the spot."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._trees: dict[str, Tree] | None = None

    def __getitem__(self, cond: str) -> Tree:
        if self._trees is None:
            try:
                self._trees = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT text, tree FROM conditions")}
            except sqlite3.OperationalError:  # no such table
                self._trees = {}
        if (t := self._trees.get(cond)) is None:
            t = self._trees[cond] = parse(cond)
        return t
