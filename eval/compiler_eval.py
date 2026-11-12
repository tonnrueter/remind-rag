"""Does our GAMS parser agree with the GAMS compiler? Compare the compiler's reference file (every symbol use it
saw while compiling main.gms under the default settings) with the where-used table of the index (symbol_uses).
Free, local, read-only on the index and on the REMIND checkout.

    uv run python eval/compiler_eval.py [--db data/remind-bge.db] [--gams C:/GAMS/51/gams.exe] [--keep-dir DIR]
        [--out eval/results/compiler-v13.txt]

What it does: exports the indexed commit with `git archive` into a temp directory (never compiles inside the
checkout), runs `gams main.gms action=c rf=main.ref` there (action=c compiles only, ~1 s), reads the reference
file and keeps entries of git-tracked files. The compile reports errors for input data that is not in git (missing
.cs4r files and the like; checked once: with empty stand-in files the tracked-file entries stay identical), so the
archive is compiled as is. Entries of files the index does not read (data .inc files) are counted apart.

Four measurements (the report explains each in place):
  1. where-used agreement per (symbol, file, line): recall and precision, per role and symbol type;
  2. line tolerance: exact line vs anywhere in the same GAMS statement (statement = lines up to the next `;`);
  3. the default verdict (✓ / ⚠ / ?) of every use row against "did the compiler see this line";
  4. examples and cause classes of each disagreement.

The compiler sees one configuration. It decides `$if` / `$ifthen` / realization selection, so only that part of
the verdict is scored (realization + `$ifthen` conditions). `if(...)` guards are decided at run time, after the
compile; they are left out of the scored verdict and shown in a second table for completeness.
"""

# ruff: noqa: E501  (the report text is written as long lines)

from __future__ import annotations

import argparse
import bisect
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from remind_rag.gams import WORD_RE, code_part, in_text_block, is_comment
from remind_rag.status import DefaultRun

ARCHIVE_DIR = "remind_archive"  # the name is searched for in the paths of the reference file
REFTYPES = {"declared", "defined", "assigned", "impl-asn", "ref", "control"}
TYPE_NAMES = {"SETS": "set", "PARAMETERS": "parameter", "VARIABLES": "variable", "EQUATIONS": "equation"}
SCORED_TYPES = ("parameter", "variable", "equation")  # the headline; sets are reported next to them (see report)


# ------------------------------------------------------------------------------------------------ the compile


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def export_and_compile(remind: Path, commit: str, gams: str, work: Path) -> tuple[Path, str]:
    """git archive <commit> into work/ARCHIVE_DIR, compile main.gms there. Returns (archive dir, compile summary)."""
    dest = work / ARCHIVE_DIR
    dest.mkdir(parents=True)
    git = subprocess.Popen(["git", "-C", str(remind), "archive", commit], stdout=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=git.stdout, capture_output=True)
    git.stdout.close()
    if git.wait() or tar.returncode:
        sys.exit(f"git archive | tar failed: {tar.stderr.decode(errors='replace')[:300]}")
    # the exit code is non-zero because of the missing input data; the .ref file is what counts
    run([gams, "main.gms", "action=c", "rf=main.ref", "lo=2", "o=compile.lst"], cwd=dest)
    if not (dest / "main.ref").exists():
        sys.exit("GAMS wrote no main.ref; see " + str(dest / "compile.lst"))
    lst = (dest / "compile.lst").read_text(errors="replace")
    errors = re.search(r"\*{4}\s+(\d+) ERROR\(S\)", lst)
    return dest, f"{errors.group(1) if errors else '?'} compile errors (missing input data, see docstring)"


def tracked_files(remind: Path, commit: str) -> dict[str, str]:
    """lower-case path -> path as git spells it, for every file in the commit."""
    out = run(["git", "-C", str(remind), "ls-tree", "-r", "--name-only", commit], text=True, check=True).stdout
    return {p.lower(): p for p in out.splitlines() if p}


def read_ref(ref: Path, tracked: dict[str, str]) -> list[tuple[str, str, str, str, int, int]]:
    """Entries (name lower, compiler type, reftype, tracked file, line, global line) of tracked files. Space separated
    `id symbolIndex name type reftype globalLine localLine column includeLevel fileIndex path`; the path may
    contain spaces. The symbol table at the end of the file has another layout, dropped by the reftype check."""
    out = []
    for line in ref.read_text(errors="replace").splitlines():
        p = line.split(maxsplit=10)
        if len(p) < 11 or p[4] not in REFTYPES or not p[6].isdigit():
            continue
        path = p[10].replace("\\", "/")
        i = path.lower().find(f"/{ARCHIVE_DIR}/")
        if i < 0:
            continue
        rel = tracked.get(path[i + len(ARCHIVE_DIR) + 2 :].lower())
        if rel:
            out.append((p[2].lower(), TYPE_NAMES.get(p[3], p[3].lower()), p[4], rel, int(p[6]), int(p[5])))
    return out


