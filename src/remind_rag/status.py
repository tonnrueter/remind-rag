"""Does this code run in a default configuration? The main.gms switch values and the verdicts built on them: per
chunk (» lines in search), per use line (✓ / ⚠ / ? in get_symbol, get_switch, get_links and the hierarchy report)."""

from __future__ import annotations

import json
import sqlite3

from .usage import eval_expr, switch_tests

# the marks as Claude reads them in the tool output (the reference test records them)
OFF, DEPENDS, ON = "⚠ not in a default run", "? depends on settings", "✓ runs by default"
TRUTH = {True: "true", False: "false", None: "?"}  # a switch test's value under the defaults; None: undecided


def equation_domain(text: str, name: str) -> str | None:
    r"""The `q(t,regi)$(cond)` part of an equation definition (before `..`), comments dropped, whitespace
    collapsed; None if the name or `..` isn't found.

    >>> equation_domain("* note\nq_x(t)$(t.val ge 2010)..\n  v_x(t) =g= 0;", "q_x")
    'q_x(t)$(t.val ge 2010)'
    """
    code = "\n".join(line for line in text.splitlines() if not line.startswith("*"))
    i = code.lower().find(name.lower())
    j = code.find("..", i)
    return " ".join(code[i:j].split()) if i >= 0 and j > i else None


def off(tag: str) -> bool:
    """A reason that keeps the code out of a default run (the other reasons depend on settings)."""
    return tag.endswith("inactive by default") or tag.startswith(("non-default realization", "module switched off"))


def detail(tags: list[str]) -> str:
    """Reasons under an entry: ⚠ for what keeps it out of a default run, ? for conditions that depend on settings."""
    return "".join(f"\n    {'⚠' if off(t) else '?'} {t}" for t in tags)


def rank(tags: list[str]) -> int:
    """0 runs by default, 1 depends on settings, 2 not in a default run (sort order of links).

    >>> rank([]), rank(["inside if cm_x eq 2: depends on non-default settings"]), rank(["non-default realization"])
    (0, 1, 2)
    """
    if any(off(t) for t in tags):
        return 2
    return 1 if tags else 0


def switch_defaults(db: sqlite3.Connection) -> dict[str, str]:
    """Switch values of a default run as assigned in main.gms, quotes dropped (`""` → empty)."""
    return {r["name"]: (r["value"] or "").strip().strip("\"'") for r in db.execute("SELECT name, value FROM switches")}


def _inactive(verdict: str) -> str:
    # the capitalized INACTIVE is for the callers' checks; in a reason shown to Claude it reads as a plain arrow
    return verdict.replace(": INACTIVE by default", " → inactive by default")


