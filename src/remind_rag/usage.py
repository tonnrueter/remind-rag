"""How a symbol is used on a line (role), which run-time `if(...)` blocks guard a line, and switch tests
evaluated against the main.gms defaults. Used by the indexer (symbol_uses) and the MCP tools."""

from __future__ import annotations

import re

from .gams import STRING_RE, code_part, strip_comments

# ------------------------------------------------------------------ roles

EQ_OPERATOR_RE = re.compile(r"=[elgnxcbELGNXCB]=")
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
    """'assigned' if the line assigns / fixes / loads `name`, else 'read'. Declarations and equation bodies
    are classified by the caller from the chunk kinds."""
    code = code_part(line)
    if LOAD_RE.search(code):
        return "assigned"
    for m in re.finditer(rf"(?<![\w.%]){re.escape(name)}(?!\w)", code, re.I):
        before = code[:m.start()].rstrip()
        # the name must start a statement or a loop/if body (after `;` or `,`); after `(` only in `for (x = ...`,
        # elsewhere `if(x = 2` / `$(x = 2)` is a comparison
        if before and before[-1] not in ";," and not FOR_OPEN_RE.search(before):
            continue
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
            k = _skip_group(code, k) if k < len(code) and code[k] == "(" else k + len(re.match(r"\w*", code[k:]).group())
            while k < len(code) and code[k] == " ":
                k += 1
        if code[k:k + 1] == "=" and code[k:k + 2] != "==" and not EQ_OPERATOR_RE.match(code, k):
            return "assigned"
    return "read"


# ------------------------------------------------------------------ run-time if-blocks


# ------------------------------------------------------------------ switch tests

TEST_RE = re.compile(
    r"(?<![\w%])(c\w*?_\w+|s\d\d_\w+|sm_\w+)\s*(eq|ne|ge|le|gt|lt|<>|>=|<=|>|<|=)(?!=)\s*"
    r"(-?\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'|\w+)", re.I)
STR_TEST_RE = re.compile(r"(not\s+)?[\"']?%(\w+)%[\"']?\s*(==|eq|ne|<>)\s*[\"']?([\w.\-]*)[\"']?", re.I)
OPS = {"eq": "==", "=": "==", "ne": "!=", "<>": "!=", "ge": ">=", ">=": ">=", "le": "<=", "<=": "<=",
       "gt": ">", ">": ">", "lt": "<", "<": "<", "==": "=="}


def _compare(a: str, op: str, b: str) -> bool | None:
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
    don't (`!! for cm_startyear = 2005 ...` is prose, not a test)."""
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
        out.setdefault(f"{name} {m.group(2)} {val}  (default {key} = {defaults[key]})",
                       _compare(defaults[key], op, val))
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


def verdict(results: list[bool | None]) -> str:
    if any(r is False for r in results):
        return "false by default"
    if results and all(r is True for r in results):
        return "true by default"
    return "depends on non-default values"


def eval_expr(cond: str, defaults: dict[str, str]) -> bool | None:
    """Value of a run-time or $ifthen condition under the main.gms defaults: switch tests are replaced by
    their truth values, and the rest must be and/or/not/parentheses. None if anything else remains or the
    result depends on a test that can't be decided."""
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
    for _ in range(5):
        new = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)[A-Za-z_][\w.]*\s*\([^()]*\)", unknown, expr, flags=re.I)
        if new == expr:
            break
        expr = new
    expr = re.sub(r"(?<!\w)[\w.\"']+\s*(?:eq|ne|gt|lt|ge|le|<>|<=|>=|<|>|=)\s*[-\w.\"']+", unknown, expr, flags=re.I)
    expr = re.sub(r"(?<!\w)(?!(?:and|or|not)\b)(?!__t\d)[A-Za-z_]\w*", unknown, expr, flags=re.I)
    if re.search(r"[^\w\s()]", expr):
        return None
    py = re.sub(r"\b(and|or|not)\b", lambda m: m.group(1).lower(), expr, flags=re.I)
    open_ = [i for i, t in enumerate(tokens) if t is None]
    if len(open_) > 6:
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
