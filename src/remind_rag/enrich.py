"""Plain-English headers per chunk, so the embedding model sees meaning instead of raw GAMS."""

from __future__ import annotations

import re

from .chunkers import Chunk, Symbol, Switch

IDENT_RE = re.compile(r"\b[A-Za-z]\w*\b")
# sets/aliases are too ubiquitous (t, regi, te, ...) to be informative as "uses"
USE_KINDS = {"scalar", "parameter", "variable", "positive variable", "negative variable",
             "binary variable", "integer variable", "equation", "table"}
MAX_USES = 8


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _location(c: Chunk) -> str:
    if c.module == "core":
        return f"REMIND core, {c.phase} ({c.path})"
    if c.module:
        num, _, name = c.module.partition("_")
        loc = f"Module {num} {name}"
        if c.realization:
            loc += f", realization {c.realization}"
        return f"{loc}, {c.phase} ({c.path})"
    return c.path


def make_header(c: Chunk, sym_desc: dict[str, str], switches: dict[str, Switch]) -> str:
    parts = [_location(c)]
    if c.kind == "equation" and c.name:
        parts.append(f"Equation {c.name}: {_short(sym_desc.get(c.name, ''), 160)}")
    elif c.kind == "declaration":
        parts.append(f"Declarations ({c.detail})")
    elif c.kind == "switch" and c.name:
        sw = switches.get(c.name)
        desc = sym_desc.get(c.name, "")
        line = f"Configuration switch {c.name}"
        if sw:
            line += f" (default: {sw.default}"
            line += f"; allowed: {sw.allowed})" if sw.allowed else ")"
        parts.append(line + (f": {_short(desc, 160)}" if desc else ""))
    elif c.kind == "module_doc":
        parts.append(f"Module description of {c.name}")
    elif c.kind == "realization_doc":
        parts.append(f"Realization description of {c.name}")
    elif c.kind == "md_section":
        parts.append(f"Documentation section: {c.name or ''}")
    elif c.kind == "r_code":
        parts.append("R code" + (f", functions {c.name}" if c.name else ""))

    if c.kind in {"equation", "gams_block", "switch"}:
        seen, uses = {c.name}, []
        for ident in IDENT_RE.findall(c.text):
            if ident not in seen and ident in sym_desc:
                seen.add(ident)
                uses.append(f"{ident} ({_short(sym_desc[ident], 60)})")
                if len(uses) >= MAX_USES:
                    break
        if uses:
            parts.append("Uses: " + "; ".join(uses))
    return "\n".join(parts)


def symbol_descriptions(symbols: list[Symbol]) -> dict[str, str]:
    """name -> first non-empty description, restricted to informative symbol kinds."""
    out: dict[str, str] = {}
    for s in symbols:
        if s.kind in USE_KINDS and s.description and s.name not in out:
            out[s.name] = s.description
    return out