class DefaultRun:
    """Verdicts against the switch values of a default run, as assigned in main.gms. That is the whole default:
    config/default.cfg sets no GAMS switches (they moved to main.gms). Scenario values: search(scenario=...).

    The verdicts are text, and callers test it: use_tags and mark look for "INACTIVE", "NOT DEFAULT" and
    "depends"; keep those words when rewording."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._defaults: dict[str, str] | None = None

    @property
    def defaults(self) -> dict[str, str]:
        """Switch name (as written in main.gms) -> value, quotes dropped; read once per index."""
        if self._defaults is None:
            self._defaults = switch_defaults(self.db)
        return self._defaults

    # ---------------------------------------------------------------- modules and conditions

    def default_realization(self, module: str | None) -> str | None:
        """The realization main.gms selects for a module folder name ("33_carbonRemoval" -> "portfolio"); None
        for core, top-level files and unknown modules."""
        if not module or module == "core":
            return None
        return self.defaults.get(module.partition("_")[2])

    def module_off_by_default(self, module: str | None) -> bool:
        """The default realization is "off" or "none": the module does nothing in a default run."""
        return (self.default_realization(module) or "").lower() in {"off", "none"}

    def realization_status(self, module: str | None, realization: str | None) -> str | None:
        """'realization X: DEFAULT' or 'realization X: NOT DEFAULT (...)' with the switch that selects it; None
        for code outside a realization."""
        default = self.default_realization(module)
        if not realization or not default:
            return None
        if default.lower() == realization.lower():
            return f"realization {realization}: DEFAULT"
        if self.module_off_by_default(module):
            return (
                f"realization {realization}: NOT DEFAULT (module {module} is switched off by default: "
                f"${module.partition('_')[2]} = {default})"
            )
        return f"realization {realization}: NOT DEFAULT (default: {default}; selected by ${module.partition('_')[2]})"

    def condition_status(self, cond: str, what: str) -> str:
        """'<what> <cond>: active by default' / 'INACTIVE by default' / 'depends on non-default settings',
        from eval_expr under the defaults; `what` names the kind of condition ("$ifthen", "inside if", ...)."""
        v = eval_expr(cond, self.defaults)
        label = {True: "active by default", False: "INACTIVE by default"}.get(v, "depends on non-default settings")
        return f"{what} {' '.join(cond.split())}: {label}"

    # ---------------------------------------------------------------- chunks (» lines)

    def chunk_status(self, r: sqlite3.Row, max_tests: int = 4) -> list[str]:
        """Lines saying whether the chunk's code runs in a default configuration: realization, $ifthen
        conditions, and switch tests inside the code (if(...) blocks, $(...) equation domains)."""
        out = []
        if s := self.realization_status(r["module"], r["realization"]):
            out.append(s)
        for c in json.loads(r["conditions"] or "[]"):
            out.append(self.condition_status(c, "compiled only if"))
        if r["kind"] in ("equation", "gams_block", "declaration"):
            tests = switch_tests(r["text"], self.defaults)
            if tests:
                shown = [f"{t} → {TRUTH[v]}" for t, v in tests[:max_tests]]
                more = f" (+{len(tests) - max_tests} more)" if len(tests) > max_tests else ""
                out.append("switch tests in this code (with default values): " + "; ".join(shown) + more)
        return out

    # ---------------------------------------------------------------- use lines (✓ / ⚠ / ?)

    def use_tags(self, u) -> list[str]:
        """Why a use line may not run in a default configuration; [] means it runs. `u` is a symbol_uses row (or
        any mapping with module, realization, conditions, guards). Reasons: a non-default or switched-off
        realization, an $ifthen that is false, an if() guard that is false, or one whose switch tests don't
        decide it (that one depends on settings; see off())."""
        tags = []
        s = self.realization_status(u["module"], u["realization"])
        if s and "NOT DEFAULT" in s:
            default = self.default_realization(u["module"])
            tags.append(
                f"module switched off by default ({default})"
                if self.module_off_by_default(u["module"])
                else f"non-default realization (default: {default})"
            )
        for c in json.loads(u["conditions"] or "[]"):
            if (v := self.condition_status(c, "$ifthen")) and "INACTIVE" in v:
                tags.append(_inactive(v))
        for g in json.loads(u["guards"] or "[]"):
            v = self.condition_status(g, "inside if")
            if "INACTIVE" in v:
                tags.append(_inactive(v))
            # an undecided if() counts only if it tests a switch: one over data (`if(p_x gt 0,`) says nothing
            # about settings, and marking it "?" would flag a large part of REMIND
            elif "depends" in v and any(t for t, _ in switch_tests(g, self.defaults)):
                tags.append(v)
        return tags

    def mark(self, u, tags: list[str]) -> str:
        """The status mark for the first line of an entry: ⚠ not in a default run, ? depends on settings, ✓ default
        realization (code in a module's selected realization) or ✓ runs by default. Always positive or negative,
        never blank: an entry without a mark was read as "unknown" (FINDINGS 10)."""
        if any(off(t) for t in tags):
            return OFF
        if tags:
            return DEPENDS
        if u["realization"] and self.default_realization(u["module"]):
            return "✓ default realization"
        return ON

    def equation_status(self, path: str, equation: str) -> list[str]:
        """[] if the equation's domain $-condition holds in a default run or tests no switch, else the one reason
        ('generated only if ... → inactive by default' or '... depends on non-default settings')."""
        row = self.db.execute(
            "SELECT text FROM chunks WHERE kind = 'equation' AND path = ? AND name = ? COLLATE NOCASE", (path, equation)
        ).fetchone()
        head = equation_domain(row["text"], equation) if row else None
        if not head or "$" not in head:
            return []
        cond = head[head.index("$") + 1 :]  # q_x(t,regi)$(cond): the part after the first $
        if not switch_tests(cond, self.defaults):
            return []
        v = self.condition_status(cond, "generated only if")
        return [] if v.endswith(": active by default") else [_inactive(v)]
