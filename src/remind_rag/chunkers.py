"""Split REMIND source files into retrieval chunks and extract symbols/switches.

GAMS files get structure-aware treatment (declaration blocks, equation
definitions, main.gms switches); everything else is split at blank lines /
comment blocks / headings and packed to a target size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

TARGET_CHARS = 1200  # pack small segments up to roughly this size
MAX_CHARS = 2500  # hard cap per chunk (~700 tokens)


@dataclass
class Chunk:
    path: str  # relative to the REMIND root, forward slashes
    kind: str  # declaration|equation|switch|module_doc|realization_doc|gams_block|r_code|md_section
    line_start: int  # 1-based, inclusive
    line_end: int
    text: str
    name: str | None = None
    module: str | None = None
    realization: str | None = None
    phase: str | None = None
    detail: str | None = None  # e.g. declaration kind ("positive variables")
    header: str = ""  # filled by enrich


@dataclass
class Symbol:
    name: str
    kind: str  # scalar|parameter|variable|positive variable|...|equation|set|table
    domain: str
    description: str
    path: str
    line: int
    module: str | None = None
    realization: str | None = None


@dataclass
class Switch:
    name: str
    default: str
    allowed: str
    path: str
    line: int
    chunk_index: int = -1  # index into the file's chunk list


@dataclass
class ParsedFile:
    chunks: list[Chunk] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

MOD_RE = re.compile(r"^modules/(\d\d_\w+)/(?:(\w+)/)?([\w.]+)$")


def path_meta(path: str) -> dict:
    m = MOD_RE.match(path)
    if m:
        module, realization, fname = m.groups()
        return {"module": module, "realization": realization, "phase": fname.rsplit(".", 1)[0]}
    if path.startswith("core/"):
        return {"module": "core", "realization": None, "phase": PurePosixPath(path).stem}
    return {"module": None, "realization": None, "phase": None}


def _size(lines: list[str], s: int, e: int) -> int:
    return sum(len(line) + 1 for line in lines[s:e])


def _split_large(lines: list[str], s: int, e: int, max_chars: int = MAX_CHARS) -> list[tuple[int, int]]:
    """Split [s, e) into pieces <= max_chars, preferring cuts after blank lines."""
    out, cur, size, last_blank = [], s, 0, None
    for i in range(s, e):
        size += len(lines[i]) + 1
        if not lines[i].strip():
            last_blank = i
        if size > max_chars and i > cur:
            cut = last_blank + 1 if last_blank is not None and last_blank > cur else i
            out.append((cur, cut))
            cur, last_blank = cut, None
            size = _size(lines, cur, i + 1)
    if cur < e:
        out.append((cur, e))
    return out


def pack(lines: list[str], segs: list[tuple[int, int]], target: int = TARGET_CHARS) -> list[tuple[int, int]]:
    """Split oversized segments, then merge consecutive small ones up to `target`."""
    pieces = [p for s, e in segs for p in _split_large(lines, s, e)]
    out: list[tuple[int, int]] = []
    for s, e in pieces:
        if out and out[-1][1] == s and _size(lines, out[-1][0], out[-1][1]) < target and _size(lines, out[-1][0], e) <= MAX_CHARS:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _trim(lines: list[str], s: int, e: int) -> tuple[int, int]:
    while s < e and not lines[s].strip():
        s += 1
    while e > s and not lines[e - 1].strip():
        e -= 1
    return s, e


def _make_chunk(path: str, lines: list[str], s: int, e: int, kind: str, meta: dict, **kw) -> Chunk | None:
    s, e = _trim(lines, s, e)
    if s >= e:
        return None
    text = "\n".join(lines[s:e])
    return Chunk(path=path, kind=kind, line_start=s + 1, line_end=e, text=text, **meta, **kw)


def _segments_from_boundaries(bounds: set[int], s: int, e: int) -> list[tuple[int, int]]:
    cuts = sorted(b for b in bounds if s < b < e)
    edges = [s, *cuts, e]
    return [(a, b) for a, b in zip(edges, edges[1:]) if a < b]


# --------------------------------------------------------------------------- GAMS

LICENSE_RE = re.compile(r"^\*\*\* \||^\*\*\* (SOF|EOF) ")
DECL_RE = re.compile(
    r"^\s*((?:positive |negative |binary |integer |free |sos[12] |semicont |semiint )?variables?"
    r"|scalars?|parameters?|equations?|sets?|tables?)\b(.*)$",
    re.I,
)
ENTRY_RE = re.compile(r"""^\s*([A-Za-z]\w*)\s*(\([^)]*\))?\s*(?:"([^"]*)"|'([^']*)'|([^/;"']*))""")
EQDEF_RE = re.compile(r"^\s*([A-Za-z]\w*)\s*(\([^)]*\))?\s*(\$.*?)?\.\.(?!\.)")
SETGLOBAL_RE = re.compile(r"^\s*\$set[gG]lobal\s+(\w+)\s+(.*?)\s*(!!.*)?$", re.I)
ASSIGN_RE = re.compile(r"^\s*([A-Za-z]\w*)\s*=\s*([^;]*);\s*(!!.*)$")
DEF_RE = re.compile(r"def\s*=\s*(.*?)\s*(?:!!|$)")
REGEXP_RE = re.compile(r"regexp\s*=\s*(.*?)\s*(?:!!|$)")
STRING_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
NEW_SWITCH_DOC_RE = re.compile(r"^\*+'?\s*(c_|cm_|c\d\d_)\w+|^\*'?-{5,}")


