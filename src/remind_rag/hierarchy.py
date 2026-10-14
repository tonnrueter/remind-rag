"""Module coupling measured from the index: who owns, who writes and who uses each interface symbol.

    uv run python -m remind_rag.hierarchy [--json out.json]

Prints the tables for MODULE_HIERARCHY docs (team and Claude versions). Every number comes from the index:
the owner of a symbol is gms::codeCheck's interface owner (= declaring module), uses are the symbol_uses rows
(declared / assigned / used in equations / read), each with its default status (✓ runs by default, ? depends on
settings, ⚠ not in a default run) exactly as get_symbol shows it. Regenerate after a rebuild; never hand-edit.

Interface symbols here are vm_ (variables), pm_ (parameters), qm_ (equations) and fm_ (input data). Switches
(cm_) and scalar constants (sm_) are left out: every module reads cm_startyear, which says nothing about coupling.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict

from . import store
from .search import Index
from .status import DefaultRun, rank

PREFIXES = ("vm_", "pm_", "qm_", "fm_")
# phases that run between solves (Nash iterations) or once before them; 'equations' is inside the optimization
BETWEEN = {"presolve", "postsolve", "loop", "solve"}
ONCE = {"datainput", "preloop", "bounds", "sets_calculations", None, "module"}

idx = Index(store.default_db())  # the pinned index (or REMIND_RAG_DB), as the server uses
default_run = DefaultRun(idx.db)


def _owners() -> dict[str, str]:
    rows = idx.db.execute("SELECT name, module FROM module_interfaces WHERE direction = 'out'").fetchall()
    return {r["name"]: r["module"] for r in rows if r["name"].startswith(PREFIXES)}


def _start_value(name: str, snippet: str | None) -> bool:
    """Starting points (.l), marginals (.m) and scaling (.scale) of a variable steer the solver, not the model:
    80_optimization loading vm_cons.l from the input gdx does not make it a writer of vm_cons."""
    return bool(re.search(re.escape(name) + r"\.(l|m|scale)\b", snippet or "", re.I))


def collect() -> dict:
    """Raw material from the index: interface owners, and every non-declaration use of an owned symbol as
    (symbol, owner, module, role, phase, default-status rank), plus the REMIND commit it describes."""
    owners = _owners()
    uses = []  # (symbol, owner, module, role, phase, rank)
    q = (
        f"SELECT * FROM symbol_uses WHERE role != 'declared' AND module IS NOT NULL AND name IN "
        f"({','.join('?' * len(owners))})"
    )
    for u in idx.db.execute(q, list(owners)):
        if u["role"] == "assigned" and _start_value(u["name"], u["snippet"]):
            continue
        uses.append((u["name"], owners[u["name"]], u["module"], u["role"], u["phase"], rank(default_run.use_tags(u))))
    return {
        "owners": owners,
        "uses": uses,
        "commit": idx.meta.get("remind_commit", "?"),
        "branch": idx.meta.get("remind_branch", "?"),
        "built_at": idx.meta.get("built_at", "?"),
    }


def analyse(data: dict, max_rank: int) -> dict:
    """max_rank 0: only uses that run in a default run; 1: plus 'depends on settings'; 2: all code."""
    owners, uses = data["owners"], [u for u in data["uses"] if u[5] <= max_rank]
    # fmt: off
    readers = defaultdict(set)      # symbol -> modules using it (read / in equations / assigned), owner excluded
    eq_readers = defaultdict(set)   # symbol -> modules using it inside equations
    writers = defaultdict(set)      # symbol -> non-owner modules assigning it (incl. bounds / fixing)
    phases = defaultdict(Counter)   # (owner, user) -> phase class counts
    # fmt: on
    for name, owner, mod, role, phase, _ in uses:
        if mod == owner:
            continue
        readers[name].add(mod)
        if role == "equation":
            eq_readers[name].add(mod)
        if role == "assigned":
            writers[name].add(mod)
        cls = "equations" if role == "equation" else "between solves" if phase in BETWEEN else "setup"
        phases[(owner, mod)][cls] += 1

    modules = sorted({o for o in owners.values()} | {u[2] for u in uses})
    owns, reaches, eq_owns, depends = (defaultdict(set) for _ in range(4))
    for name, owner in owners.items():
        owns[owner] |= readers[name]
        eq_owns[owner] |= eq_readers[name]
        for w in writers[name]:
            reaches[w] |= (readers[name] | {owner}) - {w}
        for m in readers[name]:
            depends[m].add(owner)
    for m in modules:
        reaches[m] |= owns[m]

    symbols = []
    for name, owner in owners.items():
        if readers[name]:
            symbols.append(
                {
                    "name": name,
                    "owner": owner,
                    "users": len(readers[name]),
                    "in_equations": len(eq_readers[name]),
                    "writers": sorted(writers[name]),
                }
            )
    symbols.sort(key=lambda s: (-s["users"], s["name"]))

    edges = {(a, b) for (a, b) in phases}
    return {
        "modules": modules,
        "owns": owns,
        "reaches": reaches,
        "eq_owns": eq_owns,
        "depends": depends,
        "symbols": symbols,
        "edges": edges,
        "phases": phases,
        "writers": writers,
        "owners": owners,
        "sccs": _sccs(modules, edges),
        "sccs_without_core": _sccs([m for m in modules if m != "core"], {e for e in edges if "core" not in e}),
    }


def _sccs(nodes: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components (Tarjan): groups of modules that feed each other in a loop."""
    succ = defaultdict(list)
    for a, b in edges:
        succ[a].append(b)
    index, low, stack, on, out, i = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = i[0]
        i[0] += 1
        stack.append(v)
        on.add(v)
        for w in succ[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp))

    for v in nodes:
        if v not in index:
            visit(v)
    return sorted((c for c in out if len(c) > 1), key=len, reverse=True)


