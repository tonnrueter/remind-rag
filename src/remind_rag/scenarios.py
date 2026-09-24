"""config/scenario_config*.csv: scenario rows as chunks, switch resolution and $ifthen condition evaluation."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .chunkers import MAX_CHARS, Chunk


@dataclass
class Scenario:
    name: str
    path: str
    line: int
    settings: dict[str, str]  # non-empty cells after copyConfigFrom resolution (without title/description)
    description: str


def read_scenarios(root: Path) -> list[Scenario]:
    out: list[Scenario] = []
    for f in sorted((root / "config").glob("scenario_config*.csv")):
        rel = f.relative_to(root).as_posix()
        rows = list(csv.reader(io.StringIO(f.read_text(encoding="utf-8", errors="replace")), delimiter=";"))
        if not rows:
            continue
        header = rows[0]
        resolved: dict[str, dict[str, str]] = {}
        for n, row in enumerate(rows[1:], start=2):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            cells = {h: v.strip() for h, v in zip(header, row) if h and v.strip()}
            name = cells.pop("title", row[0].strip())
            # same semantics as scripts/start/readCheckScenarioConfig.R::copyConfigFrom: fill empty cells
            # from an earlier row of the same file
            src = cells.pop("copyConfigFrom", None)
            if src and src in resolved:
                cells = {**resolved[src], **cells}
            resolved[name] = cells
            desc = cells.get("description", "")
            settings = {k: v for k, v in cells.items() if k != "description"}
            out.append(Scenario(name, rel, n, settings, desc))
    return out


def scenario_chunks(scenarios: list[Scenario]) -> list[Chunk]:
    chunks = []
    for sc in scenarios:
        text = f"Scenario {sc.name}\n{sc.description}\nSettings: " + "; ".join(f"{k} = {v}" for k, v in sc.settings.items())
        chunks.append(Chunk(path=sc.path, kind="scenario", line_start=sc.line, line_end=sc.line,
                            text=text[:MAX_CHARS], name=sc.name))
    return chunks


# ------------------------------------------------------------------ conditions

SIMPLE_COND_RE = re.compile(r'^(not\s+)?"?%(\w+)%"?\s*==\s*"?([^"]*?)"?\s*$', re.I)


def _split_and(cond: str) -> list[str]:
    """Split at top-level ' and ' (outside parentheses)."""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(cond):
        ch = cond[i]
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and cond[i:i + 5].lower() == " and ":
            parts.append(cur)
            cur, i = "", i + 5
            continue
        cur += ch
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def eval_condition(cond: str, values: dict[str, str]) -> bool | None:
    """Evaluate the $ifthen conditions produced by chunkers._line_conditions against switch values.
    Only `[not] "%switch%" == "value"` and and/not/parentheses combinations are understood; anything else
    (or an unknown switch) gives None."""
    parts = _split_and(cond)
    if len(parts) > 1:
        results = [eval_condition(p, values) for p in parts]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    c = parts[0] if parts else ""
    m = re.match(r"^not\s*\((.*)\)$", c, re.I)
    if m:
        r = eval_condition(m.group(1), values)
        return None if r is None else not r
    if c.startswith("(") and c.endswith(")"):
        return eval_condition(c[1:-1], values)
    m = SIMPLE_COND_RE.match(c)
    if not m or m.group(2) not in values:
        return None
    equal = values[m.group(2)].strip().strip('"').lower() == m.group(3).strip().lower()
    return equal != bool(m.group(1))