def _is_comment(line: str) -> bool:
    return line.startswith("*")


def _code_part(line: str) -> str:
    """Line with strings and !! end-of-line comments removed (for ; and / detection)."""
    return STRING_RE.sub("", line).split("!!", 1)[0]


def _stmt_end(lines: list[str], i: int) -> int:
    """Index (exclusive) of the line terminating the statement starting at i."""
    for j in range(i, len(lines)):
        if not _is_comment(lines[j]) and ";" in _code_part(lines[j]):
            return j + 1
    return len(lines)


def _decl_kind(word: str) -> str:
    word = " ".join(word.lower().split())
    return re.sub(r"(variable|scalar|parameter|equation|set|table)s$", r"\1", word)


def _parse_decl_block(path, lines, s, e, first_rest, kind, meta) -> list[Symbol]:
    syms: list[Symbol] = []
    in_data = False
    for i in range(s, e):
        line = first_rest if i == s else lines[i]
        if not line.strip() or _is_comment(line.lstrip() if i == s else line):
            continue
        if not in_data:
            m = ENTRY_RE.match(line)
            if m and m.group(1).lower() not in {"alias"}:
                name, dom = m.group(1), (m.group(2) or "").strip("()")
                desc = (m.group(3) or m.group(4) or m.group(5) or "").strip()
                if desc or dom:
                    syms.append(Symbol(name, kind, dom, desc, path, i + 1, meta["module"], meta["realization"]))
        # element/data lists between slashes can span lines
        if _code_part(line).count("/") % 2 == 1:
            in_data = not in_data
    return syms


def chunk_gams(path: str, raw: str) -> ParsedFile:
    lines = [("" if LICENSE_RE.match(line) else line) for line in raw.splitlines()]
    meta = path_meta(path)
    out = ParsedFile()
    fname = PurePosixPath(path).name
    is_main = path == "main.gms"

    # module.gms / realization.gms: one doc chunk each
    if meta["module"] and fname in {"module.gms", "realization.gms"}:
        kind = "module_doc" if fname == "module.gms" else "realization_doc"
        name = meta["module"] if kind == "module_doc" else f"{meta['module']}/{meta['realization']}"
        for s, e in pack(lines, [(0, len(lines))]):
            c = _make_chunk(path, lines, s, e, kind, meta, name=name)
            if c:
                out.chunks.append(c)
        return out

    covered = [False] * len(lines)
    special: list[Chunk] = []

    # 1) declaration blocks
    i = 0
    while i < len(lines):
        line = lines[i]
        m = DECL_RE.match(line) if not _is_comment(line) else None
        # "sets" etc. must be the statement keyword, not e.g. "set_x(t) = ..."
        if m and (not m.group(2) or m.group(2)[:1] in " \t\"'(" or not m.group(2).strip()):
            e = _stmt_end(lines, i)
            kind = _decl_kind(m.group(1))
            syms = _parse_decl_block(path, lines, i, e, m.group(2), kind, meta)
            out.symbols.extend(syms)
            if not is_main:  # main.gms: declarations stay inside their switch chunk
                bounds = {j for j in range(i + 1, e) if _is_comment(lines[j]) and not _is_comment(lines[j - 1])}
                for s2, e2 in pack(lines, _segments_from_boundaries(bounds, i, e), target=1500):
                    names = [sy.name for sy in syms if s2 < sy.line <= e2]
                    c = _make_chunk(path, lines, s2, e2, "declaration", meta, detail=kind,
                                    name=", ".join(names[:12]) or None)
                    if c:
                        special.append(c)
                for j in range(i, e):
                    covered[j] = True
            i = e
            continue
        i += 1

    # 2) switches (main.gms only)
    if is_main:
        anchors = []  # (line index, name, default, allowed, param_style)
        for i, line in enumerate(lines):
            if _is_comment(line):
                continue
            m = SETGLOBAL_RE.match(line)
            if m:
                bang = m.group(3) or ""
                d = DEF_RE.search(bang)
                r = REGEXP_RE.search(bang)
                anchors.append((i, m.group(1), (d.group(1) if d else m.group(2)).strip(), r.group(1) if r else "", False))
                continue
            m = ASSIGN_RE.match(line)
            if m and "def" in m.group(3):
                d = DEF_RE.search(m.group(3))
                r = REGEXP_RE.search(m.group(3))
                anchors.append((i, m.group(1), (d.group(1) if d else m.group(2)).strip(), r.group(1) if r else "", True))
        start = 0
        for idx, (ai, name, default, allowed, param_style) in enumerate(anchors):
            end = ai + 1
            if param_style:  # the option list follows the assignment, until the next switch's doc starts
                while end < len(lines) and _is_comment(lines[end]) and not NEW_SWITCH_DOC_RE.match(lines[end]):
                    end += 1
            nxt = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(lines)
            end = min(end, nxt)
            first = len(special)
            for s2, e2 in _split_large(lines, start, end):
                c = _make_chunk(path, lines, s2, e2, "switch", meta, name=name)
                if c:
                    special.append(c)
            out.switches.append(Switch(name, default, allowed, path, ai + 1, chunk_index=first))
            for j in range(start, end):
                covered[j] = True
            start = end

    # 3) equation definitions
    for i, line in enumerate(lines):
        if covered[i] or _is_comment(line):
            continue
        m = EQDEF_RE.match(line)
        if not m:
            continue
        e = _stmt_end(lines, i)
        s = i
        while s > 0 and not covered[s - 1] and _is_comment(lines[s - 1]):
            s -= 1
        for s2, e2 in _split_large(lines, s, e):
            c = _make_chunk(path, lines, s2, e2, "equation", meta, name=m.group(1))
            if c:
                special.append(c)
        for j in range(s, e):
            covered[j] = True

    # 4) everything else: split at comment-block starts and blank lines, then pack
    rest: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if covered[i]:
            i += 1
            continue
        j = i
        while j < len(lines) and not covered[j]:
            j += 1
        bounds = {k for k in range(i + 1, j)
                  if (_is_comment(lines[k]) and not _is_comment(lines[k - 1])) or not lines[k - 1].strip()}
        rest.extend(pack(lines, _segments_from_boundaries(bounds, i, j)))
        i = j
    for s, e in rest:
        c = _make_chunk(path, lines, s, e, "gams_block", meta)
        if c:
            special.append(c)

    special.sort(key=lambda c: c.line_start)
    out.chunks = special
    # switch chunk_index must refer to the sorted list
    for sw in out.switches:
        for k, c in enumerate(out.chunks):
            if c.kind == "switch" and c.name == sw.name and c.line_start <= sw.line <= c.line_end:
                sw.chunk_index = k
                break
    return out


