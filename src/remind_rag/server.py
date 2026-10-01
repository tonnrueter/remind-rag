"""MCP server (stdio) exposing the REMIND index to Claude Code / opencode.

    uv run --directory <rag> python -m remind_rag.server
    env REMIND_RAG_DB selects the index (default: newest complete data/remind-bge*.db, else remind-jina*.db)
    env REMIND_RAG_ROOT names the REMIND checkout it is served for (default: the one the index was built from)
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import checkout
from .search import Index, _module_match
from .usage import STR_TEST_RE, TEST_RE, switch_tests

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
_root = checkout.served_root(idx.meta)
_where = f"the REMIND checkout at {_root}" if _root else "the REMIND checkout you are working in"
_stale = checkout.staleness(idx.meta, _root)

server = MCPServer(
    name="remind-rag",
    instructions=(
        "Search index over the REMIND model source (GAMS core/modules, main.gms switches, scenario configs, "
        f"R scripts, tutorials). Paths are relative to {_where}. "
        + (_stale + " " if _stale else "")
        + "Use get_symbol for exact GAMS identifiers (vm_*, pm_*, q33_*, s_*, ...) incl. which modules provide / "
        "consume them, get_switch for cm_*/c_* switches and module selections, get_module for a module's "
        "realizations, interfaces and limitations, get_scenario for a scenario's settings, and search for "
        "conceptual questions (pass scenario=... only when the question is about a specific scenario). "
        "For 'how does X affect Y' or 'where does this value come from', follow the chain with get_links: it "
        "shows what a symbol is computed from, what is computed from it and which equations it shares, one hop "
        "at a time; call it again on the linked symbol that leads toward the target. "
        "Results cite path:line; read the file for more context when needed. "
        "Results carry default status (» lines in search; ✓ / ⚠ / ? on each entry of get_symbol and get_links): "
        "whether the code's realization is the default and whether its $ifthen / if(...) / $(...) switch "
        "conditions hold in a default run. ✓ means it runs in a default run, ⚠ that it doesn't, ? that it depends "
        "on settings. Only ⚠ entries are alternatives. Lead with what a default "
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


def _fmt_chunk(r, rank: int | None = None, cut: int | None = None) -> str:
    """A chunk with location, default status and header; with cut, at most that many text lines plus a pointer
    to the rest. The header's 'Uses:' line (symbol descriptions, there for the embedding) is left out."""
    where = f"{r['path']}:{r['line_start']}-{r['line_end']}"
    head = f"[{rank}] {where}" if rank else where
    status = "".join(f"\n» {s}" for s in idx.default_status(r))
    header = "\n".join(x for x in r["header"].splitlines() if not x.startswith("Uses:"))
    lines = r["text"].splitlines()
    text = r["text"]
    if cut and len(lines) > cut:
        rest = r["line_start"] + cut
        text = "\n".join(lines[:cut]) + f"\n… ({len(lines) - cut} more lines, {r['path']}:{rest}–{r['line_end']})"
    return f"{head}  ({r['kind']}){status}\n{header}\n```\n{text}\n```"


def _equation_head(text: str, name: str) -> str | None:
    """`q(t,regi)$(cond)` part of an equation definition, comments dropped, whitespace collapsed."""
    code = "\n".join(line for line in text.splitlines() if not line.startswith("*"))
    i = code.lower().find(name.lower())
    j = code.find("..", i)
    return " ".join(code[i:j].split()) if i >= 0 and j > i else None


SEARCH_LINES = 12  # text lines per search hit; the rest is a path:line range

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


def _off(tag: str) -> bool:
    return tag.endswith("inactive by default") or tag.startswith(("non-default realization", "module switched off"))


def _detail(tags: list[str]) -> str:
    """Reasons under an entry: ⚠ for what keeps it out of a default run, ? for conditions that depend on settings."""
    return "".join(f"\n    {'⚠' if _off(t) else '?'} {t}" for t in tags)


def _rank(tags: list[str]) -> int:
    """0 runs by default, 1 depends on settings, 2 not in a default run (sort order of links)."""
    return 2 if any(_off(t) for t in tags) else 1 if tags else 0


