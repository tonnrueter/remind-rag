"""Does this code run with given settings (the main.gms defaults, or a scenario's)? The settings and the verdicts
built on them: per chunk (» lines in search), per use line (✓ / ⚠ / ? in get_symbol, get_switch, get_links and the
hierarchy report)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from .conditions import Trees, holds, lower, tests_setting
from .scenarios import is_magpie_side
from .usage import switch_tests

# the marks as Claude reads them in the tool output (the reference test records them)
OFF, DEPENDS, ON = "⚠ not in a default run", "? depends on settings", "✓ runs by default"
TRUTH = {True: "true", False: "false", None: "?"}  # a switch test's value under the defaults; None: undecided


def equation_domain(text: str, name: str) -> str | None:
    r"""The `q(t,regi)$(cond)` part of an equation definition (before `..`), comment lines and `!!` comments
    dropped, whitespace collapsed; None if the name or `..` isn't found.

    >>> equation_domain("* note\nq_x(t)$(t.val ge 2010 !! from 2010 on\n  )..\n  v_x(t) =g= 0;", "q_x")
    'q_x(t)$(t.val ge 2010 )'
    """
    code = "\n".join(line.split("!!", 1)[0] for line in text.splitlines() if not line.startswith("*"))
    i = code.lower().find(name.lower())
    j = code.find("..", i)
    return " ".join(code[i:j].split()) if i >= 0 and j > i else None


def equation_condition(text: str, name: str) -> str | None:
    r"""The $-condition of an equation's domain (`q_x(t,regi)$(cond)..`: the part after the first $), None if it
    has none.

    >>> equation_condition("q_x(t)$(t.val ge 2010)..\n  v_x(t) =g= 0;", "q_x")
    '(t.val ge 2010)'
    """
    head = equation_domain(text, name)
    return head[head.index("$") + 1 :] if head and "$" in head else None


class Reason(NamedTuple):
    """Why a line may not run: `off` if it keeps the code out of a run with the settings, else the settings don't
    decide it."""

    text: str
    off: bool


def detail(tags: list[Reason]) -> str:
    """Reasons under an entry: ⚠ for what keeps it out of a default run, ? for conditions that depend on settings."""
    return "".join(f"\n    {'⚠' if t.off else '?'} {t.text}" for t in tags)


def rank(tags: list[Reason]) -> int:
    """0 runs by default, 1 depends on settings, 2 not in a default run (sort order of links).

    >>> rank([]), rank([Reason("inside if cm_x eq 2: depends", False)]), rank([Reason("non-default realization", True)])
    (0, 1, 2)
    """
    if any(t.off for t in tags):
        return 2
    return 1 if tags else 0


def switch_defaults(db: sqlite3.Connection) -> dict[str, str]:
    """Switch values of a default run as assigned in main.gms, quotes dropped (`""` → empty)."""
    return {r["name"]: (r["value"] or "").strip().strip("\"'") for r in db.execute("SELECT name, value FROM switches")}


def scenario_rows(db: sqlite3.Connection, scenario: str) -> list[sqlite3.Row]:
    """The config rows of that name: config/scenario_config.csv first, the MAgPIE side of a coupled run last."""
    rows = db.execute(
        "SELECT * FROM scenarios WHERE name = ? COLLATE NOCASE ORDER BY path = 'config/scenario_config.csv' DESC,"
        " path, line",
        (scenario,),
    ).fetchall()
    return sorted(rows, key=lambda r: is_magpie_side(r["path"]))


LABEL = {True: "active by default", False: "INACTIVE by default", None: "depends on non-default settings"}


class Settings:
    """Switch and module settings, and the verdicts against them: does a realization, a condition, a line run?

    Settings.defaults(db): the values main.gms assigns. That is the whole default: config/default.cfg sets no GAMS
    switches (they moved to main.gms). settings.scenario(name): those values overridden by the scenario's first
    REMIND row in config/scenario_config*.csv; settings.row(row) for any row.

    Compile-time variables main.gms doesn't define come from the flags table ($set lines elsewhere, followed
    wherever they stand); one defined nowhere stays its own text `%x%`, as in GAMS. Not followed: code that
    reassigns a main.gms switch later (11_aerosols `$setGlobal cm_APssp`)."""

    def __init__(
        self,
        db: sqlite3.Connection,
        values: dict[str, str],
        names: dict[str, str],
        trees: Trees,
        flags: dict[str, list[tuple[str, list[str]]]] | None = None,
        name: str | None = None,
        source: str | None = None,
        config: dict[str, str] | None = None,
    ):
        self.db = db
        self.values = values  # lower-case switch or module name -> value, quotes dropped (as conditions.holds reads)
        self.names = names  # lower-case name -> the spelling in main.gms
        self.trees = trees
        self.flags = flags or {}  # lower-case name -> (value as written, conditions) per line defining it
        self._compile: dict[str, str | None] | None = None
        self.name = name  # the scenario; None for the defaults
        self.source = source  # where its row is: "config/scenario_config.csv:12"
        self.config = config or {}  # the row's other columns (R side: regionmapping, path_gdx_ref, ...)
        # condition -> "holds in N of M REMIND configs", set by the server (status can't read the configs' rows);
        # None (the hierarchy report): reasons without a config count
        self.reach: Callable[[str], str | None] | None = None

    @classmethod
    def defaults(cls, db: sqlite3.Connection) -> Settings:
        """The main.gms values."""
        d = switch_defaults(db)
        flags: dict[str, list[tuple[str, list[str]]]] = {}
        try:
            for r in db.execute("SELECT name, value, conditions FROM flags ORDER BY path, line"):
                flags.setdefault(r["name"].lower(), []).append((r["value"], json.loads(r["conditions"])))
        except sqlite3.OperationalError:  # an index built before the flags table
            pass
        return cls(db, lower(d), lower({k: k for k in d}), Trees(db), flags)

    def scenario(self, name: str) -> Settings:
        """These settings overridden by the first REMIND row of that scenario (column names compared without case
        and surrounding spaces); ValueError if there is none."""
        found = scenario_rows(self.db, name)
        rows = [r for r in found if not is_magpie_side(r["path"])]
        if not rows:
            if found:  # a MAgPIE row's settings are MAgPIE's; they used to be read as REMIND values
                where = ", ".join(f"{r['path']}:{r['line']}" for r in found)
                raise ValueError(f"scenario {name!r} has only MAgPIE rows ({where}): no REMIND settings")
            raise ValueError(f"unknown scenario {name!r}")
        return self.row(rows[0])

    def row(self, row: sqlite3.Row) -> Settings:
        """These settings overridden by one row of the scenarios table."""
        cells = {k.strip().lower(): v.strip().strip("\"'") for k, v in json.loads(row["settings"]).items()}
        s = Settings(
            self.db,
            {k: cells.get(k, v) for k, v in self.values.items()},
            self.names,
            self.trees,
            self.flags,
            row["name"],
            f"{row['path']}:{row['line']}",
            {k: v for k, v in cells.items() if k not in self.values},
        )
        s.reach = self.reach
        return s

    @property
    def compile(self) -> dict[str, str | None]:
        """Compile-time variables (lower-case) -> value: the settings, then each flag main.gms doesn't define:
        the value of the last line whose conditions hold, None if the settings don't decide which (or the value
        is computed, `%1`, `"%cm_GDPpopScen%"`); a flag none of whose lines runs is undefined (left out)."""
        if self._compile is None:
            out: dict[str, str | None] = dict(self.values)
            for name, lines in self.flags.items():
                if name in self.values:
                    continue
                runs = [holds(["and", *(self.trees[c] for c in conds)], self.values, self.values) for _, conds in lines]
                if None in runs:
                    out[name] = None
                elif True in runs:
                    value = [v for (v, _), r in zip(lines, runs, strict=True) if r][-1].strip().strip("\"'")
                    out[name] = None if "%" in value else value
            self._compile = out
        return self._compile

    @property
    def as_written(self) -> dict[str, str]:
        """The values keyed by their main.gms spelling."""
        return {self.names[k]: v for k, v in self.values.items()}

    # ---------------------------------------------------------------- modules and conditions

    def realization(self, module: str | None) -> str | None:
        """The realization selected for a module folder name ("33_carbonRemoval" -> "portfolio"); None for core,
        top-level files and unknown modules."""
        if not module or module == "core":
            return None
        return self.values.get(module.partition("_")[2].lower())

    def module_off(self, module: str | None) -> bool:
        """The selected realization is "off" or "none": the module does nothing."""
        return (self.realization(module) or "").lower() in {"off", "none"}

    def selected(self, module: str | None, realization: str | None) -> bool | None:
        """Whether the realization is the selected one; None for code outside a realization."""
        chosen = self.realization(module)
        if not realization or not chosen:
            return None
        return chosen.lower() == realization.lower()

    def holds(self, cond: str) -> bool | None:
        """The condition's value under these settings: True, False, or None when they don't decide it."""
        return holds(self.trees[cond], self.values, self.compile)

    def realization_status(self, module: str | None, realization: str | None) -> str | None:
        """'realization X: DEFAULT' or 'realization X: NOT DEFAULT (...)' with the switch that selects it; None
        for code outside a realization."""
        sel = self.selected(module, realization)
        if sel is None:
            return None
        if sel:
            return f"realization {realization}: DEFAULT"
        chosen, switch = self.realization(module), module.partition("_")[2]
        if self.module_off(module):
            return (
                f"realization {realization}: NOT DEFAULT (module {module} is switched off by default: "
                f"${switch} = {chosen})"
            )
        return f"realization {realization}: NOT DEFAULT (default: {chosen}; selected by ${switch})"

    def condition_status(self, cond: str, what: str) -> str:
        """'<what> <cond>: active by default' / 'INACTIVE by default' / 'depends on non-default settings';
        `what` names the kind of condition ("$ifthen", "inside if", ...)."""
        return f"{what} {' '.join(cond.split())}: {LABEL[self.holds(cond)]}"

    def reason(self, cond: str, what: str, value: bool | None) -> Reason:
        """A condition that is false (off; with how many scenario configs run the code, R5: ⚠ on a line 242
        configs run read as "unused") or that the settings don't decide."""
        if value is not False:
            return Reason(f"{what} {' '.join(cond.split())}: {LABEL[value]}", False)
        text = f"{what} {' '.join(cond.split())} → inactive by default"
        if self.reach and (r := self.reach(cond)):
            text += f"; {r}"
        return Reason(text, True)

    # ---------------------------------------------------------------- chunks (» lines)

    def chunk_status(self, r: sqlite3.Row, max_tests: int = 4) -> list[str]:
        """Lines saying whether the chunk's code runs with these settings: realization, $ifthen conditions, and
        switch tests inside the code (if(...) blocks, $(...) equation domains)."""
        out = []
        if s := self.realization_status(r["module"], r["realization"]):
            out.append(s)
        for c in json.loads(r["conditions"] or "[]"):
            out.append(self.condition_status(c, "compiled only if"))
        if r["kind"] in ("equation", "gams_block", "declaration"):
            tests = switch_tests(r["text"], self.as_written)
            if tests:
                shown = [f"{t} → {TRUTH[v]}" for t, v in tests[:max_tests]]
                more = f" (+{len(tests) - max_tests} more)" if len(tests) > max_tests else ""
                out.append("switch tests in this code (with default values): " + "; ".join(shown) + more)
        return out

    # ---------------------------------------------------------------- use lines (✓ / ⚠ / ?)

    def use_tags(self, u) -> list[Reason]:
        """Why a use line may not run with these settings; [] means it runs. `u` is a symbol_uses row (or any
        mapping with module, realization, conditions, guards). Reasons: a realization that isn't selected or a
        module switched off, an $ifthen that is false, an if() guard that is false, or one whose switch tests
        don't decide it (that one depends on settings)."""
        tags = []
        if self.selected(u["module"], u["realization"]) is False:
            chosen = self.realization(u["module"])
            text = (
                f"module switched off by default ({chosen})"
                if self.module_off(u["module"])
                else f"non-default realization (default: {chosen})"
            )
            tags.append(Reason(text, True))
        for c in json.loads(u["conditions"] or "[]"):
            # an $ifthen the settings don't decide: "?" (it showed ✓ without a word, TODO 56)
            if (v := self.holds(c)) is not True:
                tags.append(self.reason(c, "$ifthen", v))
        for g in json.loads(u["guards"] or "[]"):
            v = self.holds(g)
            # an undecided if() counts only if it tests a switch: one over data (`if(p_x gt 0,`) says nothing
            # about settings, and marking it "?" would flag a large part of REMIND
            if v is False or (v is None and tests_setting(self.trees[g], self.values)):
                tags.append(self.reason(g, "inside if", v))
        return tags

    def mark(self, u, tags: list[Reason]) -> str:
        """The status mark for the first line of an entry: ⚠ not in a default run, ? depends on settings, ✓ default
        realization (code in a module's selected realization) or ✓ runs by default. Always positive or negative,
        never blank: an entry without a mark was read as "unknown" (FINDINGS 10)."""
        if any(t.off for t in tags):
            return OFF
        if tags:
            return DEPENDS
        if u["realization"] and self.realization(u["module"]):
            return "✓ default realization"
        return ON

    def equation_status(self, path: str, equation: str) -> list[Reason]:
        """[] if the equation's domain $-condition holds with these settings or tests no switch, else the one
        reason ('generated only if ... → inactive by default' or '... depends on non-default settings')."""
        row = self.db.execute(
            "SELECT text FROM chunks WHERE kind = 'equation' AND path = ? AND name = ? COLLATE NOCASE", (path, equation)
        ).fetchone()
        cond = equation_condition(row["text"], equation) if row else None
        if not cond or not tests_setting(self.trees[cond], self.values):
            return []
        v = self.holds(cond)
        return [] if v else [self.reason(cond, "generated only if", v)]
