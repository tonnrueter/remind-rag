"""config/scenario_config*.csv: scenario rows as chunks and switch resolution (copyConfigFrom)."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .chunkers import MAX_CHARS, Chunk


@dataclass
class Scenario:
    """One row of a config/scenario_config*.csv file."""

    name: str
    path: str
    line: int
    settings: dict[str, str]  # non-empty cells after copyConfigFrom resolution (without title/description)
    description: str


def is_magpie_side(path: str) -> bool:
    """A scenario_config_magpie*.csv file: the MAgPIE half of coupled runs (MAgPIE settings only; the REMIND half
    is the row of the same name in a REMIND config, scripts/start/readCheckScenarioConfig.R). Counts of REMIND
    configs leave these rows out, or every one of them would count as "keeps the main.gms default".

    >>> is_magpie_side("config/scenario_config_magpie_RIKEN.csv"), is_magpie_side("config/scenario_config_RIKEN.csv")
    (True, False)
    """
    return "scenario_config_magpie" in path


CFG_ASSIGN_RE = re.compile(r'^cfg\$(\w+)\s*<-\s*"?([^"#\n]*?)"?\s*(?:#.*)?$', re.M)


def cfg_defaults(text: str) -> dict[str, str]:
    """Plain `cfg$name <- value` lines of config/default.cfg: the value a config column takes when a scenario row
    leaves it empty (lower-case name). Nested entries (`cfg$gms$…`, `cfg$files2export$…`) are skipped.

    >>> cfg_defaults('cfg$regionmapping <- "config/regionmappingH12.csv"\\ncfg$gms$x <- 1\\ncfg$slurmConfig <- ""')
    {'regionmapping': 'config/regionmappingH12.csv', 'slurmconfig': ''}
    """
    return {m.group(1).lower(): m.group(2).strip() for m in CFG_ASSIGN_RE.finditer(text)}


def region_label(regionmapping: str) -> str:
    """Short name of a region mapping file, as REMIND people say it.

    >>> region_label("./config/regionmapping_21_EU11.csv"), region_label("config/regionmappingH12.csv")
    ('21_EU11', 'H12')
    """
    return re.sub(r"^.*regionmapping_?|\.csv$", "", regionmapping) or regionmapping


def read_scenarios(root: Path) -> list[Scenario]:
    """Every scenario row of every config/scenario_config*.csv (`;`-separated), with copyConfigFrom resolved
    within its file. Commented-out (`#`) and empty rows are skipped; a scenario defined in several files
    appears once per file."""
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
            cells = {h: v.strip() for h, v in zip(header, row, strict=False) if h and v.strip()}  # rows may be shorter
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
    """One searchable chunk per scenario row: name, description and settings as text."""
    chunks = []
    for sc in scenarios:
        text = f"Scenario {sc.name}\n{sc.description}\nSettings: " + "; ".join(
            f"{k} = {v}" for k, v in sc.settings.items()
        )
        chunks.append(
            Chunk(
                path=sc.path, kind="scenario", line_start=sc.line, line_end=sc.line, text=text[:MAX_CHARS], name=sc.name
            )
        )
    return chunks