def _mark(u, tags: list[str]) -> str:
    """Default status for the first line of an entry, positive as well as negative: a missing ⚠ alone was read
    as 'unknown' (33_carbonRemoval/portfolio, the default, was called an alternative in a smoke test)."""
    if any(_off(t) for t in tags):
        return "⚠ not in a default run"
    if tags:
        return "? depends on settings"
    if u["realization"] and idx.default_realization(u["module"]):
        return "✓ default realization"
    return "✓ runs by default"


@server.tool()
def search(query: str, k: int = 5, module: str | None = None, realization: str | None = None,
           kind: str | None = None, phase: str | None = None, scenario: str | None = None) -> str:
    """Hybrid (keyword + semantic) search over REMIND code and docs.

    Args:
        query: natural-language question or identifiers.
        k: number of chunks to return (default 5). Long chunks are cut after SEARCH_LINES lines with a
            path:line range for the rest (read the file for it).
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
    return "\n\n".join(_fmt_chunk(r, i, cut=SEARCH_LINES) for i, r in enumerate(rows, 1))


FULL_LIST_MAX = 15  # symbols with at most this many use lines get the full list instead of a summary
FLAGGED_MAX = 10  # ⚠ / ? lines listed individually in a summary
COMPUTED_MAX = 4  # ✓ assignments outside bounds listed individually: where the value is computed


def _use_line(u, tags: list[str], also: list[int] | None = None, snippet: int | None = None) -> str:
    ctx = "/".join(x for x in (u["module"], u["realization"], u["phase"]) if x)
    if u["context"]:
        ctx += f", in {u['context']}"
    text = _short(u["snippet"], snippet) if snippet else u["snippet"]
    more = f"  (same reason: also line{'s' if len(also) > 1 else ''} {', '.join(map(str, also))})" if also else ""
    return f"- {u['path']}:{u['line']} [{ctx}] {_mark(u, tags)}  {text}{more}" + _detail(tags)


def _grouped(tagged: list) -> list[str]:
    """Summary lines: uses in the same file with the same reasons share one entry (first snippet shown)."""
    groups: dict[tuple, list] = {}
    for u, t in tagged:
        groups.setdefault((u["path"], tuple(t)), []).append((u, t))
    return [_use_line(g[0][0], g[0][1], [u["line"] for u, _ in g[1:]], snippet=110) for g in groups.values()]


def _per_file(us: list, limit: int = 6) -> str:
    counts = Counter(u["path"] for u in us)
    shown = [f"{p} {n}" for p, n in counts.most_common(limit)]
    return ", ".join(shown) + (f", +{len(counts) - limit} more files" if len(counts) > limit else "")


@server.tool()
def get_symbol(name: str, role: str | None = None, module: str | None = None, offset: int = 0,
               max_uses: int = 40) -> str:
    """Look up a GAMS symbol (variable, parameter, scalar, equation, set, table) by exact name.

    Returns its declaration(s) with domain and description/unit, which module declares it and which modules
    consume it (gms::codeCheck), the equation definition if it is an equation, and where it is used. Use sites
    come as a summary per role (declared / assigned / equation / read) with counts per file and every use that
    does not run in a default run listed individually. For the lines themselves pass role="assigned" (or
    "equation", "read", "declared"), optionally module="core" / "33" and offset for the next page.
    """
    uses = idx.db.execute(
        "SELECT * FROM symbol_uses WHERE name = ? COLLATE NOCASE ORDER BY path, line", (name,)
    ).fetchall()
    if role or module:  # drill-down: the lines themselves, paged
        us = [u for u in uses if (not role or u["role"] == role) and (not module or _module_match(module, u["module"]))]
        if not us:
            return f"No {role or ''} uses of {name}" + (f" in {module}" if module else "") + "."
        page = us[offset:offset + max_uses]
        out = [f"# {name}: {ROLE_LABELS.get(role, 'all uses') if role else 'uses'}"
               + (f" in {module}" if module else "") + f", {offset + 1}–{offset + len(page)} of {len(us)}"]
        out += [_use_line(u, _use_tags(u)) for u in page]
        if offset + len(page) < len(us):
            out.append(f"- … next page: offset={offset + len(page)}")
        return "\n".join(out)

    decls = idx.db.execute("SELECT * FROM symbols WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    out = []
    if decls:
        out.append("## Declarations")
        # identical declarations repeated in every realization of a module are listed once
        groups: dict[tuple, list] = {}
        for d in decls:
            groups.setdefault((d["kind"], d["name"], d["domain"], d["description"], d["module"]), []).append(d)
        for (kind, nm, domain, description, mod), ds in groups.items():
            first = ds[0]
            where = f"{first['path']}:{first['line']}"
            reals = [d["realization"] for d in ds if d["realization"]]
            loc = (mod or "top-level") + (f" (realizations: {', '.join(reals)})" if len(reals) > 1
                                          else f"/{reals[0]}" if reals else "")
            default = idx.default_realization(mod)
            note = ("" if not (reals and default and default.lower() not in {x.lower() for x in reals})
                    else f"  — module switched off by default ({default})" if idx.module_off_by_default(mod)
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
            out += [f"- {r['module']}/{r['realization']}: {_short(r['reason'], 100)}" for r in skipped[:5]]
            if len(skipped) > 5:
                out.append(f"- … {len(skipped) - 5} more")
    eqs = idx.db.execute("SELECT * FROM chunks WHERE kind = 'equation' AND name = ? COLLATE NOCASE", (name,)).fetchall()
    for r in eqs:
        head = _equation_head(r["text"], r["name"])
        out.append("## Equation definition\n"
                   + (f"Generated for (domain and $-condition): {head}\n" if head else "") + _fmt_chunk(r, cut=60))
    if uses:
        groups: dict[str, list] = {}
        for u in uses:
            groups.setdefault(u["role"], []).append(u)
        full = len(uses) <= FULL_LIST_MAX
        out.append(f"## Uses ({len(uses)} lines)")
        flagged_left = FLAGGED_MAX
        for r in ("declared", "assigned", "equation", "read"):
            us = groups.get(r, [])
            if not us:
                continue
            tagged = [(u, _use_tags(u)) for u in us]
            marks = Counter(_mark(u, t).split(" ")[0] for u, t in tagged)
            split = ", ".join(f"{n} {m}" for m, n in sorted(marks.items(), key=lambda x: "✓?⚠".index(x[0])))
            mods = sorted({u["module"] or u["path"] for u in us})
            out.append(f"### {ROLE_LABELS[r]} ({len(us)} lines: {split}; {', '.join(mods)})")
            if full:
                out += [_use_line(u, t) for u, t in tagged]
                continue
            if r == "declared":  # the declarations themselves are listed above
                continue
            out.append(f"- per file: {_per_file(us)}")
            if r == "equation":
                names = Counter(u["context"] for u in us if u["context"])
                out.append(f"- equations: {', '.join(names)}")
            computed = []
            if r == "assigned":  # where the value is computed: assignments outside bounds that run by default
                computed = [(u, t) for u, t in tagged if not t and not u["path"].endswith("bounds.gms")][:COMPUTED_MAX]
            flagged = [(u, t) for u, t in tagged if t][:flagged_left]
            flagged_left = max(0, flagged_left - len(flagged))
            # each computation on its own line; uses that are off for the same reason share one
            out += [_use_line(u, t, snippet=110) for u, t in computed] + _grouped(flagged)
            if len(computed) + len(flagged) < len(us):
                out.append(f'- lines: get_symbol("{name}", role="{r}"[, module="…"])')
    if not out:
        similar = idx.db.execute(
            "SELECT DISTINCT name FROM symbols WHERE name LIKE ? LIMIT 15", (f"%{name}%",)
        ).fetchall()
        hint = ", ".join(r[0] for r in similar)
        return f"Symbol {name!r} not found." + (f" Similar: {hint}" if hint else " Try search().")
    return "\n".join(out)


_DECL_MODULES: dict[str, str] | None = None
_KINDS: dict[str, str] | None = None


def _decl_module(name: str) -> str:
    """Module declaring a symbol ('core' or e.g. '45_carbonprice'; '' for switches / unknown)."""
    global _DECL_MODULES, _KINDS
    if _DECL_MODULES is None:
        _DECL_MODULES, _KINDS = {}, {}
        for r in idx.db.execute("SELECT name, kind, module FROM symbols WHERE kind != 'set'"):
            _DECL_MODULES.setdefault(r["name"].lower(), r["module"] or "core")
            _KINDS.setdefault(r["name"].lower(), r["kind"])
    return _DECL_MODULES.get(name.lower(), "")


def _kind(name: str) -> str:
    _decl_module(name)
    return _KINDS.get(name.lower(), "switch" if name.lower() in {k.lower() for k in idx.defaults} else "")


def _names(names: list[str], home: str | None, limit: int = 12) -> str:
    """Symbol list with the declaring module where it differs from the statement's module."""
    shown = []
    for n in names[:limit]:
        m = _decl_module(n)
        shown.append(f"{n} [{m}]" if m and m != (home or "core") else n)
    return ", ".join(shown) + (f", +{len(names) - limit} more" if len(names) > limit else "")


