"""What steers a module's realizations (get_module "Steered by"): abort preconditions, the switches each realization's
code uses (compile-time branches, run-time tests, overwrites, plain reads), and what the scenario configs that select
it set alongside."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from .render import short
from .scenarios import is_magpie_side
from .status import Settings
from .usage import STR_TEST_RE, TEST_RE

SWITCH_REF_RE = re.compile(r"%(\w+)%")
# strongest first: a switch is listed under the first kind it has (a compile-time branch outranks a run-time test, ...)
STEER_KINDS = [
    ("branches", "branches on (compile time, $ifthen)"),
    ("tests", "tests (run time, if / $())"),
    ("sets", "overwrites"),
    ("reads", "reads"),
]
SEVERITY_TEXT = {
    "impossible": "CAN NEVER BE MET",
    "dead check": "check can never fire",
    "suspicious": "names something that doesn't exist",
    "aborts by default": "aborts under the defaults",
    "ok": "met by the defaults",
    "depends": "depends on non-default settings",
}


@dataclass
class Steering:
    """Everything the blocks and alerts of one module are built from."""

    run: Settings  # the defaults or a scenario's
    values: dict[str, str]  # switch (lower case) -> its value under those settings
    reals: list[str]
    default: str  # the realization the settings select
    uses: dict[str | None, dict[str, set[str]]]  # realization (None: module level) -> switch -> kinds
    common: set[str]  # switches every realization with switch uses shares
    active: list[str]  # realizations with switch uses
    preconditions: dict[str | None, list[sqlite3.Row]]  # realization -> abort preconditions
    scens: list[dict[str, str]]  # settings of every REMIND scenario config (MAgPIE side left out)
    chosen: dict[str, list[dict[str, str]]]  # realization -> configs that select it


def collect(db: sqlite3.Connection, run: Settings, mod: str) -> Steering:
    """Gather everything about one module (folder name, e.g. "45_carbonprice") that its blocks and alerts need.
    A switch's kind per realization: set (assigned), tested (in an if / $() test), read, or branched on
    ($ifthen around the code)."""
    values, canon = run.values, run.names
    sel = mod.partition("_")[2].lower()
    reals = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT realization FROM chunks WHERE module = ? AND "
            "realization IS NOT NULL ORDER BY realization COLLATE NOCASE",
            (mod,),
        )
    ]
    uses: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for u in db.execute(
        "SELECT name, realization, role, snippet FROM symbol_uses WHERE module = ? AND path LIKE '%.gms'", (mod,)
    ):
        n = u["name"].lower()
        if n not in values or n == sel:
            continue
        tested = any(m.group(1).lower() == n for m in TEST_RE.finditer(u["snippet"])) or any(
            m.group(2).lower() == n for m in STR_TEST_RE.finditer(u["snippet"])
        )
        kind = "sets" if u["role"] == "assigned" else "tests" if tested else "reads"
        uses[u["realization"]][canon[n]].add(kind)
    # $ifthen lines aren't uses; their switches show up in the conditions of the chunks they enclose
    for c in db.execute(
        "SELECT realization, conditions FROM chunks WHERE module = ? AND conditions IS NOT NULL", (mod,)
    ):
        for cond in json.loads(c["conditions"]):
            for n in SWITCH_REF_RE.findall(cond):
                if n.lower() in values and n.lower() != sel:
                    uses[c["realization"]][canon[n.lower()]].add("branches")
    # off/none realizations use no switches, so "shared" means shared by the ones that use any
    active = [r for r in reals if uses[r]]
    common = set.intersection(*(set(uses[r]) for r in active)) if len(active) > 1 else set()
    preconditions = defaultdict(list)
    for p in db.execute(
        "SELECT * FROM preconditions WHERE module = ? AND category = 'precondition' ORDER BY path, line", (mod,)
    ):
        preconditions[p["realization"]].append(p)
    scens = [json.loads(r[1]) for r in db.execute("SELECT path, settings FROM scenarios") if not is_magpie_side(r[0])]
    default = run.realization(mod) or ""
    # a config selects a realization through the module switch (e.g. carbonprice); left empty, it keeps the default
    chosen = {
        real: [s for s in scens if next((v for k, v in s.items() if k.lower() == sel), default).lower() == real.lower()]
        for real in reals
    }
    return Steering(run, values, reals, default, uses, common, active, preconditions, scens, chosen)


def names_with_defaults(names, values: dict, limit: int = 12) -> str:
    """Switch names, sorted, each with its value (main.gms, or a scenario's): "cm_emiscen (9), cm_startyear (2005)"."""
    shown = [f"{n} ({values[n.lower()]})" for n in sorted(names, key=str.lower)[:limit]]
    return ", ".join(shown) + (f", +{len(names) - limit} more" if len(names) > limit else "")


def condition(p) -> str:
    """When a precondition aborts; nested if/elseif chains are better said by their abort message."""
    cond = " and ".join(json.loads(p["abort_if"])) or "(unconditional)"
    if len(cond) > 120:
        cond = f"{', '.join(json.loads(p['switches']))} are inconsistent" + (
            f' ("{short(p["message"], 120)}")' if p["message"] else ""
        )
    return cond


def severity(s: Steering, p) -> str:
    """A precondition's severity; with a scenario the checks the defaults pass or fail are judged against it
    (what can never fire or never be met stays so)."""
    if not s.run.name or p["severity"] not in ("aborts by default", "ok", "depends"):
        return p["severity"]
    aborts = s.run.holds_all(json.loads(p["abort_if"]))
    return {True: "aborts by default", False: "ok"}.get(aborts, "depends")


def said(s: Steering, text: str) -> str:
    """Severity wording for the settings: "aborts under the defaults" -> "aborts with SSP2-NPi2025 settings"."""
    if not s.run.name:
        return text
    return text.replace("under the defaults", f"with {s.run.label}").replace("by the defaults", f"by {s.run.label}")


def mark(s: Steering, real: str) -> str:
    """[DEFAULT] / [not default], or with a scenario [selected by S] / [not selected]."""
    if real.lower() == s.default.lower():
        return f"selected by {s.run.name}" if s.run.name else "DEFAULT"
    return "not selected" if s.run.name else "not default"


def block(s: Steering, real: str | None, title: str) -> list[str]:
    """The full block of one realization (None: module-level files); [] if there is nothing to say."""
    lines = [title]
    quiet = []
    for p in s.preconditions[real]:
        if (sev := severity(s, p)) in ("ok", "depends"):
            quiet.append((p, sev))
            continue
        sev = said(s, SEVERITY_TEXT.get(sev, sev))
        lines.append(
            f"- requires: aborts if {condition(p)} → {sev}"
            + (f" ({p['problems']})" if p["problems"] else "")
            + f"  [{p['path']}:{p['line']}]"
        )
    if quiet:
        names = sorted({n for p, _ in quiet for n in json.loads(p["switches"])}, key=str.lower)
        where = defaultdict(list)
        for p, _ in quiet:
            where[p["path"].rsplit("/", 1)[-1]].append(str(p["line"]))
        locs = "; ".join(f + ":" + ",".join(ls) for f, ls in where.items())
        ok = sum(sev == "ok" for _, sev in quiet)
        counts = []
        if ok:
            counts.append(said(s, f"{ok} met by the defaults"))
        if len(quiet) > ok:
            counts.append(f"{len(quiet) - ok} depend on other settings")
        lines.append(f"- other abort checks ({', '.join(counts)}) on {', '.join(names)}  [{locs}]")
    own = {n: k for n, k in s.uses[real].items() if n not in s.common}
    # each switch is listed once, under the strongest way the realization uses it
    order = [kind for kind, _ in STEER_KINDS]
    strongest = {n: next(kind for kind in order if kind in kinds) for n, kinds in own.items()}
    for kind, label in STEER_KINDS:
        names = [n for n, k in strongest.items() if k == kind]
        if names:
            lines.append(f"- {label}: {names_with_defaults(names, s.values)}")
    if real is not None:
        named = {n for p in s.preconditions[real] for n in json.loads(p["switches"])}
        lines.append(_in_practice(s, real, set(own) | named))
    return lines if len(lines) > 1 else []


def _in_practice(s: Steering, real: str, relevant: set[str]) -> str:
    """How many scenario configs select the realization, and what they set for its switches."""
    low = {n.lower() for n in relevant}
    counts: dict[str, Counter] = defaultdict(Counter)
    for cfg in s.chosen[real]:
        for k, v in cfg.items():
            if k in relevant or k.lower() in low:
                counts[k][v] += 1
    text = f"- in practice: {len(s.chosen[real])} of {len(s.scens)} scenario configs select it"
    if real.lower() == s.default.lower():
        text += " (incl. those that leave the module switch empty)"
    top = sorted(counts.items(), key=lambda kv: -sum(kv[1].values()))[:8]
    if top:
        text += "; they set " + "; ".join(
            f"{k} = " + ", ".join(f"{short(v, 30)} ({n})" for v, n in c.most_common(3)) for k, c in top
        )
    return text


def alert(s: Steering, real: str) -> str:
    """What the summary line of a realization must say: verdicts that change an answer, plus usage."""
    parts, needs = [], []
    for p in s.preconditions[real]:
        if (sev := severity(s, p)) in ("impossible", "suspicious"):
            parts.append(
                f"⚠ {SEVERITY_TEXT[sev]}: aborts if {condition(p)}" + (f" ({p['problems']})" if p["problems"] else "")
            )
        elif sev == "aborts by default":  # all such checks of a realization in one clause
            needs += [n for n in json.loads(p["switches"]) if n not in needs]
    if needs:
        low = {x.lower() for x in needs}
        n_set = sum(any(k.lower() in low for k in cfg) for cfg in s.chosen[real])
        parts.append(
            said(s, "aborts under the defaults") + f" unless {', '.join(needs)} {'is' if len(needs) == 1 else 'are'}"
            " changed" + (f" (set by {n_set} of {len(s.chosen[real])} selecting configs)" if s.chosen[real] else "")
        )
    parts.append(f"{len(s.chosen[real])} of {len(s.scens)} configs")
    return " · ".join(parts)


def steering(db: sqlite3.Connection, run: Settings, mod: str) -> dict:
    """Per realization the full block (drill-down) and a one-line alert (summary), plus the shared block."""
    s = collect(db, run, mod)
    shared = block(s, None, "### all realizations (module-level files)")
    if s.common:
        shared = shared or ["### all realizations (module-level files)"]
        who = (
            "every realization"
            if len(s.active) == len(s.reals)
            else f"all {len(s.active)} realizations that use switches"
        )
        shared.append(f"- used by {who}: {names_with_defaults(s.common, s.values)}")
    blocks = {}
    for real in s.reals:
        title = f"### {real}  [{mark(s, real)}]"
        blocks[real] = block(s, real, title) or [title, "- no switch uses"]
    return {
        "reals": s.reals,
        "default": s.default,
        "shared": shared,
        "blocks": blocks,
        "alerts": {r: alert(s, r) for r in s.reals},
        "marks": {r: mark(s, r) for r in s.reals},
    }
