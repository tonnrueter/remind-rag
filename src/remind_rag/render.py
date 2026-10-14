"""Text formatting for the MCP tools: chunks, use lines with their default status, per-file summaries, doc prose."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .status import DefaultRun, detail


def short(text: str, n: int) -> str:
    r"""Whitespace collapsed, cut to at most n characters (the last one an ellipsis).

    >>> short("vm_cap   capacity\n  of technologies", 15)
    'vm_cap capacit…'
    """
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def first_sentence(text: str, n: int = 180) -> str:
    """The first sentence, cut like short().

    >>> first_sentence("Prices rise. Then they fall.")
    'Prices rise.'
    """
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return short(m.group(1) if m else text, n)


def doc_text(text: str) -> str:
    r"""The prose of a goxygen doc chunk, joined to one line: `*'` lines without their @tags; code, @title,
    @authors and separator lines dropped.

    >>> doc_text("*' @title Carbon price\n*' @description The tax path.\n*' -----\np_x = 1;")
    'The tax path.'
    """
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("*'") or "@title" in s or "@authors" in s:
            continue
        s = re.sub(r"^\*'\s*(@\w+:?\s*)?", "", s)
        if s and not re.fullmatch(r"[-=*#_ ]+", s):
            keep.append(s)
    return " ".join(keep)


def fmt_chunk(run: DefaultRun, r, rank: int | None = None, cut: int | None = None) -> str:
    """A chunk with location, default status and header; with cut, at most that many text lines plus a pointer
    to the rest. The header's 'Uses:' line (symbol descriptions, there for the embedding) is left out."""
    where = f"{r['path']}:{r['line_start']}-{r['line_end']}"
    head = f"[{rank}] {where}" if rank else where
    status = "".join(f"\n» {s}" for s in run.chunk_status(r))
    header = "\n".join(x for x in r["header"].splitlines() if not x.startswith("Uses:"))
    lines = r["text"].splitlines()
    text = r["text"]
    if cut and len(lines) > cut:
        rest = r["line_start"] + cut
        text = "\n".join(lines[:cut]) + f"\n… ({len(lines) - cut} more lines, {r['path']}:{rest}–{r['line_end']})"
    return f"{head}  ({r['kind']}){status}\n{header}\n```\n{text}\n```"


def use_line(run: DefaultRun, u, tags: list[str], also: list[int] | None = None, snippet: int | None = None) -> str:
    ctx = "/".join(x for x in (u["module"], u["realization"], u["phase"]) if x)
    if u["context"]:
        ctx += f", in {u['context']}"
    text = short(u["snippet"], snippet) if snippet else u["snippet"]
    more = f"  (same reason: also line{'s' if len(also) > 1 else ''} {', '.join(map(str, also))})" if also else ""
    return f"- {u['path']}:{u['line']} [{ctx}] {run.mark(u, tags)}  {text}{more}" + detail(tags)


def grouped(run: DefaultRun, tagged: list) -> list[str]:
    """Summary lines: uses in the same file with the same reasons share one entry (first snippet shown)."""
    groups: dict[tuple, list] = {}
    for u, t in tagged:
        groups.setdefault((u["path"], tuple(t)), []).append((u, t))
    return [use_line(run, g[0][0], g[0][1], [u["line"] for u, _ in g[1:]], snippet=110) for g in groups.values()]


def mark_split(run: DefaultRun, tagged: list) -> str:
    """'5 ✓, 1 ⚠': how many use lines run by default, depend on settings, don't run."""
    marks = Counter(run.mark(u, t).split(" ")[0] for u, t in tagged)
    return ", ".join(f"{n} {m}" for m, n in sorted(marks.items(), key=lambda x: "✓?⚠".index(x[0])))


def per_file(run: DefaultRun, tagged: list, limit: int = 6) -> str:
    """'path n ✓' or 'path n (5 ✓, 1 ⚠)': every file with its default status, so a count never hides whether
    the file runs (audit 2026-10-01: '70_water/heat/output.gms 1' without a mark left the default unchecked)."""
    marks: dict[str, Counter] = defaultdict(Counter)
    for u, t in tagged:
        marks[u["path"]][run.mark(u, t).split(" ")[0]] += 1
    files = sorted(marks, key=lambda p: -sum(marks[p].values()))

    def status(c: Counter) -> str:
        if len(c) == 1:
            return next(iter(c))
        return "(" + ", ".join(f"{c[m]} {m}" for m in "✓?⚠" if c[m]) + ")"

    shown = [f"{p} {sum(marks[p].values())} {status(marks[p])}" for p in files[:limit]]
    return ", ".join(shown) + (f", +{len(files) - limit} more files" if len(files) > limit else "")