def _loc(u) -> str:
    return "/".join(x for x in (u["module"], u["realization"], u["phase"]) if x)


def _equation_status(path: str, equation: str) -> list[str]:
    """Whether the equation's domain $-condition holds in a default run."""
    row = idx.db.execute("SELECT text FROM chunks WHERE kind = 'equation' AND path = ? AND name = ? COLLATE NOCASE",
                         (path, equation)).fetchone()
    head = _equation_head(row["text"], equation) if row else None
    if not head or "$" not in head:
        return []
    cond = head[head.index("$") + 1:]
    if not switch_tests(cond, idx.defaults):
        return []
    v = idx.condition_status(cond, "generated only if")
    return [] if v.endswith(": active by default") else [v.replace(": INACTIVE by default", " → inactive by default")]


@server.tool()
def get_links(name: str, max_links: int = 15) -> str:
    """Follow a GAMS symbol one hop through the code: which symbols it is computed from, which symbols are
    computed from it, and which other variables/parameters share an equation with it. Each link gives
    path:line, the declaring module of linked symbols when it differs, and whether the code runs in a
    default configuration. Call get_links again on a linked name for the next hop; use it to trace how an
    effect travels across modules (e.g. from a carbon price to land-use emissions).

    Args:
        name: exact GAMS symbol (vm_*, pm_*, p33_*, q_*, s_*, ...) or equation name.
        max_links: maximum links per section (default 15).
    """
    from .links import equation_members, is_load, statement

    decl = idx.db.execute("SELECT kind, module, description FROM symbols WHERE name = ? COLLATE NOCASE",
                          (name,)).fetchone()
    eq_chunks = idx.db.execute("SELECT path, name, module, realization, phase, conditions FROM chunks "
                               "WHERE kind = 'equation' AND name = ? COLLATE NOCASE", (name,)).fetchall()
    uses = idx.db.execute("SELECT * FROM symbol_uses WHERE name = ? COLLATE NOCASE AND path LIKE '%.gms' "
                          "AND role != 'declared' ORDER BY path, line", (name,)).fetchall()
    if not decl and not uses and not eq_chunks:
        return f"Symbol {name!r} not found. Try get_symbol or search."
    name = decl and idx.db.execute("SELECT name FROM symbols WHERE name = ? COLLATE NOCASE",
                                   (name,)).fetchone()[0] or name
    out = [f"# Links of {name}" + (f" — {decl['kind']} \"{decl['description']}\" [declared in "
                                   f"{decl['module'] or 'core'}]" if decl else ""),
           "One hop through the statements and equations that mention it. Each entry says whether it runs in a "
           "default configuration: ✓ yes, ⚠ no (reasons below it), ? depends on settings. Names carry [module] "
           "when declared outside the statement's module."]

    # an equation: its members
    for r in eq_chunks:
        tags = _use_tags({"module": r["module"], "realization": r["realization"], "conditions": r["conditions"],
                          "guards": "[]"}) + _equation_status(r["path"], r["name"])
        members = equation_members(idx.db, r["path"], r["name"])
        variables = [n for n in members if "variable" in _kind(n)]
        others = [n for n in members if n not in variables]
        out.append(f"## Equation {r['name']}  {r['path']} [{'/'.join(x for x in (r['module'], r['realization']) if x)}]"
                   f" {_mark(r, tags)}"
                   + _detail(tags)
                   + f"\n- variables: {_names(variables, r['module'], 40) or '-'}"
                   + f"\n- parameters / scalars / switches: {_names(others, r['module'], 40) or '-'}")

    computed, feeds, reads, equations = [], [], [], {}
    seen = set()
    for u in uses:
        if u["role"] == "equation" and u["context"]:
            if u["context"].lower() != name.lower():  # an equation's own definition is shown above
                equations.setdefault((u["path"], u["context"]), u)
            continue
        st = statement(idx.db, u["path"], u["line"])
        if not st or (st.path, st.start) in seen:
            continue
        seen.add((st.path, st.start))
        tags = _use_tags(u)
        if u["role"] == "assigned" or name in st.assigned:
            inputs = [n for n in st.names_from(u["line"]) if n.lower() != name.lower() and n not in st.assigned]
            src = "loaded from a GDX file" if is_load(st) else (
                "from " + _names(inputs, u["module"]) if inputs else "constant or set-based (no symbol inputs)")
            computed.append((_rank(tags), f"- {u['path']}:{st.start} [{_loc(u)}] {_mark(u, tags)}  {src}", tags))
        elif targets := [n for n in st.names if n in st.assigned and n.lower() != name.lower()]:
            feeds.append((_rank(tags), f"- {u['path']}:{st.start} [{_loc(u)}] {_mark(u, tags)}  → "
                                      f"{_names(targets, u['module'])}", tags))
        else:
            reads.append(u)

    def section(title: str, items: list, unit: str = "statements") -> None:
        if not items:
            return
        items.sort(key=lambda x: x[0])  # default-active links first, then depends, then inactive
        counts = [sum(1 for rank, _, _ in items if rank == r) for r in range(3)]
        out.append(f"## {title} ({len(items)} {unit}: {counts[0]} run by default"
                   + (f", {counts[1]} depend on settings" if counts[1] else "")
                   + (f", {counts[2]} not in a default run" if counts[2] else "") + ")")
        for _, line, tags in items[:max_links]:
            out.append(line + _detail(tags))
        if len(items) > max_links:
            out.append(f"- ... {len(items) - max_links} more (get_symbol lists all uses)")

    section(f"Computed from (statements assigning {name})", computed)
    section(f"Feeds into (statements reading {name} and assigning another symbol)", feeds)

    eq_items = []
    for (path, eq), u in equations.items():
        tags = _use_tags(u) + _equation_status(path, eq)
        members = [n for n in equation_members(idx.db, path, eq) if n.lower() != name.lower()]
        variables = [n for n in members if "variable" in _kind(n)]
        others = [n for n in members if n not in variables]
        line = (f"- {eq}  {path} [{_loc(u)}] {_mark(u, tags)}\n    variables: {_names(variables, u['module']) or '-'}"
                f"\n    parameters / switches: {_names(others, u['module'], 8) or '-'}")
        eq_items.append((_rank(tags), line, tags))
    section("In equations (other symbols in the same equation)", eq_items, "equations")

    if reads:
        mods = sorted({u["module"] or u["path"] for u in reads})
        out.append(f"## Other reads: {len(reads)} statements without an assignment (conditions, display, "
                   f"output; {', '.join(mods[:8])}{', …' if len(mods) > 8 else ''}); see get_symbol")
    if len(out) == 2:
        out.append("No statements or equations found (declared but never used in GAMS code).")
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


