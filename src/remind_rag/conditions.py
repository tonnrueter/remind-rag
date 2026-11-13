"""Conditions parsed once: every $ifthen condition, if() guard, abort condition and equation $-condition the index
stores is parsed at build time into a tree (table `conditions`), and every reader evaluates that tree instead of
re-reading the text. Evaluation is three-valued: True, False, or None when the settings don't decide it.

A tree is a JSON list:
  ["and", a, b, ...] / ["or", a, b, ...] / ["not", a]
  ["sw", name, op, value, "c" | "r"]   a switch test: compile time (`"%x%" == "on"`) or run time (`cm_x eq 2`);
                                       op as in Python (==, !=, >=, ...), value without quotes
  ["sw2", name, op, text]              a switch compared with something that isn't a literal: another switch
                                       (`cm_peakBudgYr < cm_startyear`, decided by both values) or data
                                       (`cm_startyear <= t.val`: it tests a setting, the setting alone doesn't
                                       decide it)
  ["def", name]                        `$if set name` and its kin: whether a compile-time variable is defined
  ["env", text]                        `exist "file"`: the checkout, not the settings, decides it
  ["other", text]                      anything else (indexed symbols, sameas(), t.val, arithmetic, comparisons
                                       of data): unknown to the settings
  ["?", text, sw, ...]                 the whole condition didn't parse: undecided; the switch tests found in it
                                       are kept (a guard that tests a switch counts as depending on settings)"""

from __future__ import annotations

import json
import re
import sqlite3

from .usage import OPS, STR_TEST_RE, TEST_RE, _compare

Tree = list

# strings, %switch%, numbers, names (with .val / .l suffixes), two-character operators, anything else one character
TOKEN_RE = re.compile(
    r"""\s*("[^"]*"|'[^']*'|%\w+%|(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?|[A-Za-z_][\w.]*|==|<>|<=|>=|\*\*|\S)""", re.I
)
UNSUPPORTED = {"xor", "eqv", "imp"}  # GAMS logic REMIND doesn't use in conditions: such a condition stays "?"
DEF_RE = re.compile(r"(set|setglobal|setlocal|setenv)\s+(\w+)", re.I)
# a switch-shaped name (as TEST_RE), not indexed, compared with an expression
SW2_RE = re.compile(r"(c\w*?_\w+|s\d\d_\w+|sm_\w+)\s*(eq|ne|ge|le|gt|lt|<>|>=|<=|>|<|=)\s*(.+)", re.I)
# compile-time directives inside a run-time condition (37_industry: `( YES $ifthen.x ... AND ... $endif.x )`):
# which parts GAMS keeps depends on the compile, not on the text
DIRECTIVE_RE = re.compile(r"\$(ifthen|elseif|else|endif|if[ei]?)\b", re.I)


def parse(cond: str) -> Tree:
    """The tree of one condition: and / or / not (not binds tighter than and, and than or, as in GAMS) over
    operands. An operand runs to the next and / or / closing parenthesis outside brackets, so arithmetic and
    calls stay inside it; one that is a whole parenthesized group is parsed as a condition itself.

    >>> parse("cm_emiscen eq 9 and not (%optimization% == 'negishi')")
    ['and', ['sw', 'cm_emiscen', '==', '9', 'r'], ['not', ['sw', 'optimization', '==', 'negishi', 'c']]]
    >>> parse("sameas(t, '2005') or cm_x gt 1 or p_y(t) * 0.5 gt 0")
    ['or', ['other', "sameas(t, '2005')"], ['sw', 'cm_x', '>', '1', 'r'], ['other', 'p_y(t) * 0.5 gt 0']]
    >>> parse("((p_x(t) < -(0.1 + p_y(t))) AND (cm_peakBudgYr > 2040))")
    ['and', ['other', 'p_x(t) < -(0.1 + p_y(t))'], ['sw', 'cm_peakBudgYr', '>', '2040', 'r']]
    >>> parse('not set cm_x'), parse('exist "./in.inc"')
    (['not', ['def', 'cm_x']], ['env', 'exist "./in.inc"'])
    >>> parse("( t.val > 2010 ) AND ( cm_startyear <= t.val )")
    ['and', ['other', 't.val > 2010'], ['sw2', 'cm_startyear', '<=', 't.val']]
    >>> parse("cm_peakBudgYr < cm_startyear")
    ['sw2', 'cm_peakBudgYr', '<', 'cm_startyear']
    >>> parse("cm_x eq 1 xor cm_y eq 2")
    ['?', 'cm_x eq 1 xor cm_y eq 2', ['sw', 'cm_x', '==', '1', 'r'], ['sw', 'cm_y', '==', '2', 'r']]
    """
    try:
        if DIRECTIVE_RE.search(cond):
            raise ValueError
        return _Parser(cond).tree()
    except ValueError:
        return ["?", " ".join(cond.split()), *_tests_in(cond)]


def _tests_in(text: str) -> list[Tree]:
    """The switch tests anywhere in a text, for a condition that didn't parse."""
    out = [_string_test(m) for m in STR_TEST_RE.finditer(text)]
    return out + [_run_test(m) for m in TEST_RE.finditer(STR_TEST_RE.sub(" ", text))]


def _string_test(m: re.Match) -> Tree:
    node = ["sw", m.group(2), OPS[m.group(3).lower()], m.group(4), "c"]
    return ["not", node] if m.group(1) else node


def _run_test(m: re.Match) -> Tree:
    if re.match(r"[A-Za-z_]", m.group(3)):  # a name, not a literal: at run time a bare word is a symbol
        return ["sw2", m.group(1), OPS[m.group(2).lower()], m.group(3)]
    return ["sw", m.group(1), OPS[m.group(2).lower()], m.group(3).strip("\"'"), "r"]


