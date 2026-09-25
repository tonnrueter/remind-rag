"""Abort preconditions of REMIND realizations: which switch settings a realization (or core) refuses to run with,
checked against the main.gms defaults and against the switches, realizations and values that exist.

Run as a sweep: `uv run python -m remind_rag.preconditions --root ..\\remind` prints every precondition that
can never be met (names a switch or realization that doesn't exist, or a value outside the documented options),
and every realization that aborts under the default settings."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .chunkers import SETGLOBAL_RE, Switch, _code_part, _is_comment, _line_conditions, chunk_gams, path_meta
from .corpus import iter_files
from .scenarios import read_scenarios
from .usage import OPS, STR_TEST_RE, TEST_RE, eval_expr, if_guards

SINGLE_IF_RE = re.compile(r"^\s*\$\$?if[ei]?\s+(.*?)\s+\$?abort\b(.*)$", re.I)
COMPILE_ABORT_RE = re.compile(r"^\s*\$\$?abort\b(.*)$", re.I)
RUNTIME_ABORT_RE = re.compile(r"(?<![\w.$%])abort(?![\w.])\s*(\$\s*(\([^;]*\)|\w+))?", re.I)
SET_TEST_RE = re.compile(r"(not\s+)?\b(set|setglobal)\s+(\w+)", re.I)
MSG_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')
SWITCH_NAME_RE = re.compile(r"^(c|cm|c\d\d)_", re.I)
NONE = "__no_such_value__"  # value of a switch that can't take the tested value


@dataclass
class Test:
    switch: str
    op: str  # as written: ==, eq, ne, ...; 'set' for $if set
    value: str
    negated: bool  # preceded by `not` (string tests only)
    source: str = ""  # 'main.gms:N', 'setglobal in <path>:N', 'declared in <path>:N', 'unknown'
    module: str | None = None  # module selected by this switch (15_climate for %climate%)
    default: str | None = None
    problems: list[str] = field(default_factory=list)  # make the test constant: can't be met / never differs
    notes: list[str] = field(default_factory=list)  # informational


@dataclass
class Precondition:
    path: str
    line: int
    raw: str
    kind: str  # compile | runtime
    form: str  # $if, $ifthen, $abort, if, abort$
    module: str | None
    realization: str | None
    abort_if: list[str]  # all conditions (compile, then run-time) under which the abort is reached
    tests: list[Test]
    message: str
    message_issue: str = ""  # the message names another realization (copied check)
    category: str = "precondition"  # precondition (switch settings) | assertion (data / run-time state)
    default_aborts: bool | None = None  # under the main.gms defaults, with this realization selected
    severity: str = "ok"  # impossible | dead check | suspicious | aborts by default | ok | depends

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


# ------------------------------------------------------------------ context: switches, realizations, values

@dataclass
class Context:
    switches: dict[str, Switch]  # main.gms, lower-case name → Switch
    setglobals: dict[str, str]  # lower-case name → first place it is $setglobal'ed outside main.gms
    declared: dict[str, str]  # lower-case name → first declaration (parameter / scalar / ...)
    modules: dict[str, tuple[str, list[str]]]  # module switch (lower) → (module folder, realizations)
    scenario_values: dict[str, set[str]]  # lower-case switch → values set in config/scenario_config*.csv

    @property
    def defaults(self) -> dict[str, str]:
        return {s.name: s.value.strip().strip("\"'") for s in self.switches.values()}


def load_context(root: Path, files: list[tuple[str, str]]) -> Context:
    switches: dict[str, Switch] = {}
    setglobals: dict[str, str] = {}
    declared: dict[str, str] = {}
    for path, raw in files:
        if not path.endswith(".gms"):
            continue
        parsed = chunk_gams(path, raw)
        if path == "main.gms":
            switches = {s.name.lower(): s for s in parsed.switches}
        for sym in parsed.symbols:
            declared.setdefault(sym.name.lower(), f"{path}:{sym.line}")
        if path != "main.gms":
            for n, line in enumerate(raw.splitlines(), 1):
                if m := SETGLOBAL_RE.match(line):
                    setglobals.setdefault(m.group(1).lower(), f"{path}:{n}")
    modules = {}
    for d in sorted((root / "modules").iterdir()):
        if d.is_dir() and re.match(r"\d\d_", d.name):
            modules[d.name.partition("_")[2].lower()] = (d.name, sorted(x.name for x in d.iterdir() if x.is_dir()))
    values: dict[str, set[str]] = defaultdict(set)
    for sc in read_scenarios(root):
        for k, v in sc.settings.items():
            values[k.lower()].add(v.strip().strip("\"'"))
    return Context(switches, setglobals, declared, modules, dict(values))


# ------------------------------------------------------------------ finding aborts

def _norm(cond: str) -> str:
    """Quotes in GAMS string tests may be ' or "; the shared test regexes expect "."""
    return " ".join(cond.replace("'", '"').split())