SWITCH_REF_RE = re.compile(r"%(\w+)%")
STEER_KINDS = [("branches", "branches on (compile time, $ifthen)"), ("tests", "tests (run time, if / $())"),
               ("sets", "overwrites"), ("reads", "reads")]
SEVERITY_TEXT = {"impossible": "CAN NEVER BE MET", "dead check": "check can never fire",
                 "suspicious": "names something that doesn't exist", "aborts by default": "aborts under the defaults",
                 "ok": "met by the defaults", "depends": "depends on non-default settings"}


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _names_with_defaults(names, sws: dict, limit: int = 12) -> str:
    shown = [f"{n} ({sws[n.lower()]})" for n in sorted(names, key=str.lower)[:limit]]
    return ", ".join(shown) + (f", +{len(names) - limit} more" if len(names) > limit else "")


def _doc_text(text: str) -> str:
    """The prose of a goxygen doc chunk: `*'` lines without the tags, no $include boilerplate or separator lines."""
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("*'") or "@title" in s or "@authors" in s:
            continue
        s = re.sub(r"^\*'\s*(@\w+:?\s*)?", "", s)
        if s and not re.fullmatch(r"[-=*#_ ]+", s):
            keep.append(s)
    return " ".join(keep)


def _first_sentence(text: str, n: int = 180) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return _short(m.group(1) if m else text, n)


