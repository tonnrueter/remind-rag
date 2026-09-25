"""Mechanical checks of REMIND's prose (comments, descriptions, switch docs) against facts decidable from the code.

Four checks, each deterministic and free:
- unknown names: a symbol or switch name in prose that occurs nowhere in code (renamed or removed symbols);
- stated defaults: "default = X" / "defaults to X" / `!! def = X` vs the value main.gms assigns;
- file pointers: "see modules/.../x.gms" / "happens in core/postsolve.gms": the file must exist and mention the
  switch or symbol the pointer is about;
- realization names: "NN_module/realization" or "module = realization" in prose must name an existing realization.

Run: `uv run python -m remind_rag.doccheck --root ..\\remind`. Semantic mismatches ("says linear, is constant") are
out of scope; they need a reader."""

from __future__ import annotations

import argparse
import difflib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .chunkers import STRING_RE, Switch, chunk_gams, path_meta
from .corpus import iter_files

# REMIND naming: p_, pm_, p45_, v_, vm_, v31_, q_, qm_, s_, sm_, f_, fm_, o_, c_, cm_, c45_, i_, ... (GAMS is case-blind)
NAME_RE = re.compile(r"(?<![\w%$.-])((?:p|pm|v|vm|q|qm|s|sm|f|fm|o|om|c|cm|i|im)\d{0,2}_[A-Za-z]\w{2,})(?![\w-])")
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
FILE_EXT_RE = re.compile(r"\.(cs\dr|csv|inc|gms|gdx|put|txt|R|py|mif|rds)\b", re.I)
PATH_RE = re.compile(r"((?:\./)?(?:modules/)?(?:\d\d_\w+/\w+|core)/\w+\.gms)")
REAL_PATH_RE = re.compile(r"(?<![\w/])(\d\d_[A-Za-z]\w*)/([A-Za-z]\w*)(?![\w.])")
REAL_EQ_RE = re.compile(r"(?:\b(\d\d_[A-Za-z]\w*)|\bmodule\s+(\w+))\s*=\s*([A-Za-z]\w*)\b", re.I)
# "default" needs an explicit connector: "(default 2040", "default = X", "default: X", "defaults to X", "default is X",
# "default value: X", "def = X". "default investment costs" is no claim about a switch value.
DEFAULT_RE = re.compile(
    r"(?:\(\s*default\b\s*[=:]?\s*|\bdefaults?\s*(?:=|:|\bto\b|\bis\b|\bvalue\s*[=:])\s*|\bdef\s*=\s*)"
    r"(\"[^\"]*\"|'[^']*'|[-+]?\d[\d.eE+-]*%?|[A-Za-z][\w.]*)", re.I)
PLACEHOLDER_RE = re.compile(r"xxx|yyy|FIRSTUNIT|SECONDUNIT|^v_var$|^s_GWh_2_EJ$", re.I)  # naming-convention examples
RENAME_LINE_RE = re.compile(r"renam|deprecat|obsolete|no longer|removed|instead", re.I)
# a comment line that is commented-out code, not prose: $include, equation heads, assignments, continuation lines
CODE_COMMENT_RE = re.compile(r"^\*+\s*(\$|\w+(\([^)]*\))?(\.\w+)?\s*(=[^=]|\.\.)|\w+\([^)]*\)\s*($|\")|[-+]\s*\(?\s*\w+\(|.*;\s*$|.*\.\.\s*$)")
MAIN_SWITCH_LINE_RE = re.compile(r"^\s*(?:\$set[gG]lobal\s+(\w+)|(\w+)\s*=)|^\*+'?\s*((?:c|cm|c\d\d)_\w+)", re.I)
STOPWORDS = {"value", "values", "setting", "settings", "the", "a", "an", "is", "in", "for", "and", "or", "setting)",
             "case", "when", "if", "with", "this", "that", "it", "run", "runs", "one", "config", "scenario", "not",
             "to", "of", "as", "on", "by", "be", "should", "can", "will", "same", "from", "all", "used"}


@dataclass
class Finding:
    check: str  # unknown name | stated default | file pointer | realization name
    path: str
    line: int
    claim: str  # the name / value / file as written
    problem: str
    text: str  # the prose line

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


# ------------------------------------------------------------------ prose extraction