def _message(lines: list[str], i: int, start: int) -> str:
    for j in range(i, min(i + 4, len(lines))):
        text = lines[j][start:] if j == i else lines[j]
        if m := MSG_RE.search(text):
            return m.group(1) if m.group(1) is not None else m.group(2)
        if j == i and (rest := text.strip().rstrip(";").strip()):
            return rest  # $abort without quotes
    return ""


def find_aborts(path: str, raw: str) -> list[Precondition]:
    lines = raw.splitlines()
    # $$ifthen / $$abort (dollar-dollar: evaluated when the enclosing text is compiled) count like $ifthen / $abort
    comp = _line_conditions([re.sub(r"^(\s*)\$\$", r"\1$", x) for x in lines])
    guards = if_guards(lines)
    meta = path_meta(path)
    out = []
    for i, line in enumerate(lines):
        if _is_comment(line) or not re.search(r"abort", line, re.I):
            continue
        conds = [_norm(c) for c in comp[i]]
        if m := SINGLE_IF_RE.match(line):
            kind, form, own = "compile", "$if", [_norm(m.group(1))]
            msg = _message(lines, i, m.start(2))
        elif m := COMPILE_ABORT_RE.match(line):
            kind, form, own = "compile", "$ifthen" if conds else "$abort", []
            msg = _message(lines, i, m.start(1))
        elif not line.lstrip().startswith("$") and (m := RUNTIME_ABORT_RE.search(_code_part(line))):
            kind = "runtime"
            own = [_norm(m.group(2).strip())] if m.group(2) else []
            form = "abort$" if own else "if" if guards[i] else "abort"
            conds += [_norm(g) for g in guards[i]]
            msg = _message(lines, i, line.lower().find("abort") + 5)
        else:
            continue
        abort_if = conds + own
        out.append(Precondition(path, i + 1, line.strip(), kind, form, meta["module"], meta["realization"],
                                abort_if, _tests(abort_if), msg))
    return out


def _tests(conds: list[str]) -> list[Test]:
    out: dict[tuple, Test] = {}
    for c in conds:
        for m in STR_TEST_RE.finditer(c):
            t = Test(m.group(2), m.group(3), m.group(4), bool(m.group(1)))
            out.setdefault((t.switch.lower(), t.op, t.value.lower(), t.negated), t)
        rest = STR_TEST_RE.sub(" ", c)
        for m in TEST_RE.finditer(rest):
            t = Test(m.group(1), m.group(2), m.group(3).strip("\"'"), False)
            out.setdefault((t.switch.lower(), t.op, t.value.lower(), False), t)
        for m in SET_TEST_RE.finditer(rest):
            t = Test(m.group(3), "set", "", bool(m.group(1)))
            out.setdefault((t.switch.lower(), "set", "", t.negated), t)
    return list(out.values())


# ------------------------------------------------------------------ checks

def _is_switch(name: str, ctx: Context) -> bool:
    n = name.lower()
    return n in ctx.switches or n in ctx.setglobals or n in ctx.modules or bool(SWITCH_NAME_RE.match(n))


def check_test(t: Test, ctx: Context) -> None:
    n = t.switch.lower()
    if n in ctx.switches:
        s = ctx.switches[n]
        t.source, t.default = f"main.gms:{s.line}", s.value.strip().strip("\"'")
    elif n in ctx.setglobals:
        t.source = f"setglobal in {ctx.setglobals[n]}"
    elif n in ctx.declared:
        t.source = f"declared in {ctx.declared[n]}"
    else:
        t.source = "unknown"
        t.problems.append(f"no switch, $setglobal or declaration named {t.switch}")
        return
    if t.op == "set":
        return
    if n in ctx.modules:
        folder, reals = ctx.modules[n]
        t.module = folder
        if t.value.lower() not in {r.lower() for r in reals}:
            t.problems.append(f"{folder} has no realization {t.value!r} (available: {', '.join(reals)})")
        return
    if n in ctx.switches and OPS.get(t.op.lower()) in ("==", "!="):
        s = ctx.switches[n]
        allowed = (s.allowed or "").strip()
        if allowed and allowed.lower() != "none":
            try:
                ok = re.fullmatch(allowed, t.value, re.I) is not None
            except re.error:
                ok = True
            if not ok:
                t.problems.append(f"{t.value!r} is outside the documented options of {s.name} (regexp = {allowed})")
        used = ctx.scenario_values.get(n, set())
        if t.value.lower() != (t.default or "").lower() and t.value.lower() not in {v.lower() for v in used}:
            t.notes.append(f"{t.value!r} is not the default ({t.default}) and set in no scenario_config*.csv")


def _is_setting(name: str, ctx: Context) -> bool:
    """A switch the user sets (main.gms, $setglobal, module switch), not a scalar computed by the model."""
    n = name.lower()
    if n in ctx.switches or n in ctx.setglobals or n in ctx.modules:
        return True
    return bool(SWITCH_NAME_RE.match(n)) and n not in ctx.declared


