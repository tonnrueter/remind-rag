"""Reading GAMS line by line: comments, statement ends, declaration starts, the $ifthen conditions and the
run-time if(...) blocks each line sits in. Shared by the chunker, the where-used table, links and sweeps."""

from __future__ import annotations

import re

# the declaration keywords, singular or plural, variables with their optional type
DECL_RE = re.compile(
    r"^\s*((?:positive |negative |binary |integer |free |sos[12] |semicont |semiint )?variables?"
    r"|scalars?|parameters?|equations?|sets?|tables?)\b(.*)$",
    re.I,
)
STRING_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
# $ifthen / $ifthene (evaluates numbers) / $iftheni (ignores case), optionally tagged ($ifthen.label) so nested
# blocks can be closed by name; a trailing !! comment is not part of the condition
IFTHEN_RE = re.compile(r"^\s*\$ifthen[ei]?(?:\.(\w+))?\s+(.*?)\s*(?:!!.*)?$", re.I)
ELSEIF_RE = re.compile(r"^\s*\$elseif[ei]?(?:\.(\w+))?\s+(.*?)\s*(?:!!.*)?$", re.I)
ELSE_RE = re.compile(r"^\s*\$else(?:\.(\w+))?\s*(?:!!.*)?$", re.I)
ENDIF_RE = re.compile(r"^\s*\$endif(?:\.(\w+))?", re.I)
ONTEXT_RE = re.compile(r"\s*\$ontext\b", re.I)
OFFTEXT_RE = re.compile(r"\s*\$offtext\b", re.I)
IF_OPEN_RE = re.compile(r"if\s*\(", re.I)
WORD_RE = re.compile(r"[A-Za-z_]\w*")


def is_comment(line: str) -> bool:
    """A GAMS comment line: `*` in the first column (indented `*` is code, e.g. a multiplication).

    >>> is_comment("* carbon price path"), is_comment("  * p_x")
    (True, False)
    """
    return line.startswith("*")


def code_part(line: str) -> str:
    """The line without quoted strings and without its `!!` end-of-line comment, for finding `;` and `/`.

    >>> code_part('p_x = 1; !! "a;b" is not code')
    'p_x = 1; '
    >>> code_part('display "a;b";')
    'display ;'
    """
    return STRING_RE.sub("", line).split("!!", 1)[0]


def stmt_end(lines: list[str], i: int) -> int:
    """Index one past the line whose `;` ends the statement starting at line i (len(lines) if none does).

    >>> stmt_end(["p_x(t)", "* comment;", "  = 1", ";", "p_y = 2;"], 0)
    4
    """
    for j in range(i, len(lines)):
        if not is_comment(lines[j]) and ";" in code_part(lines[j]):
            return j + 1
    return len(lines)


def decl_start(line: str) -> re.Match | None:
    """The DECL_RE match if the line starts a declaration statement, else None. group(1) is the keyword
    ("parameters", "positive variables", ...), group(2) the rest of the line.

    >>> decl_start("positive variables vm_cap(t)").group(1)
    'positive variables'
    >>> decl_start("sets_x(t) = yes;") is None  # a name starting with "sets", not the keyword
    True
    """
    m = DECL_RE.match(line) if not is_comment(line) else None
    # "sets" etc. must be the statement keyword, not e.g. "set_x(t) = ..."
    if m and (not m.group(2) or m.group(2)[:1] in " \t\"'(" or not m.group(2).strip()):
        return m
    return None


def line_conditions(lines: list[str]) -> list[tuple[str, ...]]:
    """For every line, the $ifthen conditions it is compiled under, outermost first. An $elseif / $else branch
    is written as the negation of the earlier branches plus its own condition. The directive lines themselves
    get the conditions around their block.

    >>> line_conditions(['$ifthen "%x%" == "a"', "p = 1;", "$else", "p = 2;", "$endif"])
    [(), ('"%x%" == "a"',), (), ('not ("%x%" == "a")',), ()]
    """
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
    r"""`text` without `*` comment lines, `$ontext` blocks and `!!` end-of-line comments. Unlike `code_part`,
    quoted strings stay: `"%carbonprice%" == "NDC"` is a test.

    >>> print(strip_comments('* note\n$ontext\nprose\n$offtext\np = 1; !! tail\ns = "a !! b";'))
    p = 1;
    s = "a !! b";
    """
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
        # strings masked with x's of the same length: a "!!" inside quotes is not a comment, and positions stay
        cut = STRING_RE.sub(lambda m: "x" * len(m.group()), line).find("!!")
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def _neg(conds: list[str]) -> str:
    return " and ".join(f"not ({c})" for c in conds)


def if_guards(lines: list[str]) -> list[tuple[str, ...]]:
    """For every line, the conditions of the run-time `if(cond, ... elseif cond2, ... else ...)` blocks it
    sits in, outermost first. Later branches are written as the negation of the earlier ones. Comments,
    strings and `$` directives don't open or close blocks. A line where the branch changes (`else`, `elseif`)
    carries the conditions of both branches; so does everything on a one-line `if(c, a; else b;);`.

    >>> if_guards(["if(cm_x eq 1,", "  p = 1;", "else", "  p = 2;", ");", "q = 1;"])
    [('cm_x eq 1',), ('cm_x eq 1',), ('cm_x eq 1', 'not (cm_x eq 1)'), ('not (cm_x eq 1)',), ('not (cm_x eq 1)',), ()]
    """
    # Walks the code character by character, counting parentheses. An `if(` opens a block; its condition is
    # read up to the first comma at the block's own depth, and the matching `)` closes the block. Per open
    # block: depth = paren depth of its `if(`, conds = the conditions of its branches so far, reading = still
    # inside a condition, current = the condition of the branch the code is in now.
    out: list[tuple[str, ...]] = []
    stack: list[dict] = []
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
            # a word start, but not inside a name, a %switch% or a .suffix: "p_if", "%else%", "x.elseif" aren't keywords
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
        # conditions active at the start of the line plus those opened on it; dict.fromkeys drops repeats, keeps order
        out.append(tuple(dict.fromkeys(active + [b["current"] for b in stack if not b["reading"]])))
    return out