def read_echo(lst: Path) -> dict[int, str]:
    """Global line number -> source text of every line the compiler echoed into compile.lst. The numbers are the
    reference file's `globalLine`. Lines in untaken `$if` branches are not echoed; directives and comments are."""
    echo: dict[int, str] = {}
    for line in lst.read_text(errors="replace").splitlines():
        m = re.match(r" *([0-9]{1,9}) {2}(.*)$", line)
        if m:
            echo.setdefault(int(m.group(1)), m.group(2))
    return echo


def squeeze(s: str) -> str:
    return re.sub(r"\s+", "", s)


def source_pattern(src: str) -> re.Pattern:
    """Regex for the echoed form of a source line: whitespace dropped, each `%name%` may have been replaced by its
    value (the echo shows `$ifthen not default == "fast"` for `$ifthen not %cm_x% == "fast"`)."""
    parts = re.split(r"%[\w.]*%", squeeze(src))
    return re.compile(".*?".join(re.escape(x) for x in parts), re.S)


# ------------------------------------------------------------------------------------------------ statements


class Files:
    """Source lines of the archive and statement boundaries, for the span tolerance and for the examples."""

    def __init__(self, root: Path):
        self.root = root
        self._lines: dict[str, list[str]] = {}
        self._stmt: dict[str, list[int]] = {}
        self._endif: dict[str, dict[int, int]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            text = (self.root / rel).read_text(errors="replace")
            self._lines[rel] = [x.rstrip("\r") for x in text.split("\n")]
        return self._lines[rel]

    def text(self, rel: str, line: int) -> str:
        ls = self.lines(rel)
        return ls[line - 1].strip() if 0 < line <= len(ls) else ""

    def stmt_start(self, rel: str, line: int) -> int:
        """First line of the statement containing `line` (1-based). A statement runs to the line with its `;`;
        comments, blank lines and `$` directive lines are statements of their own."""
        if rel not in self._stmt:
            ls = self.lines(rel)
            text = in_text_block(ls)
            start, first = [0] * len(ls), None
            for i, s in enumerate(ls):
                start[i] = i + 1
                if text[i] or is_comment(s) or not s.strip() or s.lstrip().startswith("$"):
                    continue
                first = i if first is None else first
                start[i] = first + 1
                if ";" in code_part(s):
                    for j in range(first, i + 1):
                        if not (text[j] or is_comment(ls[j]) or ls[j].lstrip().startswith("$")):
                            start[j] = first + 1
                    first = None
            self._stmt[rel] = start
        st = self._stmt[rel]
        return st[line - 1] if 0 < line <= len(st) else line

    def endif_of(self, rel: str, line: int) -> int | None:
        """Line of the `$endif` that closes the `$ifthen` / `$elseif` / `$else` at `line`, None if not found."""
        if rel not in self._endif:
            ls = self.lines(rel)
            text = in_text_block(ls)
            stack: list[list[int]] = []
            ends: dict[int, int] = {}
            for i, s in enumerate(ls):
                if text[i] or is_comment(s):
                    continue
                t = s.strip().lower()
                if re.match(r"\$+ifthen[ei]?", t):
                    stack.append([i + 1])
                elif re.match(r"\$+else(if[ei]?)?", t) and stack:
                    stack[-1].append(i + 1)
                elif re.match(r"\$+endif", t) and stack:
                    for j in stack.pop():
                        ends[j] = i + 1
            self._endif[rel] = ends
        return self._endif[rel].get(line)

    def conditional_context(self, rel: str, line: int) -> str:
        """Innermost open `$if...` directive above the line (text), '' outside any; for grouping verdict errors."""
        stack = []
        for s in self.lines(rel)[: line - 1]:
            t = s.strip().lower()
            if re.match(r"\$ifthen[ei]?\b", t):
                stack.append(t)
            elif re.match(r"\$else(if[ei]?)?\b", t) and stack:
                stack[-1] = t
            elif t.startswith("$endif") and stack:
                stack.pop()
        return stack[-1] if stack else ""


# ------------------------------------------------------------------------------------------------ measuring


def allowed_roles(reftype: str, ctype: str) -> set[str]:
    """RAG roles that count as the same thing as a compiler reftype. The RAG role `equation` is "anywhere inside
    an equation definition" and `read` everything else, so a compiler `ref` fits both; a declaration that carries
    data is `assigned` in the RAG (FINDINGS 19), so the compiler's `declared` / `defined` on such a line fit both."""
    return {
        "declared": {"declared", "assigned"},
        "assigned": {"assigned", "declared"},
        "defined": {"equation", "declared"} if ctype == "equation" else {"assigned", "declared"},
        "ref": {"read", "equation"},
        "impl-asn": {"assigned"},
    }.get(reftype, set())


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "   n/a"


def table(title: str, rows: list[tuple], header: tuple, out: list[str]) -> None:
    out.append(f"\n{title}")
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]
    for r in (header, *rows):
        out.append(
            "  "
            + "  ".join(
                str(x).rjust(w) if i else str(x).ljust(w) for i, (x, w) in enumerate(zip(r, widths, strict=True))
            )
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/remind-bge.db")
    ap.add_argument("--remind", help="REMIND checkout (default: meta.root of the index)")
    ap.add_argument("--gams", default="C:/GAMS/51/gams.exe")
    ap.add_argument("--keep-dir", help="keep the archive, main.ref and compile.lst in this directory (must not exist)")
    ap.add_argument("--out", default="eval/results/compiler-v13.txt")
    ap.add_argument("--examples", type=int, default=10, help="examples per disagreement class (default 10)")
    args = ap.parse_args()
    ex_n = args.examples

    db = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    commit = meta["remind_commit"]
    remind = Path(args.remind or meta["root"])
    head = run(["git", "-C", str(remind), "rev-parse", "HEAD"], text=True).stdout.strip()

    if args.keep_dir:
        work = Path(args.keep_dir)
        work.mkdir(parents=True)
        tmp = None
    else:
        tmp = tempfile.TemporaryDirectory(prefix="compiler_eval_")
        work = Path(tmp.name)
    try:
        lines = report(args, db, meta, remind, commit, head, work, ex_n)
    finally:
        if tmp:
            tmp.cleanup()
    text = "\n".join(lines) + "\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def report(args, db, meta, remind, commit, head, work, ex_n) -> list[str]:
    out: list[str] = []
    w = out.append
    arch, summary = export_and_compile(remind, commit, args.gams, work)
    tracked = tracked_files(remind, commit)
    entries = read_ref(arch / "main.ref", tracked)
    files = Files(arch)

    w("GAMS compiler vs index: where-used agreement")
    w(f"index: {args.db} (meta.remind_commit {commit[:12]}, built {meta.get('built_at')})")
    w(
        f"archive: git archive {commit[:12]}"
        f" (= meta.remind_commit of the index; checkout HEAD {head[:12]} {'matches' if head == commit else 'DIFFERS, archive is the index commit'})"
    )
    w(f"compiler: {args.gams} main.gms action=c, {summary}")
    w(f"reference entries in git-tracked files: {len(entries):,}; reftypes {dict(Counter(e[2] for e in entries))}")

    # ---- compiler side: key (symbol, file, line) -> reftypes
    comp: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    ctype: dict[str, str] = {}
    # file -> (local line, global line - local line) of its entries. The offset changes after every skipped
    # `$if` block (compile.lst numbers only the lines it echoes) and per include instance, so the offsets to try
    # for a line are those of the entries around it.
    offsets: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for name, typ, rt, rel, ln, gl in entries:
        comp[(name, rel.lower(), ln)].add(rt)
        ctype.setdefault(name, typ)
        offsets[rel.lower()].append((ln, gl - ln))
    echo = read_echo(arch / "compile.lst")
    echo_sq = {g: squeeze(t) for g, t in echo.items()}
    cfiles = {k[1] for k in comp}
    shown = {k[1]: None for k in comp}  # lower -> display, filled below
    for _, _, _, rel, _, _ in entries:
        shown[rel.lower()] = rel
    lines_with_entry = {(k[1], k[2]) for k in comp}
    seen_lines: dict[tuple[str, int], bool] = {}

    for v in offsets.values():
        v.sort()
    near_window = 60

    def candidate_globals(f: str, ln: int) -> set[int]:
        """Global lines the source line `ln` of file `f` can have. After an entry (local lp, global gp) the lines
        up to `ln` are echoed in order, minus the skipped ones, so ln lies in gp+1 .. gp+(ln-lp). One range per
        include instance (distinct offset) among the entries just before the line."""
        pairs = offsets[f]
        i = bisect.bisect_left(pairs, (ln, -(10**9)))
        best: dict[int, int] = {}  # offset -> nearest preceding local line with it
        for lp, d in pairs[max(0, bisect.bisect_left(pairs, (ln - near_window, -(10**9)))) : i]:
            best[d] = lp
        if not best and i:
            best[pairs[i - 1][1]] = pairs[i - 1][0]
        out: set[int] = set()
        for d, lp in best.items():
            out.update(range(lp + d + 1, ln + d + 1))
        # an `$include` between the anchor and the line shifts everything after it, so also count backwards from
        # the entries just after the line: the lines before such an entry are echoed in order, minus skipped ones
        after: dict[int, int] = {}
        j = bisect.bisect_right(pairs, (ln, 10**9))
        for ln_, d in reversed(pairs[j : bisect.bisect_right(pairs, (ln + near_window, 10**9))]):
            after[d] = ln_
        if not after and j < len(pairs):
            after[pairs[j][1]] = pairs[j][0]
        for d, ln_ in after.items():
            out.update(range(ln + d, ln_ + d))
        return out

    def echoed(f: str, ln: int, pat: re.Pattern) -> bool:
        return any(g in echo_sq and pat.fullmatch(echo_sq[g]) for g in candidate_globals(f, ln))

    def scored_tokens(f: str, ln: int) -> list[str]:
        words = WORD_RE.findall(code_part(files.text(shown[f], ln)))
        return [t.lower() for t in words if ctype.get(t.lower()) in SCORED_TYPES]

    def compiled_line(f: str, ln: int) -> bool:
        """Did the compiler read this line? Yes if it has a symbol entry, or if compile.lst echoes a line at
        local line + (an include instance's offset) whose text equals the source line (after %name% expansion).
        The echo is what makes `$ifthen` / `$include` / `$setglobal` lines and comment-only lines judgeable."""
        if (f, ln) not in seen_lines:
            ok = (f, ln) in lines_with_entry
            src1 = files.text(shown[f], ln).lower()
            if not ok and re.match(r"\$+if(i|e)? ", src1):
                # a one-line `$if cond statement`: the line is echoed whether or not the condition holds, so the
                # echo says nothing. It was read if a symbol entry sits on it (above) or the statement is a
                # `$include` of a file the compiler did compile
                m = re.search(r"\$+(?:bat)?include\s+\"?([^\"\s]+)", src1)
                if m:
                    rx = re.compile(
                        ".*".join(re.escape(x) for x in re.split(r"%[\w.]*%", m.group(1).removeprefix("./")))
                    )
                    ok = any(rx.fullmatch(c) for c in cfiles)
            elif not ok and not src1.startswith("$") and (toks := scored_tokens(f, ln)):
                # a compiled code line has an entry for every parameter / variable / equation on it (sets are
                # skipped: in a set's element list a label can share a symbol's name). The entry may sit on another
                # line of the statement (GAMS puts an assignment on the line of its `=`). Symbols but no entry
                # anywhere in the statement: the line was skipped, e.g. a one-line `$if` that failed
                st = files.stmt_start(shown[f], ln)
                ok = any(comp_span.get((t, f, st)) for t in toks)
            elif not ok:
                pat = source_pattern(files.text(shown[f], ln))
                ok = echoed(f, ln, pat)
                src = files.text(shown[f], ln).lower()
                if not ok and re.match(r"\$+(ifthen|else)", src):
                    # a false `$ifthen` / `$elseif` line and the skipped block are not echoed, its `$endif` is:
                    # the directive was read if the closing `$endif` was
                    end = files.endif_of(shown[f], ln)
                    ok = bool(end) and echoed(f, end, source_pattern(files.text(shown[f], end)))
            seen_lines[f, ln] = ok
        return seen_lines[f, ln]

    comp_span: dict[tuple[str, str, int], set[str]] = defaultdict(set)  # (symbol, file, statement start)
    for (n, f, ln), rts in comp.items():
        comp_span[(n, f, files.stmt_start(shown[f], ln))] |= rts

    # ---- index side: only .gms rows in files the compiler compiled
    uses = [dict(r) for r in db.execute("SELECT * FROM symbol_uses ORDER BY path, line, name")]
    gms_uses = [u for u in uses if u["path"].lower().endswith(".gms")]
    vocab = {u["name"].lower() for u in uses}
    kinds = {r["name"].lower(): r["kind"] for r in db.execute("SELECT name, kind FROM symbols")}
    rag: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    rag_span: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    in_compiled = []
    scanned = {r[0].lower() for r in db.execute("SELECT DISTINCT path FROM chunks")}
    switch_names = {r[0].lower() for r in db.execute("SELECT name FROM switches")}
    for u in gms_uses:
        f = u["path"].lower()
        if f in cfiles:
            in_compiled.append(u)
            rag[(u["name"].lower(), f, u["line"])].append(u)
            rag_span[(u["name"].lower(), f, files.stmt_start(shown[f], u["line"]))].append(u)
    dup = sum(len(v) > 1 for v in rag.values())

    w(
        f"\ncompiled files (>= 1 tracked entry): {len(cfiles)}; index .gms rows in them: {len(in_compiled):,} of {len(gms_uses):,}"
        f" .gms rows ({len(uses):,} rows incl. R); keys with more than one row: {dup}"
    )
    w("symbol type = the compiler's; symbols the compiler never saw get their kind from the index `symbols` table.")
    w(
        "`vocabulary` = symbol names that occur in symbol_uses at all (the index records declared symbols and switches;"
        " the compiler also reports functions, macros, files, models and local set indices)."
    )

    def stype(name: str) -> str:
        if name in switch_names and name not in ctype:
            return "switch"  # a main.gms switch used as %name%: not a GAMS symbol
        return ctype.get(name) or (kinds.get(name) or "unknown").split()[-1]

    # ============================================================ 1+2. recall
    # one item per (key, reftype): control is separate, impl-asn does not occur (error 257: solve not checked)
    unscanned = Counter(k[1].rsplit(".", 1)[-1] for k in comp if k[1] not in scanned)
    w(
        f"\ncompiler keys in files the index does not read (no chunk): {sum(unscanned.values()):,} by extension {dict(unscanned)}"
        " (data include files such as .inc / .cs4r-derived tables); left out of every recall figure below."
    )
    items = [(k, rt) for k, rts in comp.items() for rt in rts if rt != "control" and k[1] in scanned]
    ctrl_keys = [k for k, rts in comp.items() if rts == {"control"} and k[1] in scanned]
    w(
        f"\ncompiler entries as (symbol, file, line, reftype): {len(items):,}; lines with only `control` (loop / sum "
        f"indices, no counterpart in the index, not scored): {len(ctrl_keys):,}; impl-asn: {sum(rt == 'impl-asn' for _, rt in items)}"
        " (none: solve statements are not checked after error 257, so no variable counts as 'never assigned' here)"
    )

    def recall_rows(select, by):
        stats = defaultdict(lambda: [0, 0, 0, 0])  # n, exact present, span present, role ok (exact)
        for (n, f, ln), rt in items:
            if not select(n):
                continue
            s = stats[by(n, rt)]
            s[0] += 1
            hit = rag.get((n, f, ln))
            span = rag_span.get((n, f, files.stmt_start(shown[f], ln)))
            s[1] += bool(hit)
            s[2] += bool(span)
            if hit:
                roles = set().union(*(allowed_roles(r, ctype.get(n, "")) for r in comp[(n, f, ln)]))
                s[3] += any(h["role"] in roles for h in hit)
        return stats

    def emit_recall(title, stats):
        rows = [
            (k, v[0], pct(v[1], v[0]), pct(v[2], v[0]), pct(v[3], v[1]))
            for k, v in sorted(stats.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ]
        tot = [sum(v[i] for v in stats.values()) for i in range(4)]
        rows.append(("all", tot[0], pct(tot[1], tot[0]), pct(tot[2], tot[0]), pct(tot[3], tot[1])))
        table(title, rows, ("", "entries", "recall exact", "recall span", "role ok|found"), w_list)

    w_list: list[str] = []
    in_vocab = lambda n: n in vocab  # noqa: E731
    w("\n=== 1+2. RECALL: compiler entries the index has ===")
    w(
        "recall exact = the index has a row for the same symbol on the same line; span = on any line of the same statement;"
        "\n`role ok|found` = of the exact hits, the index role is one the compiler reftype allows (declared->declared|assigned,"
        " assigned->assigned|declared, defined->equation (EQU) or assigned|declared, ref->read|equation;"
        "\nall reftypes on the line are pooled, so `x(i) = x(i) + 1` is fine as `assigned`)."
    )
    for title, select in (("all compiler symbols", lambda n: True), ("symbols in the index vocabulary", in_vocab)):
        for part, by, sel in (
            ("by reftype", lambda n, rt: rt, select),
            ("by symbol type", lambda n, rt: stype(n), select),
        ):
            w_list.clear()
            emit_recall(f"-- {title}, {part}", recall_rows(sel, by))
            out += w_list
        w_list.clear()
        emit_recall(
            f"-- {title}, reftype x type (parameter / variable / equation only)",
            recall_rows(
                lambda n, select=select: select(n) and stype(n) in SCORED_TYPES, lambda n, rt: f"{stype(n)}/{rt}"
            ),
        )
        out += w_list
    w(
        "\nSets are left out of the headline: the compiler reports every index position (`sum(t, ...)`, `loop(regi, ...)`,"
        "\ndomains) of a set as ref / control, while symbol_uses holds rows for only "
        f"{sum(1 for n in vocab if ctype.get(n) == 'set')} of the {sum(1 for t in ctype.values() if t == 'set')} sets"
        "\nthe compiler saw. Their numbers stay in the tables above."
    )

    # ============================================================ 1+2. precision
    def covered(u):
        return compiled_line(u["path"].lower(), u["line"])

    on_compiled = [u for u in in_compiled if covered(u)]
    w("\n=== 1+2. PRECISION: index rows the compiler confirms ===")
    w(
        f"rows in compiled files: {len(in_compiled):,}; of them on compiled lines (see compiled_line): {len(on_compiled):,}."
        " Rows on other lines are the verdict question (section 3)."
    )
    w(
        "precision exact = the compiler has an entry (any reftype, `control` included) for the symbol on that line;"
        " span = in the same statement;\n`role ok|found` = the compiler reftypes allow the index role (control-only keys are not role-checked)."
    )

    def prec(select, by):
        stats = defaultdict(lambda: [0, 0, 0, 0, 0])
        for u in on_compiled:
            n, f = u["name"].lower(), u["path"].lower()
            if not select(u):
                continue
            s = stats[by(u)]
            s[0] += 1
            rts = comp.get((n, f, u["line"]))
            sp = comp_span.get((n, f, files.stmt_start(shown[f], u["line"])))
            s[1] += bool(rts)
            s[2] += bool(sp)
            if rts and rts != {"control"}:
                s[3] += 1
                roles = set().union(*(allowed_roles(r, ctype.get(n, "")) for r in rts))
                s[4] += u["role"] in roles
        return stats

    def emit_prec(title, stats):
        rows = [
            (k, v[0], pct(v[1], v[0]), pct(v[2], v[0]), pct(v[4], v[3]))
            for k, v in sorted(stats.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ]
        tot = [sum(v[i] for v in stats.values()) for i in range(5)]
        rows.append(("all", tot[0], pct(tot[1], tot[0]), pct(tot[2], tot[0]), pct(tot[4], tot[3])))
        table(title, rows, ("", "rows", "precision exact", "precision span", "role ok|found"), w_list)

    for title, select in (
        ("all rows", lambda u: True),
        ("rows of symbols the compiler knows", lambda u: u["name"].lower() in ctype),
    ):
        for part, by in (
            ("by index role", lambda u: u["role"]),
            ("by symbol type", lambda u: stype(u["name"].lower())),
        ):
            w_list.clear()
            emit_prec(f"-- {title}, {part}", prec(select, by))
            out += w_list
    w_list.clear()
    emit_prec(
        "-- parameter / variable / equation rows, by index role x type",
        prec(lambda u: stype(u["name"].lower()) in SCORED_TYPES, lambda u: f"{stype(u['name'].lower())}/{u['role']}"),
    )
    out += w_list

    # ============================================================ role confusion on exact hits
    conf: dict[tuple[str, str], int] = Counter()
    for (n, f, ln), rts in comp.items():
        hit = rag.get((n, f, ln))
        if hit and rts != {"control"} and stype(n) in SCORED_TYPES:
            conf["+".join(sorted(rts - {"control"})), hit[0]["role"]] += 1
    roles = ["declared", "assigned", "equation", "read"]
    classes = sorted({k[0] for k in conf}, key=lambda c: -sum(v for k, v in conf.items() if k[0] == c))
    w_list.clear()
    table(
        "-- role confusion on exact hits (parameter / variable / equation): compiler reftypes on the line (rows) vs index role",
        [(c, *(conf[c, r] for r in roles)) for c in classes[:14]],
        ("compiler", *roles),
        w_list,
    )
    out += w_list

    # ============================================================ disagreement classes and examples
    w("\n=== 4. DISAGREEMENTS AND EXAMPLES ===")
    word = lambda n, s: re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", s, re.I)  # noqa: E731

    def miss_cause(n, f, ln, rt) -> str:
        text = files.text(shown[f], ln)
        if rag_span.get((n, f, files.stmt_start(shown[f], ln))):
            return "found on another line of the statement (multi-line)"
        if n not in vocab:
            return f"symbol not in the index vocabulary ({stype(n)})"
        if text.startswith("$"):
            return "compiler entry on a $ directive line"
        if not word(n, text):
            if re.search(r"\bmacro_\w+\s*\(", text, re.I) or text.lower().startswith("$macro"):
                return "symbol not on the line: reported at the call of a $macro that uses it"
            if re.match(r"model\b.*/\s*all\s*/", text, re.I):
                return "symbol not on the line: model m /all/ (every equation reported)"
            if "%" in text:
                return "symbol not on the line: %-expansion / $batinclude argument"
            return "symbol not on the line (continuation of an earlier statement or other expansion)"
        code = code_part(text)
        if not word(n, code):
            return "symbol only inside a string or after !!"
        return "symbol on the line, no row in the index"

    def fp_cause(u) -> str:
        n, f, ln = u["name"].lower(), u["path"].lower(), u["line"]
        text = files.text(shown[f], ln)
        if n in switch_names and n not in ctype:
            return "main.gms switch used as %name% (not a GAMS symbol, the index tracks switches on purpose)"
        if comp_span.get((n, f, files.stmt_start(shown[f], ln))):
            return "compiler has it on another line of the same statement (multi-line)"
        if re.search(rf"%{re.escape(n)}%", text, re.I) and not word(n, code_part(text).replace("%" + n + "%", "")):
            return "name used as %name% (GAMS text substitution, also declared as a symbol)"
        if not word(n, code_part(text)):
            return "name only inside a string or !! comment"
        if text.startswith("$"):
            return "$ directive line"
        if n not in ctype:
            return "name unknown to the compiler (set-element label equal to a symbol name, or symbol of a non-default realization)"
        if re.search(rf"[\"'][^\"']*\b{re.escape(n)}\b[^\"']*[\"']", text, re.I):
            return "name also appears in a quoted label"
        return "compiler knows the symbol but not on this statement"

    miss_all = [(k, rt) for k, rt in items if k not in rag]
    miss = [(k, rt) for k, rt in miss_all if stype(k[0]) in SCORED_TYPES]
    other = Counter(stype(k[0]) for k, _ in miss_all if stype(k[0]) not in SCORED_TYPES)
    cause = defaultdict(list)
    for (n, f, ln), rt in miss:
        cause[miss_cause(n, f, ln, rt)].append(((n, f, ln), rt))
    n_scored = sum(stype(k[0]) in SCORED_TYPES for k, _ in items)
    w(
        f"\n-- A. compiler entry without an index row on the same line, parameters / variables / equations only:"
        f" {len(miss):,} of {n_scored:,}"
    )
    w(
        f"   (not listed: {sum(other.values()):,} entries of other types, {dict(other.most_common())}; sets are not"
        " tracked as uses, functions / macros / files / models are not symbols of the index)"
    )
    for c, v in sorted(cause.items(), key=lambda kv: -len(kv[1])):
        w(f"  {len(v):6,}  {c}   [{dict(Counter(rt for _, rt in v).most_common())}]")
        for (n, f, ln), rt in v[:ex_n]:
            w(f"           {n} ({stype(n)}) {shown[f]}:{ln}  compiler={rt}  | {files.text(shown[f], ln)[:90]}")

    fps = [u for u in on_compiled if (u["name"].lower(), u["path"].lower(), u["line"]) not in comp]
    cause = defaultdict(list)
    for u in fps:
        cause[fp_cause(u)].append(u)
    w(
        f"\n-- B. index row on a compiled line without a compiler entry on that line: {len(fps):,} of {len(on_compiled):,}"
    )
    for c, v in sorted(cause.items(), key=lambda kv: -len(kv[1])):
        w(f"  {len(v):6,}  {c}   [{dict(Counter(u['role'] for u in v).most_common())}]")
        for u in v[:ex_n]:
            same = (
                "  (compiler has it elsewhere in the statement)"
                if comp_span.get(
                    (u["name"].lower(), u["path"].lower(), files.stmt_start(shown[u["path"].lower()], u["line"]))
                )
                else ""
            )
            w(f"           {u['name']} {u['path']}:{u['line']}  index={u['role']}{same}  | {u['snippet'][:90]}")

    mism = defaultdict(list)
    for (n, f, ln), rts in comp.items():
        hit = rag.get((n, f, ln))
        if hit and rts != {"control"}:
            allowed = set().union(*(allowed_roles(r, ctype.get(n, "")) for r in rts))
            if not any(h["role"] in allowed for h in hit):
                mism["+".join(sorted(rts - {"control"})) + " -> index " + hit[0]["role"]].append((n, f, ln, hit[0]))
    w(f"\n-- C. role mismatch on exact hits: {sum(len(v) for v in mism.values()):,} (compiler reftypes -> index role)")
    for c, v in sorted(mism.items(), key=lambda kv: -len(kv[1]))[:12]:
        w(f"  {len(v):6,}  {c}")
        for n, f, ln, h in v[: min(ex_n, 4)]:
            w(f"           {n} ({stype(n)}) {shown[f]}:{ln}  | {h['snippet'][:90]}")

    # ============================================================ 3. verdict
    w("\n=== 3. DEFAULT VERDICT vs 'the compiler saw this line' ===")
    w(
        "Scored verdict = the compile-time part: realization (module switch value from main.gms) and `$ifthen` conditions"
        "\n(the `conditions` column); both are decided by the compiler's $if/$ifthen/$include handling. `guards` (`if(...)`"
        "\nblocks, `$(...)` conditions) are run-time and are compiled either way: left out here, shown in the second table."
        "\nThe verdict is DefaultRun.use_tags / mark from remind_rag.status (the same code the tools use); the scored"
        "\nvariant calls it with guards removed. mark ✓ = no tag, ⚠ = a tag that keeps the code out of a default run,"
        "\n? = a condition the defaults do not decide. 'compiled' = compiled_line: a symbol entry on the line or an echo of the line in compile.lst."
    )
    run_ = DefaultRun(db)

    def verdict(u, with_guards: bool) -> str:
        row = u if with_guards else {**u, "guards": "[]"}
        return run_.mark(row, run_.use_tags(row))[0]

    for title, wg in (
        ("compile-time verdict (scored)", False),
        ("full verdict as the tools show it (run-time guards included)", True),
    ):
        cnt = Counter()
        for u in in_compiled:
            cnt[verdict(u, wg), covered(u)] += 1
        rows = [(m, cnt[m, True], cnt[m, False], pct(cnt[m, True], cnt[m, True] + cnt[m, False])) for m in "✓⚠?"]
        w_list.clear()
        table(
            f"-- {title}; rows in compiled files",
            rows,
            ("verdict", "compiled", "not compiled", "compiled share"),
            w_list,
        )
        out += w_list
        c = cnt
        w(
            f"   ✓ precision (compiled | ✓) {pct(c['✓', True], c['✓', True] + c['✓', False])};"
            f" ✓ recall (✓ | compiled) {pct(c['✓', True], sum(c[m, True] for m in '✓⚠?'))};"
            f" ⚠ correct (not compiled | ⚠) {pct(c['⚠', False], c['⚠', True] + c['⚠', False])}"
        )

    # rows in .gms files the compiler never entered at all
    nofile = Counter(verdict(u, False) for u in gms_uses if u["path"].lower() not in cfiles)
    w(
        f"\n-- rows in tracked .gms files the compiler never compiled (non-default realizations, unused includes): "
        f"{sum(nofile.values()):,}, compile-time verdict {dict(nofile)}"
    )
    wrong_files = Counter(u["path"] for u in gms_uses if u["path"].lower() not in cfiles and verdict(u, False) == "✓")
    for p, n in wrong_files.most_common(ex_n):
        w(f"     ✓ but file never compiled: {n:4}  {p}")

    # classes of verdict disagreement
    wrong_ok = [u for u in in_compiled if verdict(u, False) == "✓" and not covered(u)]
    missed = [u for u in in_compiled if verdict(u, False) != "✓" and covered(u)]
    cause = defaultdict(list)
    for u in wrong_ok:
        ctx = files.conditional_context(u["path"], u["line"])
        has = json.loads(u["conditions"] or "[]")
        key = ("inside " + ctx.split()[0] if ctx else "no open $if") + (
            ", index lists a condition (judged active)" if has else ", index lists no condition"
        )
        cause[key].append(u)
    w(
        f"\n-- D. wrong ✓: index says runs by default (compile-time), compiler did not see the statement: {len(wrong_ok):,}"
    )
    for c, v in sorted(cause.items(), key=lambda kv: -len(kv[1])):
        w(f"  {len(v):6,}  {c}")
        for u in v[:ex_n]:
            ctx = files.conditional_context(u["path"], u["line"])
            w(f"           {u['name']} {u['path']}:{u['line']}  index={u['role']}  [{ctx[:60]}] | {u['snippet'][:70]}")
    cause = defaultdict(list)
    for u in missed:
        tags = run_.use_tags({**u, "guards": "[]"})
        first = tags[0] if tags else ""
        if first.startswith("$ifthen"):
            cond = first.removeprefix("$ifthen").split(" → ")[0].lower()
            compound = re.search(r"(?<!\w)(and|or)(?!\w)|\(", cond)
            key = "$ifthen, " + ("compound condition (and / or / parentheses)" if compound else "single comparison")
        else:
            key = first.split(" (")[0][:40]
        cause[verdict(u, False) + " " + key].append((u, tags))
    w(f"\n-- E. missed ✓: index says ⚠ or ? (compile-time), compiler read the line: {len(missed):,}")
    w(
        "   (a switch value the index takes from main.gms is overridden before the line, or the index misjudges the condition)"
    )
    for c, v in sorted(cause.items(), key=lambda kv: -len(kv[1])):
        w(f"  {len(v):6,}  {c}   [{dict(Counter(u['role'] for u, _ in v).most_common())}]")
        for u, tags in v[:ex_n]:
            w(
                f"           {u['name']} {u['path']}:{u['line']} [{u['module']}/{u['realization']}]  {tags[0][:90]}  | {u['snippet'][:50]}"
            )

    # ============================================================ headline, placed after the header lines
    def tot(stats, i, j):
        return pct(sum(v[i] for v in stats.values()), sum(v[j] for v in stats.values()))

    rs = recall_rows(lambda n: stype(n) in SCORED_TYPES, lambda n, rt: rt)
    ps = prec(lambda u: stype(u["name"].lower()) in SCORED_TYPES, lambda u: u["role"])
    head_lines = [
        "",
        "=== HEADLINE (parameters / variables / equations in files the index reads; sets and switches left out) ===",
        "recall    (compiler entry -> index row)   exact line / same statement, by compiler reftype:",
    ]
    for k in ("declared", "defined", "assigned", "ref"):
        if k in rs:
            head_lines.append(
                f"    {k:9} {rs[k][0]:6,} entries   {pct(rs[k][1], rs[k][0])} / {pct(rs[k][2], rs[k][0])}"
            )
    head_lines.append(f"    {'all':9} {sum(v[0] for v in rs.values()):6,} entries   {tot(rs, 1, 0)} / {tot(rs, 2, 0)}")
    head_lines.append(
        "precision (index row on a compiled line -> compiler entry)   exact / same statement, by index role:"
    )
    for k in ("declared", "assigned", "equation", "read"):
        if k in ps:
            head_lines.append(
                f"    {k:9} {ps[k][0]:6,} rows      {pct(ps[k][1], ps[k][0])} / {pct(ps[k][2], ps[k][0])}"
            )
    head_lines.append(f"    {'all':9} {sum(v[0] for v in ps.values()):6,} rows      {tot(ps, 1, 0)} / {tot(ps, 2, 0)}")
    head_lines.append(
        f"role agreement on exact hits: {tot(rs, 3, 1)} of compiler entries found, {tot(ps, 4, 3)} of index rows confirmed"
    )
    cnt = Counter((verdict(u, False), covered(u)) for u in in_compiled)
    head_lines += [
        "default verdict, compile-time part (rows in compiled files; compiled = the compiler read the line):",
        f"    ✓ & compiled {cnt['✓', True]:6,}   ✓ & not compiled {cnt['✓', False]:5,}   (wrong ✓)",
        f"    ⚠ & compiled {cnt['⚠', True]:6,}   ⚠ & not compiled {cnt['⚠', False]:5,}   (⚠ & compiled = missed ✓)",
        f"    ? & compiled {cnt['?', True]:6,}   ? & not compiled {cnt['?', False]:5,}",
        f"    rows in .gms files the compiler never compiled: {sum(nofile.values()):,}, of which ✓: {nofile.get('✓', 0):,}",
    ]
    w("\n=== WHAT THE COMPILER VIEW CANNOT JUDGE ===")
    for line in (
        "- Run-time behaviour: `if(...)` branches, `loop` bodies and `$(...)` conditions are compiled either way; whether they",
        "  execute (and which solve runs) is not visible. impl-asn entries are missing (error 257, solve not checked).",
        "- Other configurations: one run, main.gms defaults. A row that is ⚠ in the index but compiled would be a defect; a row",
        "  that is not compiled says nothing about a scenario that switches it on.",
        "- Compiled-line judgement is ours, not the compiler's: a line counts as read if it has a symbol entry, if a code line",
        "  has parameter / variable / equation entries somewhere in its statement, or (directives, labels, comments) if",
        "  compile.lst echoes it; false `$ifthen` lines are judged by their `$endif`. The few cases listed under D and E",
        '  (`$$` double-dollar directives, `$if ... $include "..%var%.."`, `%switch%` data in declarations) are mostly this',
        "  judgement failing, not index defects.",
        "- Data in files the index does not read (.inc, .cs4r) and the missing input data (143 compile errors).",
        "- Set-element lists, descriptions and `%name%` substitutions are not symbol uses for the compiler; the index",
        "  records some of them on purpose (switches).",
    ):
        w(line)
    out[5:5] = head_lines
    return out


if __name__ == "__main__":
    main()
