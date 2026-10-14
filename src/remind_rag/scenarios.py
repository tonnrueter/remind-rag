"""config/scenario_config*.csv: scenario rows as chunks and switch resolution (copyConfigFrom)."""

from __future__ import annotations

import csv
import io
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
