"""Reading GAMS line by line: comments, statement ends, declaration starts, the $ifthen conditions and the
run-time if(...) blocks each line sits in. Shared by the chunker, the where-used table, links and sweeps."""

from __future__ import annotations

import re

DECL_RE = re.compile(
    r"^\s*((?:positive |negative |binary |integer |free |sos[12] |semicont |semiint )?variables?"
    r"|scalars?|parameters?|equations?|sets?|tables?)\b(.*)$",
    re.I,
)
STRING_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
IFTHEN_RE = re.compile(r"^\s*\$ifthen[ei]?(?:\.(\w+))?\s+(.*?)\s*(?:!!.*)?$", re.I)
ELSEIF_RE = re.compile(r"^\s*\$elseif[ei]?(?:\.(\w+))?\s+(.*?)\s*(?:!!.*)?$", re.I)
ELSE_RE = re.compile(r"^\s*\$else(?:\.(\w+))?\s*(?:!!.*)?$", re.I)
ENDIF_RE = re.compile(r"^\s*\$endif(?:\.(\w+))?", re.I)
ONTEXT_RE = re.compile(r"\s*\$ontext\b", re.I)
OFFTEXT_RE = re.compile(r"\s*\$offtext\b", re.I)
IF_OPEN_RE = re.compile(r"if\s*\(", re.I)
WORD_RE = re.compile(r"[A-Za-z_]\w*")


def is_comment(line: str) -> bool:
    return line.startswith("*")


def code_part(line: str) -> str:
    """Line with strings and !! end-of-line comments removed (for ; and / detection)."""
    return STRING_RE.sub("", line).split("!!", 1)[0]


def stmt_end(lines: list[str], i: int) -> int:
    """Index (exclusive) of the line terminating the statement starting at i."""
    for j in range(i, len(lines)):
        if not is_comment(lines[j]) and ";" in code_part(lines[j]):
            return j + 1
    return len(lines)


def decl_start(line: str) -> re.Match | None:
    """DECL_RE match if the line starts a declaration statement ("parameter", "positive variables", ...)."""
    m = DECL_RE.match(line) if not is_comment(line) else None
    # "sets" etc. must be the statement keyword, not e.g. "set_x(t) = ..."
    if m and (not m.group(2) or m.group(2)[:1] in " \t\"'(" or not m.group(2).strip()):
        return m
    return None


def line_conditions(lines: list[str]) -> list[tuple[str, ...]]:
    """For every line, the stack of $ifthen/$elseif/$else conditions it is compiled under.
    An $elseif/$else branch is written as the negation of the earlier branches plus its own condition."""
    stack: list[list[str]] = []  # per open block: conditions of the branches seen so far
    current: list[str] = []  # effective condition per open block
    out: list[tuple[str, ...]] = []
    for line in lines:
        if m := IFTHEN_RE.match(line):
            out.append(tuple(current))
            stack.append([m.group(2)])
            current.append(m.group(2))
            continue
        if stack and (m := ELSEIF_RE.match(line)):
            out.append(tuple(current[:-1]))
            prev = " and ".join(f"not ({c})" for c in stack[-1])
            stack[-1].append(m.group(2))
            current[-1] = f"{prev} and ({m.group(2)})"
            continue
        if stack and ELSE_RE.match(line):
            out.append(tuple(current[:-1]))
            current[-1] = " and ".join(f"not ({c})" for c in stack[-1])
            continue
        if stack and ENDIF_RE.match(line):
            out.append(tuple(current[:-1]))
            stack.pop()
            current.pop()
            continue
        out.append(tuple(current))
    return out


def strip_comments(text: str) -> str:
    """`text` without `*` comment lines, `$ontext` blocks and `!!` end-of-line comments. Unlike `code_part`,
    quoted strings stay: `"%carbonprice%" == "NDC"` is a test."""
    out, in_text = [], False
    for line in text.splitlines():
        if in_text:
            in_text = not OFFTEXT_RE.match(line)
            continue
        if ONTEXT_RE.match(line):
            in_text = True
            continue
        if is_comment(line):
            continue
        cut = STRING_RE.sub(lambda m: "x" * len(m.group()), line).find("!!")
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def _neg(conds: list[str]) -> str:
    return " and ".join(f"not ({c})" for c in conds)


def if_guards(lines: list[str]) -> list[tuple[str, ...]]:
    """For every line, the conditions of the run-time `if(cond, ... elseif cond2, ... else ...)` blocks it
    sits in, outermost first. Later branches are written as the negation of the earlier ones."""
    out: list[tuple[str, ...]] = []
    stack: list[dict] = []  # depth: paren depth inside the if(; conds: branch conditions; reading; current
    depth = 0
    for line in lines:
        active = [b["current"] for b in stack if not b["reading"]]
        if is_comment(line) or line.lstrip().startswith("$"):
            out.append(tuple(active))
            continue
        code = code_part(line)
        k = 0
        while k < len(code):
            ch = code[k]
            top = stack[-1] if stack else None
            if top and top["reading"]:
                if ch == "," and depth == top["depth"]:
                    top["reading"] = False
                    top["conds"][-1] = " ".join(top["conds"][-1].split())
                    prev = _neg(top["conds"][:-1])
                    top["current"] = f"{prev} and ({top['conds'][-1]})" if prev else top["conds"][-1]
                    active.append(top["current"])
                    k += 1
                    continue
                top["conds"][-1] += ch
            elif ch.isalpha() and (k == 0 or not (code[k - 1].isalnum() or code[k - 1] in "_%.")):
                word = WORD_RE.match(code, k).group()
                low = word.lower()
                if low == "if" and IF_OPEN_RE.match(code, k):
                    k = IF_OPEN_RE.match(code, k).end()
                    depth += 1
                    stack.append({"depth": depth, "conds": [""], "reading": True, "current": ""})
                    continue
                if top and depth == top["depth"] and low in ("elseif", "else"):
                    if low == "elseif":
                        top["conds"].append("")
                        top["reading"] = True
                    else:
                        top["current"] = _neg(top["conds"])
                        active.append(top["current"])
                    k += len(word)
                    continue
                k += len(word)
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if top and top["depth"] == depth:
                    stack.pop()
                depth = max(0, depth - 1)
            k += 1
        out.append(tuple(dict.fromkeys(active + [b["current"] for b in stack if not b["reading"]])))
    return out
