"""MCP server (stdio) exposing the REMIND index to Claude Code / opencode.

    uv run --directory <rag> python -m remind_rag.server
    env REMIND_RAG_DB selects the index (default: newest complete data/remind-bge*.db, else remind-jina*.db)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .search import Index, _module_match
from .usage import switch_tests

DATA = Path(__file__).resolve().parents[2] / "data"


def _complete(db_path: Path) -> bool:
    """meta is written last, so an index still being built has no 'built_at' row yet."""
    import sqlite3

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            return db.execute("SELECT 1 FROM meta WHERE key = 'built_at'").fetchone() is not None
    except sqlite3.Error:
        return False


def _default_db() -> Path:
    if env := os.environ.get("REMIND_RAG_DB"):
        return Path(env)
    # newest complete index, bge before jina (bge won the retrieval eval); older versions keep a -vN suffix
    for model in ("bge", "jina"):
        for path in sorted(DATA.glob(f"remind-{model}*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            if _complete(path):
                return path
    raise FileNotFoundError("no index found; run: uv run python -m remind_rag.index --root <remind>")


idx = Index(_default_db())

server = MCPServer(
    name="remind-rag",
    instructions=(
        "Search index over the REMIND model source (GAMS core/modules, main.gms switches, scenario configs, "
        f"R scripts, tutorials). Paths are relative to the REMIND checkout at {idx.root}. "
        "Use get_symbol for exact GAMS identifiers (vm_*, pm_*, q33_*, s_*, ...) incl. which modules provide / "
        "consume them, get_switch for cm_*/c_* switches and module selections, get_module for a module's "
        "realizations, interfaces and limitations, get_scenario for a scenario's settings, and search for "
        "conceptual questions (pass scenario=... only when the question is about a specific scenario). "
        "Results cite path:line; read the file for more context when needed. "
        "Results carry default-status lines (» ... / ⚠ ...): whether the code's realization is the default and "
        "whether its $ifthen / if(...) / $(...) switch conditions hold in a default run. Lead with what a default "
        "run does; describe non-default realizations or inactive branches as alternatives and name the switch "
        "that enables them. get_symbol separates where a symbol is declared, assigned, used in equations and read; "
        "codeCheck's interface owner is the declaring module, not necessarily where values are computed."
    ),
)


def _has_table(name: str) -> bool:
    return idx.db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _description(name: str) -> str:
    row = idx.db.execute("SELECT description FROM symbols WHERE name = ?", (name,)).fetchone()
    return row[0] if row else ""


def _fmt_chunk(r, rank: int | None = None) -> str:
    where = f"{r['path']}:{r['line_start']}-{r['line_end']}"
    head = f"[{rank}] {where}" if rank else where
    status = "".join(f"\n» {s}" for s in idx.default_status(r))
    return f"{head}  ({r['kind']}){status}\n{r['header']}\n```\n{r['text']}\n```"


def _equation_head(text: str, name: str) -> str | None:
    """`q(t,regi)$(cond)` part of an equation definition, comments dropped, whitespace collapsed."""
    code = "\n".join(line for line in text.splitlines() if not line.startswith("*"))
    i = code.lower().find(name.lower())
    j = code.find("..", i)
    return " ".join(code[i:j].split()) if i >= 0 and j > i else None


ROLE_LABELS = {"declared": "Declared", "assigned": "Assigned / fixed / loaded",
               "equation": "Used in equations", "read": "Read"}


def _use_tags(u) -> list[str]:
    """Why a use line may not run in a default configuration."""
    tags = []
    s = idx.realization_status(u["module"], u["realization"])
    if s and "NOT DEFAULT" in s:
        default = idx.default_realization(u["module"])
        tags.append(f"module switched off by default ({default})" if idx.module_off_by_default(u["module"])
                    else f"non-default realization (default: {default})")
    for c in json.loads(u["conditions"] or "[]"):
        if (v := idx.condition_status(c, "$ifthen")) and "INACTIVE" in v:
            tags.append(v.replace(": INACTIVE by default", " → inactive by default"))
    for g in json.loads(u["guards"] or "[]"):
        v = idx.condition_status(g, "inside if")
        if "INACTIVE" in v:
            tags.append(v.replace(": INACTIVE by default", " → inactive by default"))
        elif "depends" in v and any(t for t, _ in switch_tests(g, idx.defaults)):
            tags.append(v)
    return tags


@server.tool()
def search(query: str, k: int = 6, module: str | None = None, realization: str | None = None,
           kind: str | None = None, phase: str | None = None, scenario: str | None = None) -> str:
    """Hybrid (keyword + semantic) search over REMIND code and docs.

    Args:
        query: natural-language question or identifiers.
        k: number of chunks to return (default 6).
        module: restrict to a module, e.g. "33", "carbonRemoval", "33_carbonRemoval" or "core".
        realization: restrict to a realization, e.g. "portfolio", "nash".
        kind: restrict to a chunk kind: equation, declaration, switch, module_doc, realization_doc,
            module_interface, limitations, scenario, gams_block, r_code, md_section.
        phase: restrict to a GAMS phase file: sets, declarations, datainput, equations, preloop, presolve,
            bounds, postsolve, ...
        scenario: a scenario name from config/scenario_config*.csv (e.g. "SSP2-PkBudg1000"). Drops
            realizations the scenario does not select and ranks code compiled out by its switches low.
            Only use it when the question is about that scenario.
    """
    try:
        rows = idx.search(query, k=max(1, min(k, 20)), module=module, realization=realization, kind=kind,
                          phase=phase, scenario=scenario)
    except ValueError as e:
        return str(e)
    if not rows:
        return "No results."
    return "\n\n".join(_fmt_chunk(r, i) for i, r in enumerate(rows, 1))


@server.tool()
def get_symbol(name: str, max_uses: int = 40) -> str:
    """Look up a GAMS symbol (variable, parameter, scalar, equation, set, table) by exact name.

    Returns its declaration(s) with domain and description/unit, which module provides it and which modules
    consume it (gms::codeCheck), the equation definition if it is an equation, and where it is used
    (path:line with the enclosing equation).
    """
    decls = idx.db.execute("SELECT * FROM symbols WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    out = []
    if decls:
        out.append("## Declarations")
        # identical declarations repeated in every realization of a module are listed once
        groups: dict[tuple, list] = {}
        for d in decls:
            groups.setdefault((d["kind"], d["name"], d["domain"], d["description"], d["module"]), []).append(d)
        for (kind, nm, domain, description, module), ds in groups.items():
            first = ds[0]
            where = f"{first['path']}:{first['line']}"
            reals = [d["realization"] for d in ds if d["realization"]]
            loc = (module or "top-level") + (f" (realizations: {', '.join(reals)})" if len(reals) > 1
                                             else f"/{reals[0]}" if reals else "")
            default = idx.default_realization(module)
            note = ("" if not (reals and default and default.lower() not in {x.lower() for x in reals})
                    else f"  — module switched off by default ({default})" if idx.module_off_by_default(module)
                    else f"  — only in non-default realizations (default: {default})")
            out.append(f"- {kind} {nm}({domain}) \"{description}\"  [{loc}] {where}"
                       + (f" (+{len(ds) - 1} more files)" if len(ds) > 1 else "") + note)
    if _has_table("module_interfaces"):
        rows = idx.db.execute("SELECT module, direction FROM module_interfaces WHERE name = ? COLLATE NOCASE",
                              (name,)).fetchall()
        if rows:
            prov = sorted({r["module"] for r in rows if r["direction"] == "out"})
            cons = sorted({r["module"] for r in rows if r["direction"] == "in"})
            # codeCheck's "output" module is the one that declares the object; where values are actually
            # assigned is listed under Uses below
            out.append(f"## Module interface (gms::codeCheck)\n- declared in (interface owner): "
                       f"{', '.join(prov) or '?'}\n- used by: {', '.join(cons) or 'none'}")
        skipped = idx.db.execute("SELECT * FROM not_used WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
        if skipped:
            out.append("## Deliberately not used in (not_used.txt)")
            out += [f"- {r['module']}/{r['realization']}: {r['reason']}" for r in skipped[:20]]
    eqs = idx.db.execute("SELECT * FROM chunks WHERE kind = 'equation' AND name = ? COLLATE NOCASE", (name,)).fetchall()
    for r in eqs:
        head = _equation_head(r["text"], r["name"])
        out.append("## Equation definition\n"
                   + (f"Generated for (domain and $-condition): {head}\n" if head else "") + _fmt_chunk(r))
    uses = idx.db.execute(
        "SELECT * FROM symbol_uses WHERE name = ? COLLATE NOCASE ORDER BY path, line", (name,)
    ).fetchall()
    if uses:
        has_roles = "role" in uses[0].keys()
        groups: dict[str, list] = {}
        for u in uses:
            groups.setdefault(u["role"] if has_roles else "read", []).append(u)
        out.append(f"## Uses ({len(uses)} lines)")
        budget = max_uses
        for role in ("declared", "assigned", "equation", "read"):
            us = groups.get(role, [])
            if not us:
                continue
            mods = sorted({u["module"] or u["path"] for u in us})
            out.append(f"### {ROLE_LABELS[role]} ({len(us)} lines; {', '.join(mods)})")
            # assignments matter most for 'where is it computed', so they get more room
            n = len(us) if role == "assigned" else max(3, min(len(us), budget))
            for u in us[:n]:
                ctx = "/".join(x for x in (u["module"], u["realization"], u["phase"]) if x)
                if u["context"]:
                    ctx += f", in {u['context']}"
                tags = _use_tags(u) if has_roles else []
                out.append(f"- {u['path']}:{u['line']} [{ctx}]  {u['snippet']}"
                           + "".join(f"\n    ⚠ {t}" for t in tags))
            if n < len(us):
                out.append(f"- ... {len(us) - n} more")
            budget = max(0, budget - n)
    if not out:
        similar = idx.db.execute(
            "SELECT DISTINCT name FROM symbols WHERE name LIKE ? LIMIT 15", (f"%{name}%",)
        ).fetchall()
        hint = ", ".join(r[0] for r in similar)
        return f"Symbol {name!r} not found." + (f" Similar: {hint}" if hint else " Try search().")
    return "\n".join(out)


@server.tool()
def get_switch(name: str) -> str:
    """Look up a configuration switch (cm_*, c_*, or a module selection like "carbonprice") in main.gms:
    its documentation, default and allowed values, plus config files / scripts that reference it."""
    sws = idx.db.execute("SELECT * FROM switches WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    if not sws:
        similar = idx.db.execute("SELECT name FROM switches WHERE name LIKE ? LIMIT 15", (f"%{name}%",)).fetchall()
        return f"Switch {name!r} not found in main.gms." + (
            f" Similar: {', '.join(r[0] for r in similar)}" if similar else "")
    out = []
    for sw in sws:
        out.append(f"## {sw['name']}  default: {sw['default_value']}"
                   + (f"  allowed (regexp): {sw['allowed']}" if sw["allowed"] else "")
                   + f"  [{sw['path']}:{sw['line']}]")
        chunk = idx.db.execute("SELECT * FROM chunks WHERE id = ?", (sw["chunk_id"],)).fetchone()
        if chunk:
            out.append(f"```\n{chunk['text']}\n```")
    uses = idx.db.execute(
        "SELECT path, line, snippet FROM symbol_uses WHERE name = ? COLLATE NOCASE AND path != 'main.gms' "
        "ORDER BY path, line LIMIT 40", (name,)
    ).fetchall()
    if uses:
        out.append("## Referenced in")
        out += [f"- {u['path']}:{u['line']}  {u['snippet']}" for u in uses]
    return "\n".join(out)


@server.tool()
def get_module(module: str) -> str:
    """Everything about a module (e.g. "33", "carbonRemoval", "core"): description, realizations with the
    default one, known limitations, and its interfaces (what it provides to / consumes from other modules)."""
    rows = idx.db.execute(
        "SELECT * FROM chunks WHERE kind IN ('module_doc', 'realization_doc', 'limitations') "
        "ORDER BY realization IS NOT NULL, realization, line_start"
    ).fetchall()
    rows = [r for r in rows if _module_match(module, r["module"])]
    is_core = module.lower() == "core"
    if not rows and not is_core:
        return f"Module {module!r} not found."
    mod = "core" if is_core else rows[0]["module"]
    out = []
    if not is_core:
        sel = idx.db.execute("SELECT * FROM switches WHERE name = ? COLLATE NOCASE", (mod.split("_", 1)[1],)).fetchone()
        out.append(f"# {mod}" + (f"  (default realization in main.gms: {sel['value']})" if sel else ""))
    for r in rows:
        default = idx.default_realization(mod)
        marker = ("" if not r["realization"] or not default else
                  "  [DEFAULT]" if r["realization"].lower() == default.lower() else "  [not default]")
        label = {"module_doc": "module", "limitations": f"limitations ({r['realization'] or 'module'})"}.get(
            r["kind"], f"realization {r['realization']}") + marker
        out.append(f"## {label}  {r['path']}:{r['line_start']}\n{r['text'][:1500]}")
    if _has_table("module_interfaces"):
        ifs = idx.db.execute("SELECT name, direction FROM module_interfaces WHERE module = ? ORDER BY name",
                             (mod,)).fetchall()
        for direction, label in (("out", "provides (outputs)"), ("in", "consumes (inputs)")):
            names = [r["name"] for r in ifs if r["direction"] == direction]
            switches = [n for n in names if n.startswith("c")] if direction == "in" else []
            names = [n for n in names if n not in switches]
            if not names and not switches:
                continue
            other = "in" if direction == "out" else "out"
            arrow = "-> used by" if direction == "out" else "<- from"
            lines = [f"## Interfaces: {label}, {len(names) + len(switches)}"]
            for n in names[:80]:
                peers = sorted({r[0] for r in idx.db.execute(
                    "SELECT module FROM module_interfaces WHERE name = ? AND direction = ?", (n, other))} - {mod})
                lines.append(f"- {n}: {_description(n)[:100]}  {arrow} {', '.join(peers) or '-'}")
            if len(names) > 80:
                lines.append(f"- ... {len(names) - 80} more")
            if switches:
                lines.append(f"- switches read: {', '.join(switches)}")
            out.append("\n".join(lines))
    return "\n\n".join(out)


@server.tool()
def get_scenario(name: str) -> str:
    """Settings of a scenario from config/scenario_config*.csv (after copyConfigFrom), and which switches
    and module realizations differ from the main.gms defaults."""
    resolved = idx.scenario_values(name)
    if resolved is None:
        similar = idx.db.execute("SELECT DISTINCT name FROM scenarios WHERE name LIKE ? LIMIT 20",
                                 (f"%{name}%",)).fetchall()
        return f"Scenario {name!r} not found." + (f" Similar: {', '.join(r[0] for r in similar)}" if similar else "")
    _, row = resolved
    settings = json.loads(row["settings"])
    out = [f"# {row['name']}  [{row['path']}:{row['line']}]", row["description"] or "", "## Settings"]
    out += [f"- {k} = {v}" for k, v in settings.items()]
    defaults = {r["name"]: r["value"] for r in idx.db.execute("SELECT name, value FROM switches")}
    changed = [k for k in settings if k in defaults and str(defaults[k]).lower() != str(settings[k]).lower()]
    if changed:
        out.append("## Differs from main.gms")
        out += [f"- {k}: {defaults[k]} -> {settings[k]}" for k in changed]
    others = idx.db.execute("SELECT path FROM scenarios WHERE name = ? COLLATE NOCASE AND path != ?",
                            (row["name"], row["path"])).fetchall()
    if others:
        out.append("(also defined in: " + ", ".join(r[0] for r in others) + ")")
    return "\n".join(out)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