def _steering(mod: str) -> dict:
    """Per realization: the abort preconditions, the switches its code uses (compile-time branches, run-time
    tests, overwrites, plain reads), and what the scenario configs that select it set alongside.
    Returns the full blocks (drill-down), the shared block, and per realization a one-line alert with the
    config count for the summary."""
    sws = {r["name"].lower(): (r["value"] or "").strip().strip("\"'")
           for r in idx.db.execute("SELECT name, value FROM switches")}
    canon = {r["name"].lower(): r["name"] for r in idx.db.execute("SELECT name FROM switches")}
    sel = mod.partition("_")[2].lower()
    reals = [r[0] for r in idx.db.execute("SELECT DISTINCT realization FROM chunks WHERE module = ? AND "
                                          "realization IS NOT NULL ORDER BY realization COLLATE NOCASE", (mod,))]
    uses: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for u in idx.db.execute("SELECT name, realization, role, snippet FROM symbol_uses WHERE module = ? AND "
                            "path LIKE '%.gms'", (mod,)):
        n = u["name"].lower()
        if n not in sws or n == sel:
            continue
        tested = any(m.group(1).lower() == n for m in TEST_RE.finditer(u["snippet"])) or any(
            m.group(2).lower() == n for m in STR_TEST_RE.finditer(u["snippet"]))
        kind = "sets" if u["role"] == "assigned" else "tests" if tested else "reads"
        uses[u["realization"]][canon[n]].add(kind)
    # $ifthen lines aren't uses; their switches show up in the conditions of the chunks they enclose
    for c in idx.db.execute("SELECT realization, conditions FROM chunks WHERE module = ? AND conditions IS NOT NULL",
                            (mod,)):
        for cond in json.loads(c["conditions"]):
            for n in SWITCH_REF_RE.findall(cond):
                if n.lower() in sws and n.lower() != sel:
                    uses[c["realization"]][canon[n.lower()]].add("branches")
    # switches that every realization with switch uses shares are listed once (off/none realizations use none)
    active = [r for r in reals if uses[r]]
    common = set.intersection(*(set(uses[r]) for r in active)) if len(active) > 1 else set()
    pre = defaultdict(list)
    if _has_table("preconditions"):
        for p in idx.db.execute("SELECT * FROM preconditions WHERE module = ? AND category = 'precondition' "
                                "ORDER BY path, line", (mod,)):
            pre[p["realization"]].append(p)
    scens = [json.loads(r[0]) for r in idx.db.execute("SELECT settings FROM scenarios")]
    default = idx.default_realization(mod) or ""
    chosen = {real: [s for s in scens if next((v for k, v in s.items() if k.lower() == sel), default).lower()
                     == real.lower()] for real in reals}

    def condition(p) -> str:
        cond = " and ".join(json.loads(p["abort_if"])) or "(unconditional)"
        if len(cond) > 120:  # nested if/elseif chains: the message says it better than the condition
            cond = (f"{', '.join(json.loads(p['switches']))} are inconsistent"
                    + (f' ("{_short(p["message"], 120)}")' if p["message"] else ""))
        return cond

    def block(real: str | None, title: str) -> list[str]:
        lines = [title]
        quiet = []
        for p in pre[real]:
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
        own = {n: k for n, k in uses[real].items() if n not in common}
        for kind, label in STEER_KINDS:
            # each switch under its strongest kind: a compile-time branch outranks a run-time test, ...
            names = [n for n, ks in own.items() if kind in ks and not any(
                k in ks for k, _ in STEER_KINDS[:[x for x, _ in STEER_KINDS].index(kind)])]
            if names:
                lines.append(f"- {label}: {_names_with_defaults(names, sws)}")
        if real is not None:
            relevant = set(own) | {n for p in pre[real] for n in json.loads(p["switches"])}
            counts: dict[str, Counter] = defaultdict(Counter)
            for s in chosen[real]:
                for k, v in s.items():
                    if k in relevant or k.lower() in {n.lower() for n in relevant}:
                        counts[k][v] += 1
            text = f"- in practice: {len(chosen[real])} of {len(scens)} scenario configs select it"
            if real.lower() == default.lower():
                text += " (incl. those that leave the module switch empty)"
            top = sorted(counts.items(), key=lambda kv: -sum(kv[1].values()))[:8]
            if top:
                text += "; they set " + "; ".join(
                    f"{k} = " + ", ".join(f"{_short(v, 30)} ({n})" for v, n in c.most_common(3)) for k, c in top)
            lines.append(text)
        return lines if len(lines) > 1 else []

    def alert(real: str) -> str:
        """What the summary line of a realization must say: verdicts that change an answer, plus usage."""
        parts = []
        for p in pre[real]:
            if p["severity"] in ("impossible", "suspicious"):
                parts.append(f"⚠ {SEVERITY_TEXT[p['severity']]}: aborts if {condition(p)}"
                             + (f" ({p['problems']})" if p["problems"] else ""))
            elif p["severity"] == "aborts by default":
                names = json.loads(p["switches"])
                n_set = sum(any(k.lower() in {x.lower() for x in names} for k in s) for s in chosen[real])
                parts.append(f"aborts under the defaults unless {', '.join(names)} "
                             f"{'is' if len(names) == 1 else 'are'} changed"
                             + (f" (set by {n_set} of {len(chosen[real])} selecting configs)" if chosen[real] else ""))
        parts.append(f"{len(chosen[real])} of {len(scens)} configs")
        return " · ".join(parts)

    shared = block(None, "### all realizations (module-level files)")
    if common:
        shared = shared or ["### all realizations (module-level files)"]
        who = "every realization" if len(active) == len(reals) else f"all {len(active)} realizations that use switches"
        shared.append(f"- used by {who}: {_names_with_defaults(common, sws)}")
    blocks = {}
    for real in reals:
        mark = "DEFAULT" if real.lower() == default.lower() else "not default"
        blocks[real] = block(real, f"### {real}  [{mark}]") or [f"### {real}  [{mark}]", "- no switch uses"]
    return {"reals": reals, "default": default, "shared": shared, "blocks": blocks,
            "alerts": {r: alert(r) for r in reals}}