def self_test(data: dict) -> None:
    """Anchors that a wrong owner or a wrong default filter would break."""
    a = analyse(data, 0)
    assert a["owners"]["vm_cesIO"] == "01_macro", a["owners"]["vm_cesIO"]
    assert a["owners"]["pm_taxCO2eqSum"] == "46_carbonpriceRegi"
    assert "21_tax" in a["owns"]["46_carbonpriceRegi"]  # pm_taxCO2eqSum -> 21_tax/on (default)
    assert not any(k.startswith(("cm_", "sm_")) for k in a["owners"])
    # 47_regipol writes interface parameters owned elsewhere: its reach must exceed what it owns
    assert len(a["reaches"]["47_regipol"]) >= len(a["owns"]["47_regipol"])


def report(data: dict) -> str:
    """The Markdown tables of MODULE_HIERARCHY: default run, and where it differs, all realizations."""
    d, al = analyse(data, 0), analyse(data, 2)
    out = [
        f"REMIND {data['branch']} @ {data['commit'][:9]}, index built {data['built_at']}",
        f"interface symbols (vm_/pm_/qm_/fm_, codeCheck owner): {len(data['owners'])}; "
        f"used outside their owner in a default run: {len(d['symbols'])}",
    ]
    out.append(
        "\n## Modules (default run | all code)\nmodule | owns | reaches | gap | in equations | depends on"
        " || owns | reaches | depends on"
    )
    rows = sorted(d["modules"], key=lambda m: (-len(d["reaches"][m]), m))
    for m in rows:
        o, r, e, dep = (len(d[k][m]) for k in ("owns", "reaches", "eq_owns", "depends"))
        out.append(
            f"{m} | {o} | {r} | {r - o:+d} | {e} | {dep} || {len(al['owns'][m])} | {len(al['reaches'][m])}"
            f" | {len(al['depends'][m])}"
        )
    out.append(
        "\n## Symbols used by most other modules (default run)\nsymbol | owner | users | in equations | "
        "non-owner writers"
    )
    for s in d["symbols"][:25]:
        out.append(f"{s['name']} | {s['owner']} | {s['users']} | {s['in_equations']} | {', '.join(s['writers'])}")
    out.append("\n## Non-owners writing interface symbols (default run)")
    wr = defaultdict(list)
    for name, ws in d["writers"].items():
        for w in ws:
            wr[w].append(f"{name} ({d['owners'][name]})")
    for w, ns in sorted(wr.items(), key=lambda x: (-len(x[1]), x[0])):  # ties by name: sets have no stable order
        out.append(f"- {w} {len(ns)}: {', '.join(sorted(ns))}")
    out.append("\n## How module pairs couple (default run): uses inside equations / between solves / setup")
    tot = Counter()
    for c in d["phases"].values():
        tot.update({k: 1 for k in c})
    only = Counter("+".join(sorted(c)) for c in d["phases"].values())
    out.append(
        f"module pairs: {len(d['phases'])}; with equation uses {tot['equations']}, between-solve uses "
        f"{tot['between solves']}, setup uses {tot['setup']}"
    )
    out.append("pairs by combination: " + ", ".join(f"{k} {v}" for k, v in only.most_common()))
    out.append("\n## Feedback loops (default run)")
    for label, key in (("with core", "sccs"), ("without core", "sccs_without_core")):
        for c in d[key]:
            out.append(f"- {label}: {len(c)} modules: {', '.join(c)}")
    out.append(
        "\n## Per module (default run): default realization; symbols it owns -> modules using them; "
        "symbols it writes but does not own"
    )
    for m in rows:
        real = "-" if m == "core" else default_run.default_realization(m)
        owned = [f"{s['name']}->{s['users']}" for s in d["symbols"] if s["owner"] == m][:6]
        written = sorted(n for n, ws in d["writers"].items() if m in ws)
        out.append(f"- {m} [{real}] owns: {', '.join(owned) or '-'}; writes: {', '.join(written) or '-'}")
    out.append("\n## Modules nobody else uses (default run)")
    out.append(", ".join(m for m in d["modules"] if not d["reaches"][m]))
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", help="also write the raw analysis (default run) as JSON")
    args = ap.parse_args()
    data = collect()
    self_test(data)
    print(report(data))
    if args.json:
        d = analyse(data, 0)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "meta": {k: data[k] for k in ("commit", "branch", "built_at")},
                    "edges": sorted(map(list, d["edges"])),
                    "symbols": d["symbols"],
                    "modules": {
                        m: {k: sorted(d[k][m]) for k in ("owns", "reaches", "eq_owns", "depends")} for m in d["modules"]
                    },
                },
                f,
                indent=1,
            )


if __name__ == "__main__":
    main()
