"""How a symbol is used on a line (role), and the switch tests in code with their values under the main.gms defaults.
Used by the indexer (symbol_uses), the tools and the sweeps; whole conditions are evaluated in conditions.py."""

from __future__ import annotations

import re

from .gams import IF_LINE_RE, STRING_RE, code_part, strip_comments

# ------------------------------------------------------------------ roles

# =e= =l= =g= ...: the relation in an equation body, not an assignment
EQ_OPERATOR_RE = re.compile(r"=[elgnxcbELGNXCB]=")
# a line that loads from a gdx file assigns every symbol on it
LOAD_RE = re.compile(r"\bexecute_load(point)?\b|\$gdxin|\$load", re.I)
FOR_OPEN_RE = re.compile(r"\bfor\s*\($", re.I)


def _skip_group(s: str, k: int) -> int:
    """Index after the balanced (...) group starting at s[k] == '('."""
    depth = 0
    for j in range(k, len(s)):
        depth += s[j] == "("
        depth -= s[j] == ")"
        if depth == 0:
            return j + 1
    return len(s)


def role(line: str, name: str) -> str:
    r"""'assigned' if the line assigns, fixes, bounds or loads `name`, else 'read'. Only one line is looked at;
    declarations and equation bodies are classified by the caller (index.build_uses) from the chunk kinds.

    >>> role("vm_cap.fx(t,regi,te) = 0;", "vm_cap"), role("p_x(t) = vm_cap.l(t);", "vm_cap")
    ('assigned', 'read')
    >>> role("if(cm_nucscen = 5,", "cm_nucscen")  # `=` inside if( is a comparison
    'read'
    >>> role("p_x(t)$(cm_startyear gt 2005) = 1;", "p_x")  # conditional assignment
    'assigned'
    >>> role("vm_cap.lo(t,te) $ pm_histCap(t,te) = 0;", "vm_cap")  # condition without brackets, with an index
    'assigned'
    >>> role("cm_x\t= 0.05;", "cm_x")  # a tab before the =
    'assigned'
    >>> role('$if %cm_rcp_scen% == "none" s15_c = 1;', "s15_c")  # the statement after a one-line $if
    'assigned'
    """
    if m := IF_LINE_RE.match(line):
        line = m.group(2)
    code = code_part(line).replace("\t", " ")
    if LOAD_RE.search(code):
        return "assigned"
    for m in re.finditer(rf"(?<![\w.%]){re.escape(name)}(?!\w)", code, re.I):
        before = code[: m.start()].rstrip()
        # the name must start a statement or a loop/if body (after `;` or `,`); after `(` only in `for (x = ...`,
        # elsewhere `if(x = 2` / `$(x = 2)` is a comparison
        if before and before[-1] not in ";," and not FOR_OPEN_RE.search(before):
            continue
        # step over what may stand between the name and an assignment's `=`: an index (t,regi), a suffix
        # .fx / .lo / .up / .l with its own index, and a condition $(...) or $name
        k = m.end()
        while k < len(code) and code[k] == " ":
            k += 1
        if k < len(code) and code[k] == "(":
            k = _skip_group(code, k)
        if sm := re.match(r"\.(fx|lo|up|l|m|scale|prior)\b", code[k:], re.I):
            k += sm.end()
            if k < len(code) and code[k] == "(":
                k = _skip_group(code, k)
        while k < len(code) and code[k] == " ":
            k += 1
        if k < len(code) and code[k] == "$":  # conditional assignment x(t)$(cond) = ..., x(t) $ p(t) = ...
            k += 1
            while k < len(code) and code[k] == " ":
                k += 1
            if k < len(code) and code[k] == "(":
                k = _skip_group(code, k)
            else:  # $name, possibly with its index: $pm_histCap(t,regi,te)
                k += len(re.match(r"\w*", code[k:]).group())
                while k < len(code) and code[k] == " ":
                    k += 1
                if k < len(code) and code[k] == "(":
                    k = _skip_group(code, k)
            while k < len(code) and code[k] == " ":
                k += 1
        if code[k : k + 1] == "=" and code[k : k + 2] != "==" and not EQ_OPERATOR_RE.match(code, k):
            return "assigned"
    return "read"


# ------------------------------------------------------------------ switch tests

# a comparison with a candidate switch name (cm_*, c_*, module scalars s33_*, sm_*); only names that are
# known switches count, the callers check that
TEST_RE = re.compile(
    r"(?<![\w%])(c\w*?_\w+|s\d\d_\w+|sm_\w+)\s*(eq|ne|ge|le|gt|lt|<>|>=|<=|>|<|=)(?!=)\s*"
    r"(-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'|\w+)",
    re.I,
)
# a string comparison as in $if / $ifthen: "%carbonprice%" == "NDC", optionally negated, quotes optional
STR_TEST_RE = re.compile(r"(not\s+)?[\"']?%(\w+)%[\"']?\s*(==|eq|ne|<>)\s*[\"']?([\w.\-]*)[\"']?", re.I)
# operator spellings in GAMS (eq, =, ...) and $if (==) -> the Python comparison
# fmt: off
OPS = {"eq": "==", "=": "==", "ne": "!=", "<>": "!=", "ge": ">=", ">=": ">=", "le": "<=", "<=": "<=",
       "gt": ">", ">": ">", "lt": "<", "<": "<", "==": "=="}
# fmt: on


def _compare(a: str, op: str, b: str) -> bool | None:
    """a op b, numerically if both are numbers, else as case-blind strings (== / != only, else None)."""
    a, b = a.strip().strip("\"'"), b.strip().strip("\"'")
    try:
        x, y = float(a), float(b)
    except ValueError:
        if op not in ("==", "!="):
            return None
        x, y = a.lower(), b.lower()
    return {"==": x == y, "!=": x != y, ">=": x >= y, "<=": x <= y, ">": x > y, "<": x < y}[op]


def switch_tests(text: str, defaults: dict[str, str]) -> list[tuple[str, bool | None]]:
    """Switch comparisons in the code of `text` (`cm_emiscen eq 6`, `"%carbonprice%" == "NDC"`), each with its
    value under the main.gms defaults (None: not decidable). Only names that are known switches count; comments
    and message strings don't (`!! for cm_startyear = 2005 ...` is prose, not a test). Each test once.

    >>> switch_tests("if(cm_emiscen eq 6, p = 1); !! cm_emiscen = 1 is prose", {"cm_emiscen": "9"})
    [('cm_emiscen eq 6  (default cm_emiscen = 9)', False)]
    """
    text = strip_comments(text)
    # a match that starts inside a string is message text (abort "... requires cm_x = 0 ...")
    in_string = [(s.start(), s.end()) for s in STRING_RE.finditer(text)]
    out: dict[str, bool | None] = {}
    for m in TEST_RE.finditer(text):
        if any(a < m.start() < b for a, b in in_string):
            continue
        name, op, val = m.group(1), OPS[m.group(2).lower()], m.group(3)
        key = next((k for k in defaults if k.lower() == name.lower()), None)
        if key is None:
            continue
        out.setdefault(
            f"{name} {m.group(2)} {val}  (default {key} = {defaults[key]})", _compare(defaults[key], op, val)
        )
    for m in STR_TEST_RE.finditer(text):
        neg, name, op, val = m.group(1), m.group(2), OPS[m.group(3).lower()], m.group(4)
        key = next((k for k in defaults if k.lower() == name.lower()), None)
        if key is None:
            continue
        r = _compare(defaults[key], op, val)
        if r is not None and neg:
            r = not r
        out.setdefault(f"{m.group(0).strip()}  (default {key} = {defaults[key]})", r)
    return list(out.items())