def prose_lines(path: str, raw: str) -> list[tuple[int, str, str]]:
    """(line number, prose, code) for every line of a .gms file that carries prose: comment lines, $ontext blocks,
    `!!` end-of-line comments, and description strings of declarations."""
    out = []
    in_text = False
    for n, line in enumerate(raw.splitlines(), 1):
        s = line.lstrip()
        if re.match(r"\$offtext", s, re.I):
            in_text = False
            continue
        if in_text:
            out.append((n, line, ""))
            continue
        if re.match(r"\$ontext", s, re.I):
            in_text = True
            continue
        if line.startswith("*"):
            if not CODE_COMMENT_RE.match(line):  # commented-out code references old names by design
                out.append((n, line, ""))
            continue
        code, _, bang = line.partition("!!")
        prose = [bang] if bang else []
        if not s.startswith("$"):
            # description strings: quoted text with a space and no %switch% (those are string tests)
            prose += [m.group(0)[1:-1] for m in STRING_RE.finditer(code)
                      if " " in m.group(0) and "%" not in m.group(0)]
        if prose:
            out.append((n, "  ".join(prose), STRING_RE.sub('""', code)))
    return out


# ------------------------------------------------------------------ context

@dataclass
class Context:
    root: Path
    code_names: set[str]  # lower-case identifiers in code (GAMS code parts, R, cfg, csv headers)
    file_names: set[str]  # lower-case stems of input files referenced in code ($include p_x.cs4r)
    all_names: dict[str, str]  # lower → as written, for suggestions
    switches: dict[str, Switch]  # main.gms switches, lower-case name
    modules: dict[str, tuple[str, list[str]]]  # lower-case 'NN_name' and 'name' → (folder, realizations)
    files: dict[str, str]  # path → text (.gms only)
    renamed: dict[str, str]  # lower-case name → R line that lists it as renamed / deprecated


def load_context(root: Path) -> Context:
    code_names: set[str] = set()
    file_names: set[str] = set()
    all_names: dict[str, str] = {}
    files: dict[str, str] = {}
    switches: dict[str, Switch] = {}
    renamed: dict[str, str] = {}
    for path, raw in iter_files(root):
        if path.endswith(".md"):
            continue
        if path.endswith(".gms"):
            files[path] = raw
            if path == "main.gms":
                switches = {s.name.lower(): s for s in chunk_gams(path, raw).switches}
            texts = []
            in_text = False
            for line in raw.splitlines():
                s = line.lstrip()
                if re.match(r"\$(on|off)text", s, re.I):
                    in_text = s[1:3].lower() == "on"
                    continue
                if in_text or line.startswith("*"):
                    continue
                code = line.split("!!", 1)[0]
                if s.startswith("$"):
                    code = re.sub(r"\"[^\"]*\"|'[^']*'", lambda m: m.group(0) if "%" in m.group(0) else "", code)
                else:
                    code = STRING_RE.sub(lambda m: m.group(0) if "%" in m.group(0) else "", code)
                texts.append(code)
            text = "\n".join(texts)
        else:
            # R scripts, cfg, csv: any mention counts as use, except in rename / deprecation tables
            kept = []
            for n, line in enumerate(raw.splitlines(), 1):
                if RENAME_LINE_RE.search(line):
                    for name in NAME_RE.findall(line):
                        renamed.setdefault(name.lower(), f"{path}:{n}")
                else:
                    kept.append(line)
            text = "\n".join(kept)
        for m in re.finditer(r"[\w.-]+" + FILE_EXT_RE.pattern, text, re.I):
            file_names.add(m.group(0).rsplit(".", 1)[0].lower())
        text = re.sub(r"[\w.-]+" + FILE_EXT_RE.pattern, " ", text, flags=re.I)
        for ident in IDENT_RE.findall(text):
            code_names.add(ident.lower())
            all_names.setdefault(ident.lower(), ident)
    modules = {}
    for d in sorted((root / "modules").iterdir()):
        if d.is_dir() and re.match(r"\d\d_", d.name):
            reals = sorted(x.name for x in d.iterdir() if x.is_dir() and x.name != "input")
            modules[d.name.lower()] = modules[d.name.partition("_")[2].lower()] = (d.name, reals)
    renamed = {k: v for k, v in renamed.items() if k not in code_names}
    return Context(root, code_names, file_names, all_names, switches, modules, files, renamed)


# ------------------------------------------------------------------ checks

def check_names(path: str, n: int, prose: str, ctx: Context) -> list[Finding]:
    out = []
    for m in NAME_RE.finditer(prose):
        name = m.group(1)
        low = name.lower()
        if low in ctx.code_names or low.rstrip("_") in ctx.code_names:
            continue
        if prose[m.end():m.end() + 1] == "*" or name.endswith("_") or PLACEHOLDER_RE.search(name):
            continue  # wildcard (pm_tax*) or naming-convention example
        if low in ctx.renamed:
            problem = f"listed as renamed or deprecated in {ctx.renamed[low]}"
        elif low in ctx.file_names:
            problem = "occurs in code only as an input file name"
        else:
            problem = "occurs nowhere in code"
        close = difflib.get_close_matches(low, [k for k in ctx.code_names if k[:2] == low[:2]], n=2, cutoff=0.75)
        if close:
            problem += "; closest: " + ", ".join(ctx.all_names[c] for c in close)
        out.append(Finding("unknown name", path, n, name, problem, prose.strip()))
    return out