# --------------------------------------------------------------------------- R

RFUN_RE = re.compile(r"^([\w.]+)\s*(<-|=)\s*function\b")
RSECTION_RE = re.compile(r"^#+\s*(-{3,}|={3,}|#{3,})|^#{2,}\s")


def chunk_r(path: str, raw: str) -> ParsedFile:
    lines = raw.splitlines()
    meta = path_meta(path)
    bounds = set()
    for i, line in enumerate(lines):
        if RFUN_RE.match(line) or RSECTION_RE.match(line):
            s = i  # attach roxygen / comment block above
            while s > 0 and lines[s - 1].startswith("#"):
                s -= 1
            bounds.add(s)
        elif i > 0 and not lines[i - 1].strip() and line.strip():
            bounds.add(i)
    out = ParsedFile()
    for s, e in pack(lines, _segments_from_boundaries(bounds, 0, len(lines))):
        funs = [m.group(1) for line in lines[s:e] if (m := RFUN_RE.match(line))]
        c = _make_chunk(path, lines, s, e, "r_code", meta, name=", ".join(funs) or None)
        if c:
            out.chunks.append(c)
    return out


# --------------------------------------------------------------------------- Markdown / Rmd

HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)")


def chunk_markdown(path: str, raw: str) -> ParsedFile:
    lines = raw.splitlines()
    meta = path_meta(path)
    sections: list[tuple[int, int, str]] = []
    crumbs: list[str] = []
    in_fence, start, title = False, 0, ""
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = HEADING_RE.match(line) if not in_fence else None
        if m:
            if i > start:
                sections.append((start, i, title))
            level = len(m.group(1))
            crumbs = crumbs[: level - 1] + [m.group(2).strip()]
            start, title = i, " > ".join(crumbs)
    sections.append((start, len(lines), title))
    out = ParsedFile()
    for s, e, t in sections:
        segs = _segments_from_boundaries({k for k in range(s + 1, e) if not lines[k - 1].strip()}, s, e)
        for s2, e2 in pack(lines, segs, target=1800):
            c = _make_chunk(path, lines, s2, e2, "md_section", meta, name=t or None)
            if c:
                out.chunks.append(c)
    return out


def chunk_file(path: str, raw: str) -> ParsedFile:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".gms":
        return chunk_gams(path, raw)
    if suffix in {".r", ".cfg"}:
        return chunk_r(path, raw)
    if suffix in {".md", ".rmd"}:
        return chunk_markdown(path, raw)
    raise ValueError(f"unsupported file type: {path}")