def _operand(text: str) -> Tree:
    """One operand: a switch test, $if set / exist, or something the settings don't know."""
    if m := STR_TEST_RE.fullmatch(text):
        return _string_test(m)
    if m := TEST_RE.fullmatch(text):
        return _run_test(m)
    if m := SW2_RE.fullmatch(text):
        return ["sw2", m.group(1), OPS[m.group(2).lower()], m.group(3)]
    if m := DEF_RE.fullmatch(text):
        return ["def", m.group(2)]
    if re.match(r"d?exist\s", text, re.I):
        return ["env", text]
    return ["other", text]


class _Parser:
    def __init__(self, cond: str):
        self.cond = cond
        self.toks = [(m.group(1), m.start(1), m.end(1)) for m in TOKEN_RE.finditer(cond)]
        self.i = 0

    def tree(self) -> Tree:
        t = self._or()
        if self.i != len(self.toks):
            raise ValueError
        return t

    def _word(self) -> str | None:
        return self.toks[self.i][0].lower() if self.i < len(self.toks) else None

    def _chain(self, op: str, operand) -> Tree:
        items = [operand()]
        while self._word() == op:
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
        if self._word() == "not":
            self.i += 1
            return ["not", self._not()]
        return self._operand()

    def _end(self, i: int) -> int:
        """Index after the operand starting at token i: up to and / or / an unmatched ')' outside brackets."""
        depth = 0
        while i < len(self.toks):
            w = self.toks[i][0].lower()
            if depth == 0 and (w in ("and", "or") or w == ")"):
                break
            if depth == 0 and w in UNSUPPORTED:
                raise ValueError
            depth += (w == "(") - (w == ")")
            i += 1
        return i

    def _operand(self) -> Tree:
        start, end = self.i, self._end(self.i)
        if end == start:
            raise ValueError
        # a whole parenthesized group is a condition of its own: (a and b), (cm_x eq 1)
        if self.toks[start][0] == "(" and self._close(start) == end - 1:
            self.i = start + 1
            t = self._or()
            if self.i != end - 1:
                raise ValueError
            self.i = end
            return t
        self.i = end
        return _operand(" ".join(self.cond[self.toks[start][1] : self.toks[end - 1][2]].split()))

    def _close(self, i: int) -> int:
        """Index of the ')' matching the '(' at token i."""
        depth = 0
        for j in range(i, len(self.toks)):
            depth += (self.toks[j][0] == "(") - (self.toks[j][0] == ")")
            if depth == 0:
                return j
        return -1


def lower(values: dict[str, str]) -> dict[str, str]:
    """Settings keyed by lower-case name, as holds() reads them; on a clash of spellings the first one counts."""
    out: dict[str, str] = {}
    for k, v in values.items():
        out.setdefault(k.lower(), v)
    return out


def holds(tree: Tree, values: dict[str, str], compile: dict[str, str | None] | None = None) -> bool | None:
    """The tree's value under the settings `values` (lower-case name -> value, see lower()): True, False, or None
    when they don't decide it. A test of a name that isn't a setting is undecided, unless `compile` is given: the
    compile-time variables defined (lower-case name -> value, None if the settings don't decide it). Then a
    compile-time test of a name defined nowhere compares its literal text, as GAMS does (`%cm_less_TS%` stays
    `%cm_less_TS%`, so `"%cm_less_TS%" == "on"` is false), and `$if set x` is decided.

    >>> v = {"cm_emiscen": "9", "optimization": "nash"}
    >>> holds(parse("cm_emiscen eq 9 and not (%optimization% == 'negishi')"), v)
    True
    >>> holds(parse("cm_emiscen eq 6 and sameas(t, '2005')"), v)  # false whatever t is
    False
    >>> holds(parse("cm_emiscen eq 9 and sameas(t, '2005')"), v) is None  # depends on t
    True
    >>> holds(parse('"%cm_gone%" == "on"'), v) is None, holds(parse('"%cm_gone%" == "on"'), v, compile=v)
    (True, False)
    >>> w = {"cm_peakbudgyr": "2050", "cm_startyear": "2025"}
    >>> holds(parse("cm_peakBudgYr < cm_startyear"), w), holds(parse("cm_peakBudgYr eq s45_check"), w) is None
    (False, True)
    """
    # Kleene's three-valued logic: and is False if any part is, True if all are; or the other way round. Each
    # operand occurs once in the tree, so this is exactly "the same for every value of the unknowns".
    kind = tree[0]
    if kind in ("and", "or"):
        parts = [holds(t, values, compile) for t in tree[1:]]
        stop = kind == "or"  # the value that decides: True for or, False for and
        if stop in parts:
            return stop
        return None if None in parts else not stop
    if kind == "not":
        v = holds(tree[1], values, compile)
        return None if v is None else not v
    if kind == "sw" and tree[4] == "c" and compile is not None:
        name = tree[1].lower()
        value = compile[name] if name in compile else f"%{tree[1]}%"
        return None if value is None else _compare(value, tree[2], tree[3])
    if kind == "sw":
        value = values.get(tree[1].lower())
        return None if value is None else _compare(value, tree[2], tree[3])
    if kind == "sw2":  # decided only if the other side is a setting too
        a, b = values.get(tree[1].lower()), values.get(tree[3].lower())
        return None if a is None or b is None else _compare(a, tree[2], b)
    if kind == "def" and compile is not None:  # defined; None also when it may or may not be
        name = tree[1].lower()
        return None if name in compile and compile[name] is None else name in compile
    return None


def tests_setting(tree: Tree, values: dict[str, str]) -> bool:
    """The tree contains a test of a name in `values` (lower-case keys)."""
    if tree[0] in ("sw", "sw2"):
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
