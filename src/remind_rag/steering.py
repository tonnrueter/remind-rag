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
from .status import DefaultRun
from .usage import STR_TEST_RE, TEST_RE

SWITCH_REF_RE = re.compile(r"%(\w+)%")
# strongest first: a switch is listed under the first kind it has (a compile-time branch outranks a run-time test, ...)
STEER_KINDS = [("branches", "branches on (compile time, $ifthen)"), ("tests", "tests (run time, if / $())"),
               ("sets", "overwrites"), ("reads", "reads")]
SEVERITY_TEXT = {"impossible": "CAN NEVER BE MET", "dead check": "check can never fire",
                 "suspicious": "names something that doesn't exist", "aborts by default": "aborts under the defaults",
                 "ok": "met by the defaults", "depends": "depends on non-default settings"}


@dataclass
class Steering:
    """Everything the blocks and alerts of one module are built from."""
    values: dict[str, str]  # switch (lower case) -> main.gms value
    reals: list[str]
    default: str  # default realization
    uses: dict[str | None, dict[str, set[str]]]  # realization (None: module level) -> switch -> kinds
    common: set[str]  # switches every realization with switch uses shares
    active: list[str]  # realizations with switch uses
    pre: dict[str | None, list[sqlite3.Row]]  # realization -> abort preconditions
    scens: list[dict[str, str]]  # settings of every scenario config
    chosen: dict[str, list[dict[str, str]]]  # realization -> configs that select it


def collect(db: sqlite3.Connection, run: DefaultRun, mod: str) -> Steering:
    values = {k.lower(): v for k, v in run.defaults.items()}
    canon = {k.lower(): k for k in run.defaults}
    sel = mod.partition("_")[2].lower()
    reals = [r[0] for r in db.execute("SELECT DISTINCT realization FROM chunks WHERE module = ? AND "
                                      "realization IS NOT NULL ORDER BY realization COLLATE NOCASE", (mod,))]
    uses: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for u in db.execute("SELECT name, realization, role, snippet FROM symbol_uses WHERE module = ? AND "
                        "path LIKE '%.gms'", (mod,)):
        n = u["name"].lower()
        if n not in values or n == sel:
            continue
        tested = any(m.group(1).lower() == n for m in TEST_RE.finditer(u["snippet"])) or any(
            m.group(2).lower() == n for m in STR_TEST_RE.finditer(u["snippet"]))
        kind = "sets" if u["role"] == "assigned" else "tests" if tested else "reads"
        uses[u["realization"]][canon[n]].add(kind)
    # $ifthen lines aren't uses; their switches show up in the conditions of the chunks they enclose
    for c in db.execute("SELECT realization, conditions FROM chunks WHERE module = ? AND conditions IS NOT NULL",
                        (mod,)):
        for cond in json.loads(c["conditions"]):
            for n in SWITCH_REF_RE.findall(cond):
                if n.lower() in values and n.lower() != sel:
                    uses[c["realization"]][canon[n.lower()]].add("branches")
    # off/none realizations use no switches, so "shared" means shared by the ones that use any
    active = [r for r in reals if uses[r]]
    common = set.intersection(*(set(uses[r]) for r in active)) if len(active) > 1 else set()
    pre = defaultdict(list)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'preconditions'").fetchone():
        for p in db.execute("SELECT * FROM preconditions WHERE module = ? AND category = 'precondition' "
                            "ORDER BY path, line", (mod,)):
            pre[p["realization"]].append(p)
    scens = [json.loads(r[0]) for r in db.execute("SELECT settings FROM scenarios")]
    default = run.default_realization(mod) or ""
    chosen = {real: [s for s in scens if next((v for k, v in s.items() if k.lower() == sel), default).lower()
                     == real.lower()] for real in reals}
    return Steering(values, reals, default, uses, common, active, pre, scens, chosen)


def names_with_defaults(names, values: dict, limit: int = 12) -> str:
    shown = [f"{n} ({values[n.lower()]})" for n in sorted(names, key=str.lower)[:limit]]
    return ", ".join(shown) + (f", +{len(names) - limit} more" if len(names) > limit else "")