STEER_HEAD = "## Steered by  (switches in the module's code; values in parentheses = main.gms defaults)"


def _interfaces(mod: str) -> list[str]:
    """Names only (descriptions: get_symbol); outputs with their consumers, inputs grouped by source module."""
    if not _has_table("module_interfaces"):
        return []
    ifs = idx.db.execute("SELECT name, direction FROM module_interfaces WHERE module = ? ORDER BY name",
                         (mod,)).fetchall()
    out = []
    provides = [r["name"] for r in ifs if r["direction"] == "out"]
    if provides:
        lines = [f"## Interfaces: provides (outputs), {len(provides)}"]
        for n in provides:
            users = sorted({r[0] for r in idx.db.execute(
                "SELECT module FROM module_interfaces WHERE name = ? AND direction = 'in'", (n,))} - {mod})
            lines.append(f"- {n} -> used by {_short(', '.join(users) or '-', 120)}")
        out.append("\n".join(lines))
    consumes = [r["name"] for r in ifs if r["direction"] == "in"]
    if consumes:
        switches = [n for n in consumes if n.startswith("c")]
        by_source = defaultdict(list)
        for n in consumes:
            if n in switches:
                continue
            src = idx.db.execute("SELECT module FROM module_interfaces WHERE name = ? AND direction = 'out' "
                                 "AND module != ? ORDER BY module LIMIT 1", (n, mod)).fetchone()
            by_source[src[0] if src else "?"].append(n)
        lines = [f"## Interfaces: consumes (inputs), {len(consumes)}"]
        lines += [f"- from {src}: {', '.join(ns)}" for src, ns in sorted(by_source.items())]
        if switches:
            lines.append(f"- switches read: {', '.join(switches)}")
        out.append("\n".join(lines))
    return out


