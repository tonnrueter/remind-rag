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
        # camelCase module names are single tokens for BM25 and opaque-ish for embeddings: add plain words
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", name).lower()
        loc = f"Module {num} {name}" + (f" ({words})" if words != name.lower() else "")
        if c.realization:
            loc += f", realization {c.realization}"
        return f"{loc}, {c.phase} ({c.path})"
    return c.path


def make_header(c: Chunk, sym_desc: dict[str, str], switches: dict[str, Switch],
                iface: dict[str, dict[str, list[str]]] | None = None) -> str:
    iface = iface or {}
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
    elif c.kind == "scenario":
        parts.append(f"Scenario configuration {c.name}")
    elif c.kind == "module_interface":
        parts.append(f"Module interfaces (inputs/outputs) of {c.name}")
    elif c.kind == "limitations":
        parts.append(f"Known limitations of {c.name}")

    if c.conditions:
        parts.append("Only compiled if: " + _short(" and ".join(c.conditions), 200))

    if c.kind == "declaration" and iface:
        # keep this short: only the first MAX_EMBED_CHARS of header + text get embedded
        roles = []
        for n in [n for n in IDENT_RE.findall(c.name or "") if n in iface][:4]:
            users = [u for u in iface[n]["consumed_by"] if u != c.module]
            roles.append(f"{n} (used by {', '.join(users) if len(users) <= 2 else f'{len(users)} modules'})")
        if roles:
            parts.append("Interfaces: " + "; ".join(roles))

    if c.kind in {"equation", "gams_block", "switch"}:
        seen, uses = {c.name}, []
        for ident in IDENT_RE.findall(c.text):
            if ident not in seen and ident in sym_desc:
                seen.add(ident)
                src = [m for m in iface.get(ident, {}).get("provided_by", []) if m != c.module]
                uses.append(f"{ident} ({_short(sym_desc[ident], 60)}" + (f"; from {src[0]})" if src else ")"))
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