def _num(f: float) -> str:
    return repr(round(f, 9))


def _norm_value(v: str) -> str:
    v = " ".join(v.strip().strip("\"'").rstrip(".,;:)]").lower().split())
    try:
        return _num(float(v.rstrip("%")) / (100 if v.endswith("%") else 1))
    except ValueError:
        return v


def _same_value(stated: str, sw: Switch) -> bool:
    s = _norm_value(stated)
    vals = {_norm_value(sw.value)}
    try:  # "4.5%" vs 0.045, vs 4.5, and vs a growth factor 1.045
        f = float(sw.value.strip().strip("\"'"))
        vals |= {_num(f), _num(f / 100), _num(f - 1)}
    except ValueError:
        pass
    # "def = GLO 1 i.e. …" for the value "GLO 1"
    return s in vals or re.match(re.escape(_norm_value(sw.value)) + r"(?!\w)", s) is not None


def _clause_matches(text: str, sw: Switch) -> bool:
    """Any value in the clause after `def =` / `default` matches: "def = 6.5$/kg = 0.2 $/Kwh" with value 0.2."""
    clause = re.split(r"[;)]|!!", text, maxsplit=1)[0]
    cands = [_first_value(text), _first_value(clause), clause] + re.findall(r"[-+]?\d[\d.eE+-]*%?", clause)
    return any(_same_value(v, sw) for v in cands if v)


def _allowed(value: str, sw: Switch) -> bool:
    allowed = (sw.allowed or "").strip()
    if not allowed or allowed.lower() == "none":
        return False
    try:
        return re.fullmatch(allowed, value.strip("\"'"), re.I) is not None
    except re.error:
        return False


def _first_value(text: str) -> str:
    m = re.match(r"\s*(\"[^\"]*\"|'[^']*'|\S+)", text)
    return m.group(1) if m else ""


def check_defaults(path: str, n: int, prose: str, code: str, ctx: Context, doc_switch: str | None) -> list[Finding]:
    out = []
    if path == "main.gms" and code and (m := re.match(r"^\s*(\w+)\s*=", code)) and m.group(1).lower() in ctx.switches:
        sw = ctx.switches[m.group(1).lower()]
        if (dm := re.search(r"\bdef\s*=\s*", prose)) and not _clause_matches(prose[dm.end():], sw):
            out.append(Finding("stated default", path, n, "def = " + _first_value(prose[dm.end():]),
                               f"{sw.name} is set to {sw.value.strip()} on this line", prose.strip()))
        return out
    if path == "main.gms" and re.match(r"^\s*\$setglobal", code or "", re.I):
        return out  # $setglobal lines: SETGLOBAL_RE parsing gives default == value; checked via the Switch below
    for dm in DEFAULT_RE.finditer(prose):
        stated = dm.group(1)
        if stated.lower() in STOPWORDS or stated.lower().startswith(("cm_", "c_")):
            continue
        before = prose[:dm.start()]
        # the switch named last before "default" (not a module switch: tax, trade, industry are common words)
        names = [x for x in re.findall(r"\w+", before) if x.lower() in ctx.switches and x.lower() not in ctx.modules]
        # the enclosing switch doc only for explicit `def = X` lines
        name = names[-1] if names else doc_switch if re.match(r"\s*def\b", prose.lstrip("*' ")) else None
        if not name or name.lower() not in ctx.switches:
            continue
        sw = ctx.switches[name.lower()]
        looks_like_value = stated[0] in "\"'" or re.search(r"\d", stated) or stated.lower() in {"on", "off", "none"} \
            or _allowed(stated, sw) \
            or re.match(r"\s*(=|:|to\b)", prose[dm.start():dm.start(1)].lower().split("default")[-1].lstrip("s"))
        if not looks_like_value:
            continue
        if not _clause_matches(prose[dm.start(1):], sw):
            out.append(Finding("stated default", path, n, f"{sw.name} default {stated}",
                               f"main.gms:{sw.line} sets {sw.name} = {sw.value.strip()}", prose.strip()))
    return out


def _resolve(p: str, ctx: Context) -> str | None:
    p = p.removeprefix("./")
    for cand in (p, "modules/" + p):
        if cand in ctx.files:
            return cand
    return None