@server.tool()
def get_module(module: str, realization: str | None = None) -> str:
    """A module (e.g. "33", "carbonRemoval", "core"): description, the default realization with the switches that
    steer it, every alternative realization in one line (with verdicts such as "aborts under the defaults" or
    "CAN NEVER BE MET" and how many scenario configs select it), and its interfaces (names; descriptions via
    get_symbol). Pass realization="..." for one realization in full: its description, limitations, abort
    preconditions, compile-time branches, run-time tests, switches read, and what the configs that select it set."""
    rows = idx.db.execute(
        "SELECT * FROM chunks WHERE kind IN ('module_doc', 'realization_doc', 'limitations') "
        "ORDER BY realization IS NOT NULL, realization, line_start"
    ).fetchall()
    rows = [r for r in rows if _module_match(module, r["module"])]
    is_core = module.lower() == "core"
    if not rows and not is_core:
        return f"Module {module!r} not found."
    mod = "core" if is_core else rows[0]["module"]
    default = idx.default_realization(mod) or ""
    docs: dict[str | None, list] = defaultdict(list)
    limits: dict[str | None, list] = defaultdict(list)
    for r in rows:
        (limits if r["kind"] == "limitations" else docs)[r["realization"]].append(r)

    def described(real: str | None, n: int) -> str:
        text = " ".join(_doc_text(r["text"]) for r in docs[real])
        return _short(text, n) if text else "(no description)"

    def where(real: str | None) -> str:
        return f"  [{docs[real][0]['path']}:{docs[real][0]['line_start']}]" if docs[real] else ""

    def limitations(real: str | None, n: int) -> list[str]:
        return [f"## limitations ({real or 'module'})  [{r['path']}:{r['line_start']}]\n{_short(_doc_text(r['text']), n)}"
                for r in limits[real]]

    out = []
    if not is_core:
        sel = idx.db.execute("SELECT * FROM switches WHERE name = ? COLLATE NOCASE", (mod.split("_", 1)[1],)).fetchone()
        out.append(f"# {mod}" + (f"  (default realization in main.gms: {sel['value']})" if sel else ""))
    st = _steering(mod) if not is_core else None

    if realization:  # drill-down: one realization in full
        reals = st["reals"] if st else []
        real = next((r for r in reals if r.lower() == realization.lower()), None)
        if real is None:
            return f"{mod} has no realization {realization!r}. Realizations: {', '.join(reals)}"
        mark = "DEFAULT" if real.lower() == default.lower() else "not default"
        out.append(f"## realization {real}  [{mark}]{where(real)}\n{described(real, 4000)}")
        out += limitations(real, 1500)
        out.append("\n".join([STEER_HEAD, *st["shared"], *st["blocks"][real]]))
        return "\n\n".join(out)

    out.append(f"## module{where(None)}\n{described(None, 700)}")
    out += limitations(None, 400)
    if st:
        reals = st["reals"]
        d = next((r for r in reals if r.lower() == default.lower()), None)
        if d:
            out.append(f"## realization {d}  [DEFAULT]{where(d)}\n{described(d, 900)}")
            out += limitations(d, 400)
            out.append("\n".join([STEER_HEAD, *st["shared"], *st["blocks"][d]]))
        alts = [r for r in reals if r != d]
        if alts:
            lines = [f'## Alternatives (one in full: get_module("{mod}", realization="..."))']
            for r in alts:
                lim = " · has limitations" if limits[r] else ""
                lines.append(f"- realization {r}  [not default]: {_first_sentence(described(r, 400))}{lim}"
                             f" · {st['alerts'][r]}")
            out.append("\n".join(lines))
    out += _interfaces(mod)
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
