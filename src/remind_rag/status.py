"""Does this code run in a default configuration? The main.gms switch values and the verdicts built on them: per
chunk (» lines in search), per use line (✓ / ⚠ / ? in get_symbol, get_switch, get_links and the hierarchy report)."""

from __future__ import annotations

import json
import sqlite3

from .usage import eval_expr, switch_tests

OFF, DEPENDS, ON = "⚠ not in a default run", "? depends on settings", "✓ runs by default"


def equation_domain(text: str, name: str) -> str | None:
    """`q(t,regi)$(cond)` part of an equation definition, comments dropped, whitespace collapsed."""
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
    """0 runs by default, 1 depends on settings, 2 not in a default run (sort order of links)."""
    if any(off(t) for t in tags):
        return 2
    return 1 if tags else 0


def switch_defaults(db: sqlite3.Connection) -> dict[str, str]:
    """Switch values of a default run as assigned in main.gms, quotes dropped (`""` → empty)."""
    return {r["name"]: (r["value"] or "").strip().strip("\"'") for r in db.execute("SELECT name, value FROM switches")}


def _inactive(verdict: str) -> str:
    return verdict.replace(": INACTIVE by default", " → inactive by default")


class DefaultRun:
    """Verdicts against the switch values of a default run, as assigned in main.gms (not default.cfg, #27)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self._defaults: dict[str, str] | None = None

    @property
    def defaults(self) -> dict[str, str]:
        if self._defaults is None:
            self._defaults = switch_defaults(self.db)
        return self._defaults

    # ---------------------------------------------------------------- modules and conditions

    def default_realization(self, module: str | None) -> str | None:
        if not module or module == "core":
            return None
        return self.defaults.get(module.partition("_")[2])

    def module_off_by_default(self, module: str | None) -> bool:
        return (self.default_realization(module) or "").lower() in {"off", "none"}

    def realization_status(self, module: str | None, realization: str | None) -> str | None:
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
                shown = [f"{t} → {'true' if v else 'false' if v is False else '?'}" for t, v in tests[:max_tests]]
                more = f" (+{len(tests) - max_tests} more)" if len(tests) > max_tests else ""
                out.append("switch tests in this code (with default values): " + "; ".join(shown) + more)
        return out

    # ---------------------------------------------------------------- use lines (✓ / ⚠ / ?)

    def use_tags(self, u) -> list[str]:
        """Why a use line may not run in a default configuration."""
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
            elif "depends" in v and any(t for t, _ in switch_tests(g, self.defaults)):
                tags.append(v)
        return tags

    def mark(self, u, tags: list[str]) -> str:
        """Default status for the first line of an entry, positive as well as negative: a missing ⚠ alone was read
        as 'unknown' (33_carbonRemoval/portfolio, the default, was called an alternative in a smoke test)."""
        if any(off(t) for t in tags):
            return OFF
        if tags:
            return DEPENDS
        if u["realization"] and self.default_realization(u["module"]):
            return "✓ default realization"
        return ON

    def equation_status(self, path: str, equation: str) -> list[str]:
        """Whether the equation's domain $-condition holds in a default run."""
        row = self.db.execute(
            "SELECT text FROM chunks WHERE kind = 'equation' AND path = ? AND name = ? COLLATE NOCASE", (path, equation)
        ).fetchone()
        head = equation_domain(row["text"], equation) if row else None
        if not head or "$" not in head:
            return []
        cond = head[head.index("$") + 1 :]
        if not switch_tests(cond, self.defaults):
            return []
        v = self.condition_status(cond, "generated only if")
        return [] if v.endswith(": active by default") else [_inactive(v)]