def check_files(path: str, n: int, prose: str, code: str, ctx: Context, doc_switch: str | None) -> list[Finding]:
    out = []
    for m in PATH_RE.finditer(prose):
        target = _resolve(m.group(1), ctx)
        if target is None:
            out.append(Finding("file pointer", path, n, m.group(1), "no such file", prose.strip()))
            continue
        about = {x for x in NAME_RE.findall(code + " " + prose) if x.lower() in ctx.code_names}
        if not about and doc_switch:
            about = {doc_switch}
        if about:
            text = ctx.files[target].lower()
            if not any(re.search(rf"(?<!\w){re.escape(a.lower())}(?!\w)", text) for a in about):
                out.append(Finding("file pointer", path, n, m.group(1),
                                   f"{target} doesn't mention {', '.join(sorted(about))}", prose.strip()))
    return out


def check_realizations(path: str, n: int, prose: str, ctx: Context) -> list[Finding]:
    out = []
    hits = [(m.group(1), m.group(2)) for m in REAL_PATH_RE.finditer(prose)]
    hits += [(m.group(1) or m.group(2), m.group(3)) for m in REAL_EQ_RE.finditer(prose)
             if (m.group(1) or m.group(2)).lower() in ctx.modules]
    for mod, real in hits:
        if mod.lower() not in ctx.modules:
            if re.match(r"\d\d_", mod):
                out.append(Finding("realization name", path, n, f"{mod}/{real}", "no such module", prose.strip()))
            continue
        folder, reals = ctx.modules[mod.lower()]
        if real.lower() in {r.lower() for r in reals} or real.lower() in {"input", "none", "off", "on"}:
            continue
        if real.lower() in ctx.switches or real.isdigit():
            continue
        out.append(Finding("realization name", path, n, f"{mod}={real}",
                           f"{folder} has no realization {real} (available: {', '.join(reals)})", prose.strip()))
    return out


def _doc_switch(path: str, lines: list[str], n: int, ctx: Context) -> str | None:
    """In main.gms, the switch whose documentation block line n belongs to (nearest switch line above)."""
    if path != "main.gms":
        return None
    for i in range(n, max(0, n - 15), -1):
        m = MAIN_SWITCH_LINE_RE.match(lines[i - 1])
        if m:
            name = next(g for g in m.groups() if g)
            if name.lower() in ctx.switches:
                return name
    return None


def sweep(root: Path) -> list[Finding]:
    ctx = load_context(root)
    out = []
    for path, raw in ctx.files.items():
        if not (path == "main.gms" or path.startswith(("modules/", "core/"))):
            continue
        lines = raw.splitlines()
        for n, prose, code in prose_lines(path, raw):
            doc = _doc_switch(path, lines, n, ctx)
            out += check_names(path, n, prose, ctx)
            out += check_defaults(path, n, prose, code, ctx, doc)
            out += check_files(path, n, prose, code, ctx, doc)
            out += check_realizations(path, n, prose, ctx)
    # $setglobal switches in main.gms: documented default vs assigned value
    for sw in ctx.switches.values():
        if sw.default and sw.value and not _clause_matches(sw.default, sw):
            line = ctx.files["main.gms"].splitlines()[sw.line - 1]
            if not any(f.path == "main.gms" and f.line == sw.line and f.check == "stated default" for f in out):
                out.append(Finding("stated default", "main.gms", sw.line, f"def = {sw.default}",
                                   f"{sw.name} is set to {sw.value.strip()} on this line", line.strip()))
    return out


# ------------------------------------------------------------------ CLI

CHECKS = ["unknown name", "stated default", "file pointer", "realization name"]


def to_markdown(by: dict[str, list[Finding]]) -> str:
    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("`", "'")[:140]
    out = []
    for c in CHECKS:
        rows = sorted(by.get(c, []), key=lambda f: (f.path, f.line))
        out.append(f"### {c} ({len(rows)})\n\n| where | claim | problem | text |\n|---|---|---|---|")
        out += [f"| `{f.where}` | `{cell(f.claim)}` | {cell(f.problem)} | {cell(f.text)} |" for f in rows]
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="REMIND checkout")
    ap.add_argument("--module", help="only files under this path prefix, e.g. modules/45_carbonprice")
    ap.add_argument("--md", type=Path, help="also write the findings as Markdown tables to this file")
    args = ap.parse_args()
    found = sweep(args.root)
    if args.module:
        found = [f for f in found if f.path.startswith(args.module)]
    by = defaultdict(list)
    for f in found:
        by[f.check].append(f)
    if args.md:
        args.md.write_text(to_markdown(by), encoding="utf-8")
    print(f"{len(found)} findings: " + ", ".join(f"{c} {len(by[c])}" for c in CHECKS))
    for c in CHECKS:
        if by[c]:
            print(f"\n## {c} ({len(by[c])})")
            for f in sorted(by[c], key=lambda f: (f.path, f.line)):
                print(f"  {f.where}: {f.claim} — {f.problem}\n      {f.text[:160]}")


if __name__ == "__main__":
    main()
