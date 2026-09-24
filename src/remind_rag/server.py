"""MCP server (stdio) exposing the REMIND index to Claude Code / opencode.

    uv run --directory <rag> python -m remind_rag.server
    env REMIND_RAG_DB selects the index (default: newest complete data/remind-bge*.db, else remind-jina*.db)
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .search import Index, _module_match

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
        "Search index over the REMIND model source (GAMS core/modules, main.gms switches, R scripts, tutorials). "
        f"Paths are relative to the REMIND checkout at {idx.root}. "
        "Use get_symbol for exact GAMS identifiers (vm_*, pm_*, q33_*, s_*, ...), get_switch for cm_*/c_* "
        "configuration switches and module selections, list_realizations for a module's variants, and search for "
        "conceptual questions. Results cite path:line; read the file for more context when needed."
    ),
)


def _fmt_chunk(r, rank: int | None = None) -> str:
    where = f"{r['path']}:{r['line_start']}-{r['line_end']}"
    head = f"[{rank}] {where}" if rank else where
    return f"{head}  ({r['kind']})\n{r['header']}\n```\n{r['text']}\n```"


@server.tool()
def search(query: str, k: int = 6, module: str | None = None, realization: str | None = None,
           kind: str | None = None) -> str:
    """Hybrid (keyword + semantic) search over REMIND code and docs.

    Args:
        query: natural-language question or identifiers.
        k: number of chunks to return (default 6).
        module: restrict to a module, e.g. "33", "carbonRemoval", "33_carbonRemoval" or "core".
        realization: restrict to a realization, e.g. "portfolio", "nash".
        kind: restrict to a chunk kind: equation, declaration, switch, module_doc, realization_doc,
            gams_block, r_code, md_section.
    """
    rows = idx.search(query, k=max(1, min(k, 20)), module=module, realization=realization, kind=kind)
    if not rows:
        return "No results."
    return "\n\n".join(_fmt_chunk(r, i) for i, r in enumerate(rows, 1))


@server.tool()
def get_symbol(name: str, max_uses: int = 40) -> str:
    """Look up a GAMS symbol (variable, parameter, scalar, equation, set, table) by exact name.

    Returns its declaration(s) with domain and description/unit, the equation definition if it is an
    equation, and where it is used (path:line, grouped by module/phase).
    """
    decls = idx.db.execute("SELECT * FROM symbols WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    out = []
    if decls:
        out.append("## Declarations")
        for d in decls:
            loc = d["module"] or "top-level"
            if d["realization"]:
                loc += f"/{d['realization']}"
            out.append(f"- {d['kind']} {d['name']}({d['domain']}) \"{d['description']}\"  "
                       f"[{loc}] {d['path']}:{d['line']}")
    eqs = idx.db.execute("SELECT * FROM chunks WHERE kind = 'equation' AND name = ? COLLATE NOCASE", (name,)).fetchall()
    for r in eqs:
        out.append("## Equation definition\n" + _fmt_chunk(r))
    uses = idx.db.execute(
        "SELECT * FROM symbol_uses WHERE name = ? COLLATE NOCASE ORDER BY path, line", (name,)
    ).fetchall()
    if uses:
        out.append(f"## Uses ({len(uses)} lines, showing {min(len(uses), max_uses)})")
        for u in uses[:max_uses]:
            ctx = "/".join(x for x in (u["module"], u["realization"], u["phase"]) if x)
            if "context" in u.keys() and u["context"]:
                ctx += f", in {u['context']}"
            out.append(f"- {u['path']}:{u['line']} [{ctx}]  {u['snippet']}")
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
def list_realizations(module: str) -> str:
    """List the realizations of a module (e.g. "45", "carbonprice") with their descriptions, and the
    default realization selected in main.gms."""
    rows = idx.db.execute(
        "SELECT * FROM chunks WHERE kind IN ('module_doc', 'realization_doc') ORDER BY module, realization, line_start"
    ).fetchall()
    rows = [r for r in rows if _module_match(module, r["module"])]
    if not rows:
        return f"Module {module!r} not found."
    mod = rows[0]["module"]
    sel = idx.db.execute("SELECT * FROM switches WHERE name = ? COLLATE NOCASE", (mod.split("_", 1)[1],)).fetchone()
    out = [f"# {mod}" + (f"  (default realization in main.gms: {sel['default_value']})" if sel else "")]
    for r in rows:
        label = "module" if r["kind"] == "module_doc" else f"realization {r['realization']}"
        out.append(f"## {label}  {r['path']}:{r['line_start']}\n{r['text'][:1500]}")
    return "\n\n".join(out)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
