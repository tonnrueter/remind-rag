"""How a symbol is used on a line (role), and switch tests and conditions evaluated against switch values
(the main.gms defaults, or a scenario's). Used by the indexer (symbol_uses), the tools and the sweeps."""

from __future__ import annotations

import re

from .gams import STRING_RE, code_part, strip_comments

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
    """'assigned' if the line assigns, fixes, bounds or loads `name`, else 'read'. Only one line is looked at;
    declarations and equation bodies are classified by the caller (index.build_uses) from the chunk kinds.

    >>> role("vm_cap.fx(t,regi,te) = 0;", "vm_cap"), role("p_x(t) = vm_cap.l(t);", "vm_cap")
    ('assigned', 'read')
    >>> role("if(cm_nucscen = 5,", "cm_nucscen")  # `=` inside if( is a comparison
    'read'
    >>> role("p_x(t)$(cm_startyear gt 2005) = 1;", "p_x")  # conditional assignment
    'assigned'
    """
    code = code_part(line)
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
        if k < len(code) and code[k] == "$":  # conditional assignment x(t)$(cond) = ... or x(t) $ (cond) = ...
            k += 1
            while k < len(code) and code[k] == " ":
                k += 1
            k = (
                _skip_group(code, k)
                if k < len(code) and code[k] == "("
                else k + len(re.match(r"\w*", code[k:]).group())
            )
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


def eval_expr(cond: str, defaults: dict[str, str]) -> bool | None:
    """Value of a run-time or $ifthen condition under the switch values in `defaults` (main.gms or a scenario):
    True, False, or None when the switches alone don't decide it. Switch tests become their truth values;
    everything else (indexed symbols, sameas(), other comparisons) becomes an unknown, and the result counts
    only if it is the same for every combination of the unknowns (at most 6 of them).

    >>> d = {"cm_emiscen": "9", "optimization": "nash"}
    >>> eval_expr("cm_emiscen eq 9 and not (%optimization% == 'negishi')", d)
    True
    >>> eval_expr("cm_emiscen eq 6 and sameas(t, '2005')", d)  # false whatever t is
    False
    >>> eval_expr("cm_emiscen eq 9 and sameas(t, '2005')", d) is None  # depends on t
    True
    """
    # Approach: every switch test is replaced by a placeholder __t0, __t1, ... holding its truth value; every
    # other operand becomes a placeholder with value None (unknown). What is left is a Python expression of
    # placeholders, and/or/not and parentheses. It is evaluated for every True/False combination of the
    # unknowns (a truth table): if all combinations agree, the switches decide it.
    tokens: list[bool | None] = []

    def sub(m: re.Match, string: bool) -> str:
        if string:
            neg, name, op, val = m.group(1), m.group(2), OPS[m.group(3).lower()], m.group(4)
        else:
            neg, name, op, val = None, m.group(1), OPS[m.group(2).lower()], m.group(3)
        key = next((k for k in defaults if k.lower() == name.lower()), None)
        r = None if key is None else _compare(defaults[key], op, val)
        if r is not None and neg:
            r = not r
        tokens.append(r)
        return f" __t{len(tokens) - 1} "

    expr = STR_TEST_RE.sub(lambda m: sub(m, True), cond)
    expr = TEST_RE.sub(lambda m: sub(m, False), expr)
    if not tokens:
        return None

    def unknown(_m: re.Match) -> str:
        tokens.append(None)
        return f" __t{len(tokens) - 1} "

    # everything that isn't a switch test becomes an unknown: function calls / indexed symbols, comparisons,
    # bare names. The result is only reported if the switch tests alone decide it.
    for _ in range(5):  # innermost calls first, so nested ones (up to 5 deep) go from the inside out
        new = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)[A-Za-z_][\w.]*\s*\([^()]*\)", unknown, expr, flags=re.I)
        if new == expr:
            break
        expr = new
    expr = re.sub(r"(?<!\w)[\w.\"']+\s*(?:eq|ne|gt|lt|ge|le|<>|<=|>=|<|>|=)\s*[-\w.\"']+", unknown, expr, flags=re.I)
    expr = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)(?!__t\d)[A-Za-z_]\w*", unknown, expr, flags=re.I)
    if re.search(r"[^\w\s()]", expr):  # something left that isn't a placeholder, keyword or parenthesis
        return None
    py = re.sub(r"\b(and|or|not)\b", lambda m: m.group(1).lower(), expr, flags=re.I)
    open_ = [i for i, t in enumerate(tokens) if t is None]
    if len(open_) > 6:  # 2**6 = 64 combinations; with more unknowns the switches rarely decide anyway
        return None
    results = set()
    for mask in range(2 ** len(open_)):
        env = {f"__t{i}": t for i, t in enumerate(tokens)}
        for j, i in enumerate(open_):
            env[f"__t{i}"] = bool(mask >> j & 1)
        try:
            results.add(bool(eval(py, {"__builtins__": {}}, env)))  # noqa: S307 - only and/or/not/parens/names
        except SyntaxError:
            return None
    return results.pop() if len(results) == 1 else None
