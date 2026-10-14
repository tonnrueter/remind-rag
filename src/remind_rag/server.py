"""MCP server (stdio) exposing the REMIND index to Claude Code / opencode.

    uv run --directory <rag> python -m remind_rag.server
    env REMIND_RAG_DB selects the index (default: the pinned data/remind-bge.db)
    env REMIND_RAG_ROOT names the REMIND checkout it is served for (default: the one the index was built from)
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from mcp.server.mcpserver import MCPServer

from . import checkout, embeddings, store
from .search import Index, module_match
from .render import doc_text, first_sentence, fmt_chunk, grouped, mark_split, per_file, short, use_line
from .status import DefaultRun, detail, equation_domain, rank
from .steering import steering

DATA = Path(__file__).resolve().parents[2] / "data"


def _default_db() -> Path:
    """REMIND_RAG_DB, else the pinned data/remind-bge.db. Never "the newest file": that can be a draft build."""
    path = Path(os.environ.get("REMIND_RAG_DB") or DATA / "remind-bge.db")
    if not path.exists():
        raise FileNotFoundError(f"no index at {path}; build one: uv run python -m remind_rag.index --root <remind>")
    return path


idx = Index(_default_db())
if (_schema := int(idx.meta.get("schema_version", 0))) < store.SCHEMA_VERSION:
    raise SystemExit(f"{_default_db()} was built by an older remind-rag (index schema {_schema}, this version reads "
                     f"{store.SCHEMA_VERSION}); rebuild it, reusing its embeddings (seconds): uv run python -m "
                     f"remind_rag.index --root <remind> --reuse {_default_db()}")
default_run = DefaultRun(idx.db)
_root = checkout.served_root(idx.meta)
_where = f"the REMIND checkout at {_root}" if _root else "the REMIND checkout you are working in"
_stale = checkout.staleness(idx.meta, _root)
_model = embeddings.mismatch(idx.meta)

server = MCPServer(
    name="remind-rag",
    instructions=(
        "Search index over the REMIND model source (GAMS core/modules, main.gms switches, scenario configs, "
        f"R scripts, tutorials). Paths are relative to {_where}. "
        + (_stale + " " if _stale else "")
        + (_model + " " if _model else "")
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


SEARCH_LINES = 12  # text lines per search hit; the rest is a path:line range

ROLE_LABELS = {"declared": "Declared", "assigned": "Assigned / fixed / loaded",
               "equation": "Used in equations", "read": "Read"}


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
    return "\n\n".join(fmt_chunk(default_run, r, i, cut=SEARCH_LINES) for i, r in enumerate(rows, 1))


FULL_LIST_MAX = 15  # symbols with at most this many use lines get the full list instead of a summary
FLAGGED_MAX = 10  # ⚠ / ? lines listed individually in a summary
COMPUTED_MAX = 4  # ✓ assignments outside bounds listed individually: where the value is computed
SHORT_ROLE = 3  # a role with at most this many lines is listed in full


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
        us = [u for u in uses if (not role or u["role"] == role) and (not module or module_match(module, u["module"]))]
        if not us:
            return f"No {role or ''} uses of {name}" + (f" in {module}" if module else "") + "."
        page = us[offset:offset + max_uses]
        out = [f"# {name}: {ROLE_LABELS.get(role, 'all uses') if role else 'uses'}"
               + (f" in {module}" if module else "") + f", {offset + 1}–{offset + len(page)} of {len(us)}"]
        out += [use_line(default_run, u, default_run.use_tags(u)) for u in page]
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
            default = default_run.default_realization(mod)
            note = ("" if not (reals and default and default.lower() not in {x.lower() for x in reals})
                    else f"  — module switched off by default ({default})" if default_run.module_off_by_default(mod)
                    else f"  — only in non-default realizations (default: {default})")
            out.append(f"- {kind} {nm}({domain}) \"{description}\"  [{loc}] {where}"
                       + (f" (+{len(ds) - 1} more files)" if len(ds) > 1 else "") + note)
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
        out += [f"- {r['module']}/{r['realization']}: {short(r['reason'], 100)}" for r in skipped[:5]]
        if len(skipped) > 5:
            out.append(f"- also: {', '.join(r['module'] + '/' + r['realization'] for r in skipped[5:])}")
    eqs = idx.db.execute("SELECT * FROM chunks WHERE kind = 'equation' AND name = ? COLLATE NOCASE", (name,)).fetchall()
    for r in eqs:
        head = equation_domain(r["text"], r["name"])
        out.append("## Equation definition\n"
                   + (f"Generated for (domain and $-condition): {head}\n" if head else "") + fmt_chunk(default_run, r, cut=60))
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
            tagged = [(u, default_run.use_tags(u)) for u in us]
            split = mark_split(default_run, tagged)
            mods = sorted({u["module"] or u["path"] for u in us})
            out.append(f"### {ROLE_LABELS[r]} ({len(us)} lines: {split}; {', '.join(mods)})")
            if r == "declared" and not full:  # the declarations themselves are listed above
                continue
            if full or len(us) <= SHORT_ROLE:
                out += [use_line(default_run, u, t) for u, t in tagged]
                continue
            out.append(f"- per file: {per_file(default_run, tagged)}")
            if r == "equation":
                names = Counter(u["context"] for u in us if u["context"])
                out.append(f"- equations: {', '.join(names)}")
            computed = []
            if r == "assigned":  # where the value is computed: assignments outside bounds that run by default
                computed = [(u, t) for u, t in tagged if not t and not u["path"].endswith("bounds.gms")][:COMPUTED_MAX]
            flagged = [(u, t) for u, t in tagged if t][:flagged_left]
            flagged_left = max(0, flagged_left - len(flagged))
            # each computation on its own line; uses that are off for the same reason share one
            out += [use_line(default_run, u, t, snippet=110) for u, t in computed] + grouped(default_run, flagged)
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
    return _KINDS.get(name.lower(), "switch" if name.lower() in {k.lower() for k in default_run.defaults} else "")


def _names(names: list[str], home: str | None, limit: int = 12) -> str:
    """Symbol list with the declaring module where it differs from the statement's module."""
    shown = []
    for n in names[:limit]:
        m = _decl_module(n)
        shown.append(f"{n} [{m}]" if m and m != (home or "core") else n)
    return ", ".join(shown) + (f", +{len(names) - limit} more" if len(names) > limit else "")


