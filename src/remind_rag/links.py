"""One-hop links between GAMS symbols, computed at query time from the index (no extra tables).

For a symbol X, every statement that mentions it is looked up in the chunk text (from the previous `;` to the
next one) and classified with the roles stored in symbol_uses:
- computed from: statements assigning X, with the other symbols they read;
- feeds into:    statements reading X and assigning something else;
- in equations:  the other symbols of each equation X appears in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .chunkers import _code_part, _is_comment
from .usage import LOAD_RE

MAX_BACK, MAX_FORWARD = 40, 80  # statement length limits (lines) when scanning a chunk


@dataclass
class Statement:
    path: str
    start: int
    end: int
    text: str
    names: list[str] = field(default_factory=list)  # symbols in order of first appearance
    assigned: set[str] = field(default_factory=set)
    lines: dict[str, list[int]] = field(default_factory=dict)  # symbol -> lines it appears on

    def names_from(self, line: int) -> list[str]:
        """Symbols appearing on or after `line`: the right-hand side of an assignment starting there, without
        the conditions of an enclosing if(...) that the statement scan picked up."""
        return [n for n in self.names if max(self.lines[n]) >= line]


def _ends_statement(line: str) -> bool:
    return not _is_comment(line) and ";" in _code_part(line)


def statement(db: sqlite3.Connection, path: str, line: int) -> Statement | None:
    """The GAMS statement containing path:line, bounded by the chunk it sits in."""
    row = db.execute(
        "SELECT line_start, text FROM chunks WHERE path = ? AND line_start <= ? AND line_end >= ? "
        "AND kind IN ('gams_block', 'equation') ORDER BY line_end - line_start LIMIT 1", (path, line, line)
    ).fetchone()
    if not row:
        return None
    lines, first = row["text"].splitlines(), row["line_start"]
    i = line - first
    if not 0 <= i < len(lines):
        return None
    start = i
    while (start > 0 and i - start < MAX_BACK and not _ends_statement(lines[start - 1])
           and not lines[start - 1].lstrip().startswith("$")):
        start -= 1
    while start < i and (_is_comment(lines[start]) or not lines[start].strip()):
        start += 1  # report the first code line, not a comment above it
    end = i
    while end < len(lines) - 1 and end - i < MAX_FORWARD and not _ends_statement(lines[end]):
        end += 1
    text = "\n".join(x for x in lines[start:end + 1] if not _is_comment(x))
    st = Statement(path, first + start, first + end, text)
    for r in db.execute("SELECT name, role, line FROM symbol_uses WHERE path = ? AND line BETWEEN ? AND ? "
                        "ORDER BY line", (path, st.start, st.end)):
        if r["name"] not in st.names:
            st.names.append(r["name"])
        st.lines.setdefault(r["name"], []).append(r["line"])
        if r["role"] == "assigned":
            st.assigned.add(r["name"])
    return st


def is_load(st: Statement) -> bool:
    return bool(LOAD_RE.search(_code_part(st.text)))


def equation_members(db: sqlite3.Connection, path: str, equation: str) -> list[str]:
    """Symbols in one definition of an equation (in order of first appearance)."""
    names: list[str] = []
    for r in db.execute("SELECT name FROM symbol_uses WHERE path = ? AND context = ? COLLATE NOCASE "
                        "ORDER BY line", (path, equation)):
        if r["name"].lower() != equation.lower() and r["name"] not in names:
            names.append(r["name"])
    return names