def condition(p) -> str:
    """When a precondition aborts; nested if/elseif chains are better said by their abort message."""
    cond = " and ".join(json.loads(p["abort_if"])) or "(unconditional)"
    if len(cond) > 120:
        cond = (f"{', '.join(json.loads(p['switches']))} are inconsistent"
                + (f' ("{short(p["message"], 120)}")' if p["message"] else ""))
    return cond


def block(s: Steering, real: str | None, title: str) -> list[str]:
    """The full block of one realization (None: module-level files); [] if there is nothing to say."""
    lines = [title]
    quiet = []
    for p in s.pre[real]:
        if p["severity"] in ("ok", "depends"):
            quiet.append(p)
            continue
        sev = SEVERITY_TEXT.get(p["severity"], p["severity"])
        lines.append(f"- requires: aborts if {condition(p)} → {sev}"
                     + (f" ({p['problems']})" if p["problems"] else "") + f"  [{p['path']}:{p['line']}]")
    if quiet:
        names = sorted({n for p in quiet for n in json.loads(p["switches"])}, key=str.lower)
        where = defaultdict(list)
        for p in quiet:
            where[p["path"].rsplit("/", 1)[-1]].append(str(p["line"]))
        locs = "; ".join(f + ":" + ",".join(ls) for f, ls in where.items())
        ok = sum(p["severity"] == "ok" for p in quiet)
        counts = ", ".join(x for x in (f"{ok} met by the defaults" if ok else "",
                                       f"{len(quiet) - ok} depend on other settings" if len(quiet) > ok else "")
                           if x)
        lines.append(f"- other abort checks ({counts}) on {', '.join(names)}  [{locs}]")
    own = {n: k for n, k in s.uses[real].items() if n not in s.common}
    for i, (kind, label) in enumerate(STEER_KINDS):
        stronger = {k for k, _ in STEER_KINDS[:i]}
        names = [n for n, ks in own.items() if kind in ks and not ks & stronger]
        if names:
            lines.append(f"- {label}: {names_with_defaults(names, s.values)}")
    if real is not None:
        lines.append(_in_practice(s, real, set(own) | {n for p in s.pre[real] for n in json.loads(p["switches"])}))
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
            f"{k} = " + ", ".join(f"{short(v, 30)} ({n})" for v, n in c.most_common(3)) for k, c in top)
    return text


def alert(s: Steering, real: str) -> str:
    """What the summary line of a realization must say: verdicts that change an answer, plus usage."""
    parts, needs = [], []
    for p in s.pre[real]:
        if p["severity"] in ("impossible", "suspicious"):
            parts.append(f"⚠ {SEVERITY_TEXT[p['severity']]}: aborts if {condition(p)}"
                         + (f" ({p['problems']})" if p["problems"] else ""))
        elif p["severity"] == "aborts by default":  # all such checks of a realization in one clause
            needs += [n for n in json.loads(p["switches"]) if n not in needs]
    if needs:
        low = {x.lower() for x in needs}
        n_set = sum(any(k.lower() in low for k in cfg) for cfg in s.chosen[real])
        parts.append(f"aborts under the defaults unless {', '.join(needs)} {'is' if len(needs) == 1 else 'are'}"
                     " changed" + (f" (set by {n_set} of {len(s.chosen[real])} selecting configs)"
                                   if s.chosen[real] else ""))
    parts.append(f"{len(s.chosen[real])} of {len(s.scens)} configs")
    return " · ".join(parts)


def steering(db: sqlite3.Connection, run: DefaultRun, mod: str) -> dict:
    """Per realization the full block (drill-down) and a one-line alert (summary), plus the shared block."""
    s = collect(db, run, mod)
    shared = block(s, None, "### all realizations (module-level files)")
    if s.common:
        shared = shared or ["### all realizations (module-level files)"]
        who = ("every realization" if len(s.active) == len(s.reals)
               else f"all {len(s.active)} realizations that use switches")
        shared.append(f"- used by {who}: {names_with_defaults(s.common, s.values)}")
    blocks = {}
    for real in s.reals:
        mark = "DEFAULT" if real.lower() == s.default.lower() else "not default"
        blocks[real] = block(s, real, f"### {real}  [{mark}]") or [f"### {real}  [{mark}]", "- no switch uses"]
    return {"reals": s.reals, "default": s.default, "shared": shared, "blocks": blocks,
            "alerts": {r: alert(s, r) for r in s.reals}}