def _switch_only(cond: str, ctx: Context) -> bool:
    """True if nothing but switch tests and and/or/not/parentheses is left after removing the switch tests."""
    rest = STR_TEST_RE.sub(lambda m: " " if _is_setting(m.group(2), ctx) else m.group(0), cond)
    rest = TEST_RE.sub(lambda m: " " if _is_setting(m.group(1), ctx) else m.group(0), rest)
    rest = SET_TEST_RE.sub(lambda m: " " if _is_setting(m.group(3), ctx) else m.group(0), rest)
    return not re.sub(r"\b(and|or|not)\b|[()\s]", "", rest, flags=re.I)


MSG_REAL_RE = re.compile(r"module\s+(\w+)\s*=\s*(\w+)|realization\s+(\d\d_\w+)/(\w+)", re.I)


def _message_issue(p: Precondition, ctx: Context) -> str:
    """The abort message names a realization of its own module other than the one it sits in."""
    if not p.realization or not p.module:
        return ""
    num, _, mname = p.module.partition("_")
    for m in MSG_REAL_RE.finditer(p.message):
        mod, real = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if mod.lower() in {num, mname.lower(), p.module.lower()} and real.lower() != p.realization.lower():
            return f"message names {mod}={real}, but the check sits in {p.module}/{p.realization}"
    return ""


def classify(p: Precondition, ctx: Context) -> None:
    for t in p.tests:
        check_test(t, ctx)
    # precondition: decided by switch settings alone; assertion: also depends on data or run-time state
    p.category = "precondition" if p.abort_if and all(_switch_only(c, ctx) for c in p.abort_if) else "assertion"
    p.message_issue = _message_issue(p, ctx)
    cond = " and ".join(f"({c})" for c in p.abort_if)
    if not p.abort_if:
        p.default_aborts = True  # unconditional abort (in this file / branch)
    else:
        p.default_aborts = eval_expr(cond, ctx.defaults)
    if p.category != "precondition":
        p.severity = "-"
        return
    bad = [t for t in p.tests if t.problems]
    if bad:
        # the problematic switch can't take the tested value: decide the condition with all other tests open
        const = eval_expr(cond, {t.switch: NONE for t in bad})
        p.severity = {True: "impossible", False: "dead check"}.get(const, "suspicious")
        return
    p.severity = {True: "aborts by default", False: "ok"}.get(p.default_aborts, "depends")


def sweep(root: Path) -> tuple[list[Precondition], Context]:
    files = [(p, r) for p, r in iter_files(root) if p.endswith(".gms")]
    ctx = load_context(root, files)
    out = []
    for path, raw in files:
        if path.startswith(("modules/", "core/")):
            out += find_aborts(path, raw)
    for p in out:
        classify(p, ctx)
    return out, ctx


def for_realization(pre: list[Precondition], module: str, realization: str | None) -> list[Precondition]:
    """Preconditions of one realization (or of a module's shared files / core with realization=None)."""
    return [p for p in pre if p.module == module and p.realization == realization and p.category == "precondition"]


# ------------------------------------------------------------------ CLI

def _fmt(p: Precondition) -> str:
    probs = "; ".join(x for t in p.tests for x in t.problems)
    notes = "; ".join(x for t in p.tests for x in t.notes)
    cond = " AND ".join(f"({c})" if re.search(r"\bor\b", c, re.I) else c for c in p.abort_if) or "(unconditional)"
    s = f"  [{p.severity}] {p.where} ({p.kind}, {p.form})\n    abort if: {cond}\n    message: {p.message}"
    if probs:
        s += f"\n    problem: {probs}"
    if notes:
        s += f"\n    note: {notes}"
    if p.message_issue:
        s += f"\n    message check: {p.message_issue}"
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="REMIND checkout")
    ap.add_argument("--all", action="store_true", help="also list ok / depends preconditions and assertions")
    args = ap.parse_args()
    pre, _ctx = sweep(args.root)
    by = defaultdict(list)
    for p in pre:
        by[p.severity].append(p)
    kinds = defaultdict(int)
    for p in pre:
        kinds[(p.category, p.kind)] += 1
    print(f"{len(pre)} aborts: " + ", ".join(f"{c}/{k} {n}" for (c, k), n in sorted(kinds.items())))
    print("preconditions by severity: " + ", ".join(f"{s} {len(v)}" for s, v in sorted(by.items()) if s != "-"))
    order = ["impossible", "dead check", "suspicious", "aborts by default"] + (["depends", "ok", "-"] if args.all else [])
    msg = [p for p in pre if p.message_issue]
    if msg:
        print(f"\n## message names another realization ({len(msg)})")
        for p in msg:
            print(f"  {p.where}: {p.message_issue}")
    for sev in order:
        if by[sev]:
            print(f"\n## {sev} ({len(by[sev])})")
            for p in by[sev]:
                print(_fmt(p))


if __name__ == "__main__":
    main()