class Link(NamedTuple):
    """One entry of a get_links section: a statement (path, line) or an equation (path None, line = its name)."""
    rank: int  # 0 runs by default, 1 depends on settings, 2 not in a default run
    path: str | None
    line: int | str
    mark: str
    text: str
    tags: list[str]


@server.tool()
def get_links(name: str, max_links: int = 10) -> str:
    """Follow a GAMS symbol one hop through the code: which symbols it is computed from, which symbols are
    computed from it, and which other variables/parameters share an equation with it. Each link gives
    path:line, the declaring module of linked symbols when it differs, and whether the code runs in a
    default configuration. Call get_links again on a linked name for the next hop; use it to trace how an
    effect travels across modules (e.g. from a carbon price to land-use emissions).

    Args:
        name: exact GAMS symbol (vm_*, pm_*, p33_*, q_*, s_*, ...) or equation name.
        max_links: maximum entries per section (default 10; links that don't run by default get at most half,
            the rest is counted per module).
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
           "One hop. ✓ runs by default · ⚠ doesn't (reasons below) · ? depends on settings · "
           "[module] = declared outside the statement's module."]

    # an equation: its members
    for r in eq_chunks:
        tags = default_run.use_tags({"module": r["module"], "realization": r["realization"], "conditions": r["conditions"],
                          "guards": "[]"}) + default_run.equation_status(r["path"], r["name"])
        members = equation_members(idx.db, r["path"], r["name"])
        variables = [n for n in members if "variable" in _kind(n)]
        others = [n for n in members if n not in variables]
        out.append(f"## Equation {r['name']}  {r['path']} [{'/'.join(x for x in (r['module'], r['realization']) if x)}]"
                   f" {default_run.mark(r, tags)}"
                   + detail(tags)
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
        tags = default_run.use_tags(u)
        if u["role"] == "assigned" or name in st.assigned:
            inputs = [n for n in st.names_from(u["line"]) if n.lower() != name.lower() and n not in st.assigned]
            src = "loaded from a GDX file" if is_load(st) else (
                "from " + _names(inputs, u["module"]) if inputs else "constant or set-based (no symbol inputs)")
            computed.append(Link(rank(tags), u["path"], st.start, default_run.mark(u, tags), src, tags))
        elif targets := [n for n in st.names if n in st.assigned and n.lower() != name.lower()]:
            feeds.append(Link(rank(tags), u["path"], st.start, default_run.mark(u, tags),
                              f"→ {_names(targets, u['module'])}", tags))
        else:
            reads.append(u)

    def section(title: str, items: list, unit: str = "statements", merge_default: bool = True) -> None:
        """Statements in the same file with the same status share one
        entry, each further line with its own text ('; :22 → x'); with merge_default off, default statements stay
        separate (in 'Computed from' each is a different way the value is computed)."""
        if not items:
            return
        # default-active first, then depends, then inactive; within a file by line (equations by name)
        items.sort(key=lambda x: (x.rank, x.path or "", x.line if isinstance(x.line, int) else 0, str(x.line)))
        counts = [sum(1 for it in items if it.rank == r) for r in range(3)]
        out.append(f"## {title} ({len(items)} {unit}: {counts[0]} run by default"
                   + (f", {counts[1]} depend on settings" if counts[1] else "")
                   + (f", {counts[2]} not in a default run" if counts[2] else "") + ")")
        groups: dict[tuple, list[Link]] = {}
        for it in items:
            separate = it.path is None or (not merge_default and it.rank == 0)
            key = (it.path, it.line) if separate else (it.path, tuple(it.tags))
            groups.setdefault(key, []).append(it)
        entries = list(groups.values())
        # links that don't run by default get at most half the room; the rest is counted per module
        active = [g for g in entries if g[0].rank < 2][:max_links]
        off = [g for g in entries if g[0].rank == 2][:max(3, min(max_links // 2, max_links - len(active)))]
        for g in active + off:
            first = g[0]
            if first.path is None:
                out.append(f"- {first.text}" + detail(first.tags))
                continue
            more = "".join(f"; :{x.line} {x.text}" for x in g[1:])
            if first.rank == 2:  # not in a default run: the further lines' details may be cut
                more = short(more, 200)
            out.append(f"- {first.path}:{first.line} {first.mark}  {first.text}{more}" + detail(first.tags))
        hidden = [it for g in entries if g not in active and g not in off for it in g]
        if hidden:
            mods = Counter((it.path or "").split("/")[1] if (it.path or "").startswith("modules/") else "core"
                           for it in hidden)
            out.append(f"- ... {len(hidden)} more ({', '.join(f'{m} {n}' for m, n in mods.most_common())}): "
                       f'get_symbol("{name}", role=…, module=…) lists them')

    section(f"Computed from (statements assigning {name})", computed, merge_default=False)
    section(f"Feeds into (statements reading {name} and assigning another symbol)", feeds)

    eq_items = []
    for (path, eq), u in equations.items():
        tags = default_run.use_tags(u) + default_run.equation_status(path, eq)
        members = [n for n in equation_members(idx.db, path, eq) if n.lower() != name.lower()]
        variables = [n for n in members if "variable" in _kind(n)]
        others = [n for n in members if n not in variables]
        text = (f"{eq}  {path} {default_run.mark(u, tags)}\n    variables: {_names(variables, u['module']) or '-'}"
                f"\n    parameters / switches: {_names(others, u['module'], 8) or '-'}")
        eq_items.append(Link(rank(tags), None, eq, "", text, tags))  # path None: rendered as is, never merged
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
        "SELECT * FROM symbol_uses WHERE name = ? COLLATE NOCASE AND path != 'main.gms' ORDER BY path, line", (name,)
    ).fetchall()
    if uses:
        tagged = [(u, default_run.use_tags(u)) for u in uses]
        split = mark_split(default_run, tagged)
        out.append(f"## Referenced in ({len(uses)} lines: {split})")
        if len(uses) > SWITCH_REFS_LISTED:
            out.append(f"- per file: {per_file(default_run, tagged)}")
        # code that runs by default first: that is what the default value actually steers
        shown = sorted(tagged, key=lambda x: rank(x[1]))[:SWITCH_REFS_LISTED]
        out += [f"- {u['path']}:{u['line']} {default_run.mark(u, t)}  {short(u['snippet'], 110)}" for u, t in shown]
        if len(uses) > SWITCH_REFS_LISTED:
            out.append(f'- lines: get_symbol("{sws[0]["name"]}", role="read"[, module="…"]) (also role="assigned")')
    return "\n".join(out)


SWITCH_REFS_LISTED = 10  # references of a switch listed individually (default-active first)
INTERFACES_LISTED = 40  # longer output lists (core: 266) are summarized per consuming module
STEER_HEAD = "## Steered by  (switches in the module's code; values in parentheses = main.gms defaults)"


def _interfaces(mod: str) -> list[str]:
    """Names only (descriptions: get_symbol); outputs with their consumers, inputs grouped by source module."""
    ifs = idx.db.execute("SELECT name, direction FROM module_interfaces WHERE module = ? ORDER BY name",
                         (mod,)).fetchall()
    out = []
    provides = [r["name"] for r in ifs if r["direction"] == "out"]
    if provides:
        lines = [f"## Interfaces: provides (outputs), {len(provides)}"]
        users = {n: sorted({r[0] for r in idx.db.execute(
            "SELECT module FROM module_interfaces WHERE name = ? AND direction = 'in'", (n,))} - {mod}) for n in provides}
        if len(provides) <= INTERFACES_LISTED:
            lines += [f"- {n} -> used by {short(', '.join(users[n]) or '-', 120)}" for n in provides]
        else:  # core provides hundreds: counts per consumer; each consumer's get_module lists its inputs by name
            per = Counter(m for n in provides for m in users[n])
            lines.append("- per consuming module: " + ", ".join(f"{m} {c}" for m, c in per.most_common()))
            lines.append(f'- names: get_module("<consumer>") lists its inputs "from {mod}"; get_symbol for one name')
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
def get_module(name: str | None = None, realization: str | None = None, module: str | None = None) -> str:
    """A module (name e.g. "33", "carbonRemoval", "core"): description, the default realization with the switches
    that steer it, every alternative realization in one line (with verdicts such as "aborts under the defaults" or
    "CAN NEVER BE MET" and how many scenario configs select it), and its interfaces (names; descriptions via
    get_symbol). Pass realization="..." for one realization in full: its description, limitations, abort
    preconditions, compile-time branches, run-time tests, switches read, and what the configs that select it set.
    (module= is the old name of name=.)"""
    module = name or module  # name= like the other tools; module= until 1.0 (ERRORS.md: Claude called name=)
    if not module:
        return 'Pass the module as name="33" (or "carbonRemoval", "33_carbonRemoval", "core").'
    rows = idx.db.execute(
        "SELECT * FROM chunks WHERE kind IN ('module_doc', 'realization_doc', 'limitations') "
        "ORDER BY realization IS NOT NULL, realization, line_start"
    ).fetchall()
    rows = [r for r in rows if module_match(module, r["module"])]
    is_core = module.lower() == "core"
    if not rows and not is_core:
        return f"Module {module!r} not found."
    mod = "core" if is_core else rows[0]["module"]
    default = default_run.default_realization(mod) or ""
    docs: dict[str | None, list] = defaultdict(list)
    limits: dict[str | None, list] = defaultdict(list)
    for r in rows:
        (limits if r["kind"] == "limitations" else docs)[r["realization"]].append(r)

    def described(real: str | None, n: int) -> str:
        text = " ".join(doc_text(r["text"]) for r in docs[real])
        return short(text, n) if text else "(no description)"

    def where(real: str | None) -> str:
        return f"  [{docs[real][0]['path']}:{docs[real][0]['line_start']}]" if docs[real] else ""

    def limitations(real: str | None, n: int) -> list[str]:
        return [f"## limitations ({real or 'module'})  [{r['path']}:{r['line_start']}]\n{short(doc_text(r['text']), n)}"
                for r in limits[real]]

    out = []
    if not is_core:
        sel = idx.db.execute("SELECT * FROM switches WHERE name = ? COLLATE NOCASE", (mod.split("_", 1)[1],)).fetchone()
        out.append(f"# {mod}" + (f"  (default realization in main.gms: {sel['value']})" if sel else ""))
    st = steering(idx.db, default_run, mod) if not is_core else None

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
                lines.append(f"- realization {r}  [not default]: {first_sentence(described(r, 400), 140)}{lim}"
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
    written = {r["name"]: r["value"] for r in idx.db.execute("SELECT name, value FROM switches")}
    defaults = default_run.defaults  # compared without quotes: `"a, b"` in main.gms equals `a, b` in a config
    changed = [k for k in settings
               if k in defaults and defaults[k].lower() != str(settings[k]).strip().strip("\"'").lower()]
    if changed:
        out.append("## Differs from main.gms")
        out += [f"- {k}: {written[k]} -> {settings[k]}" for k in changed]
    others = idx.db.execute("SELECT path FROM scenarios WHERE name = ? COLLATE NOCASE AND path != ?",
                            (row["name"], row["path"])).fetchall()
    if others:
        out.append("(also defined in: " + ", ".join(r[0] for r in others) + ")")
    return "\n".join(out)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
