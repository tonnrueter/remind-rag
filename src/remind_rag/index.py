"""Build the index: walk corpus -> chunk -> enrich -> embed -> SQLite.

    uv run python -m remind_rag.index --root ../remind [--model bge|jina] [--gms-export]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np

from . import embeddings, gmsdata, preconditions, scenarios, store
from .chunkers import ENDIF_RE, IFTHEN_RE, _code_part, _is_comment, _line_conditions, _stmt_end, chunk_file, path_meta
from .usage import LOAD_RE, if_guards, role
from .corpus import iter_files
from .enrich import IDENT_RE, USE_KINDS, make_header, symbol_descriptions

DEFAULT_DB_DIR = Path(__file__).resolve().parents[2] / "data"
MAX_EMBED_CHARS = 1000


def embed_text(header: str, text: str) -> str:
    return header + "\n" + re.sub(r"[ \t]+", " ", text)


def check_parse(files: list[tuple[str, str]], parsed: dict) -> list[str]:
    """Structural self-checks of the GAMS parse; a warning names what the chunker missed or cut."""
    warnings = []
    eq_chunks = {c.name.lower() for pf in parsed.values() for c in pf.chunks if c.kind == "equation" and c.name}
    declared = {(s.name.lower(), s.path, s.line) for pf in parsed.values() for s in pf.symbols
                if s.kind == "equation"}
    for name, path, line in sorted(declared):
        if name not in eq_chunks:
            warnings.append(f"equation {name} declared at {path}:{line} has no definition chunk")
    # a continuation piece of a split equation has no `..` of its own (several alternative definitions of
    # one equation in $ifthen branches are fine)
    warnings += [f"equation {c.name} is split at {c.path}:{c.line_start} (longer than the size cap)"
                 for pf in parsed.values() for c in pf.chunks if c.kind == "equation" and ".." not in c.text]
    for rel, text in files:
        if not rel.endswith(".gms"):
            continue
        lines = text.splitlines()
        opened = sum(1 for line in lines if IFTHEN_RE.match(line))
        closed = sum(1 for line in lines if ENDIF_RE.match(line))
        if opened != closed:
            warnings.append(f"{rel}: {opened} $ifthen vs {closed} $endif")
    modules = {c.module for pf in parsed.values() for c in pf.chunks if c.module and c.module != "core"}
    documented = {c.module for pf in parsed.values() for c in pf.chunks if c.kind == "module_doc"}
    warnings += [f"module {m} has no module.gms description" for m in sorted(modules - documented)]
    return warnings


def _role(lines: list[str], i: int, name: str) -> str:
    """role() of line i; when the statement continues on the next lines (`x(t)` here, `= ...` below), the
    statement up to its `;` is checked as one line."""
    r = role(lines[i], name)
    if r == "read" and ";" not in _code_part(lines[i]) and not lines[i].lstrip().startswith("$"):
        end = min(_stmt_end(lines, i), i + 10)
        rest = []
        for x in lines[i + 1:end]:
            if x.lstrip().startswith("$"):  # a compiler directive ends the text we can join
                break
            if not _is_comment(x):
                rest.append(x.split("!!", 1)[0])
        # the following lines may only supply the `=` for the occurrence on this line: their own mentions of
        # the name (e.g. the body of an if/loop opened here) and loads belong to other statements
        tail = re.sub(rf"(?<![\w.%]){re.escape(name)}(?!\w)", "_", " ".join(rest), flags=re.I)
        if not LOAD_RE.search(_code_part(tail)):
            r = role(lines[i].split("!!", 1)[0] + " " + tail, name)
    return r


def build_uses(db, files: list[tuple[str, str]]) -> int:
    """Where-used: every non-comment GAMS/R line mentioning a known (non-set) symbol or switch, tagged with
    the equation it sits in (if any), its role (declared / assigned / equation / read), the $ifthen conditions
    it is compiled under and the run-time if(...) conditions guarding it."""
    usable = {r[0] for r in db.execute(
        f"SELECT name FROM symbols WHERE kind IN ({','.join('?' * len(USE_KINDS))})", sorted(USE_KINDS))}
    usable |= {r[0] for r in db.execute("SELECT name FROM switches")}
    eq_ranges: dict[str, list[tuple[int, int, str]]] = {}
    decl_ranges: dict[str, list[tuple[int, int]]] = {}
    for path, s, e, name, kind in db.execute(
            "SELECT path, line_start, line_end, name, kind FROM chunks WHERE kind IN ('equation', 'declaration')"):
        if kind == "equation":
            eq_ranges.setdefault(path, []).append((s, e, name))
        else:
            decl_ranges.setdefault(path, []).append((s, e))
    db.execute("DROP TABLE IF EXISTS symbol_uses")
    db.execute("CREATE TABLE symbol_uses (name TEXT, path TEXT, line INTEGER, module TEXT, realization TEXT, "
               "phase TEXT, context TEXT, snippet TEXT, role TEXT, conditions TEXT, guards TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS symbol_uses_name ON symbol_uses(name COLLATE NOCASE)")
    uses = []
    for rel, text in files:
        is_gms = rel.endswith(".gms")
        if not rel.lower().endswith((".gms", ".r")):
            continue
        meta = path_meta(rel)
        comment = "*" if is_gms else "#"
        ranges, decls = eq_ranges.get(rel, []), decl_ranges.get(rel, [])
        lines = text.splitlines()
        conds = _line_conditions(lines) if is_gms else [()] * len(lines)
        guards = if_guards(lines) if is_gms else [()] * len(lines)
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith(comment):
                continue
            code = line.split("!!", 1)[0] if is_gms else line  # names in !! end-of-line comments aren't uses
            idents = set(IDENT_RE.findall(code)) & usable
            if not idents:
                continue
            ctx = next((name for s, e, name in ranges if s <= n <= e), None)
            in_decl = any(s <= n <= e for s, e in decls)
            if in_decl:  # in declarations, a name inside another symbol's description is not a use
                idents &= set(IDENT_RE.findall(_code_part(line)))
            for ident in idents:
                r = ("declared" if in_decl else "equation" if ctx else _role(lines, n - 1, ident)) if is_gms else "read"
                uses.append((ident, rel, n, meta["module"], meta["realization"], meta["phase"], ctx,
                             line.strip()[:200], r, json.dumps(list(conds[n - 1])),
                             json.dumps(list(guards[n - 1]))))
    db.executemany("INSERT INTO symbol_uses VALUES (?,?,?,?,?,?,?,?,?,?,?)", uses)
    db.commit()
    return len(uses)


def build_preconditions(db, root: Path) -> int:
    """Every abort in modules/ and core/ with the conditions that reach it, classified by preconditions.py:
    category precondition (switch settings alone) or assertion (data / run-time state), severity against the
    main.gms defaults and the switches, realizations and values that exist."""
    found, _ctx = preconditions.sweep(root)
    db.execute("DROP TABLE IF EXISTS preconditions")
    db.execute("CREATE TABLE preconditions (path TEXT, line INTEGER, module TEXT, realization TEXT, kind TEXT, "
               "form TEXT, category TEXT, severity TEXT, abort_if TEXT, switches TEXT, message TEXT, problems TEXT, "
               "notes TEXT, message_issue TEXT)")
    db.executemany("INSERT INTO preconditions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (p.path, p.line, p.module, p.realization, p.kind, p.form, p.category, p.severity, json.dumps(p.abort_if),
         json.dumps(sorted({t.switch for t in p.tests})), p.message,
         "; ".join(x for t in p.tests for x in t.problems), "; ".join(x for t in p.tests for x in t.notes),
         p.message_issue)
        for p in found])
    db.commit()
    return len(found)


def build(root: Path, db_path: Path, model: str, gms_export: dict | None = None) -> dict:
    t0 = time.perf_counter()
    files = list(iter_files(root))
    parsed = {rel: chunk_file(rel, text) for rel, text in files}
    chunks = [c for pf in parsed.values() for c in pf.chunks]
    symbols = [s for pf in parsed.values() for s in pf.symbols]
    switches = {sw.name: sw for pf in parsed.values() for sw in pf.switches}
    sym_desc = symbol_descriptions(symbols)
    scens = scenarios.read_scenarios(root)
    # extra chunks go after the file chunks, so file chunk ids (used for switches below) stay contiguous
    chunks += scenarios.scenario_chunks(scens)
    iface = {}
    if gms_export:
        iface = gmsdata.interface_map(gms_export)
        chunks += gmsdata.module_interface_chunks(gms_export, sym_desc)
        chunks += gmsdata.limitations_chunks(gms_export, root)
        print(gmsdata.parser_diff(gms_export, {s.name for s in symbols if s.kind != "set"}))
    for c in chunks:
        c.header = make_header(c, sym_desc, switches, iface)
    warnings = check_parse(files, parsed)
    print(f"parser checks: {len(warnings)} warning(s)")
    for w in warnings[:40]:
        print("  " + w)
    t_chunk = time.perf_counter() - t0

    dim = embeddings.MODELS[model][1]
    db = store.create(db_path, dim)
    db.executemany(
        "INSERT INTO chunks (id, path, kind, name, detail, module, realization, phase, line_start, line_end, header,"
        " text, conditions) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i + 1, c.path, c.kind, c.name, c.detail, c.module, c.realization, c.phase,
          c.line_start, c.line_end, c.header, c.text, json.dumps(c.conditions) if c.conditions else None)
         for i, c in enumerate(chunks)],
    )
    store.build_fts(db)
    db.executemany(
        "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?)",
        [(s.name, s.kind, s.domain, s.description, s.path, s.line, s.module, s.realization) for s in symbols],
    )

    # switch -> chunk id (chunk ids are assigned in file order above)
    offset, first_id = 0, {}
    for rel, pf in parsed.items():
        first_id[rel] = offset + 1
        offset += len(pf.chunks)
    db.executemany(
        "INSERT INTO switches VALUES (?,?,?,?,?,?,?)",
        [(sw.name, sw.default, sw.allowed, sw.value, sw.path, sw.line, first_id[sw.path] + sw.chunk_index)
         for pf in parsed.values() for sw in pf.switches],
    )
    db.executemany("INSERT INTO scenarios VALUES (?,?,?,?,?)",
                   [(s.name, s.path, s.line, json.dumps(s.settings), s.description) for s in scens])
    if gms_export:
        folders = gmsdata.module_folders(gms_export)
        db.executemany("INSERT INTO module_interfaces VALUES (?,?,?)",
                       [(folders.get(r["module"], r["module"]), r["name"], r["direction"])
                        for r in gms_export["interfaces"]])
        db.executemany("INSERT INTO not_used VALUES (?,?,?,?,?)",
                       [(r["module"], r["realization"], r["name"], r.get("type") or "", r.get("reason") or "")
                        for r in gms_export["not_used"] or []])
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(f"version_{k}", v) for k, v in gms_export["versions"].items()])

    n_uses = build_uses(db, files)
    n_pre = build_preconditions(db, root)

    t1 = time.perf_counter()
    # GAMS tokenizes at ~2 chars/token, so embedding dominates build time on CPU. Embedding only the
    # header + start of each chunk and batching similar lengths together (less padding) keeps it tractable;
    # BM25 still indexes the full text.
    texts = [embed_text(c.header, c.text)[:MAX_EMBED_CHARS] for c in chunks]
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    batch = []
    vecs = embeddings.embed_passages(model, [texts[i] for i in order])
    for n, (i, vec) in enumerate(zip(order, vecs), 1):
        batch.append((i + 1, np.asarray(vec, dtype=np.float32).tobytes()))
        if len(batch) == 256:
            db.executemany("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)", batch)
            db.commit()
            batch.clear()
            print(f"  embedded {n}/{len(texts)}  ({time.perf_counter() - t1:.0f}s)", flush=True)
    db.executemany("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)", batch)
    t_embed = time.perf_counter() - t1

    stats = {
        "root": str(root.resolve()),
        "model": model,
        "files": len(files),
        "corpus_mb": round(sum(len(t) for _, t in files) / 1e6, 2),
        "chunks": len(chunks),
        "symbols": len(symbols),
        "symbol_uses": n_uses,
        "preconditions": n_pre,
        "switches": len(switches),
        "scenarios": len(scens),
        "gms_export": bool(gms_export),
        "check_warnings": json.dumps(warnings),
        "chunk_seconds": round(t_chunk, 1),
        "embed_seconds": round(t_embed, 1),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in stats.items()])
    db.commit()
    db.execute("VACUUM")
    db.close()
    stats["db_mb"] = round(db_path.stat().st_size / 1e6, 1)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True, help="REMIND checkout")
    ap.add_argument("--model", choices=sorted(embeddings.MODELS), default="bge")
    ap.add_argument("--db", type=Path, help="default: data/remind-<model>.db")
    ap.add_argument("--gms-json", type=Path, default=DEFAULT_DB_DIR / "gms_export.json",
                    help="gms/goxygen export (r/export_gms.R); used if it exists")
    ap.add_argument("--gms-export", action="store_true", help="(re)run r/export_gms.R before indexing")
    ap.add_argument("--no-embed", action="store_true",
                    help="existing db: only rebuild keyword index and where-used table (no re-embedding)")
    args = ap.parse_args()
    db_path = args.db or DEFAULT_DB_DIR / f"remind-{args.model}.db"
    if args.no_embed:
        db = store.connect(db_path)
        store.build_fts(db)
        n = build_uses(db, list(iter_files(args.root)))
        n_pre = build_preconditions(db, args.root)
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                       [("symbol_uses", str(n)), ("preconditions", str(n_pre)),
                        ("uses_rebuilt_at", time.strftime("%Y-%m-%d %H:%M:%S"))])
        db.commit()
        print(f"rebuilt keyword index, {n} symbol uses and {n_pre} preconditions in {db_path}")
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.gms_export:
        gmsdata.run_export(args.root, args.gms_json)
    export = gmsdata.load(args.gms_json)
    if export is None:
        print(f"no gms export at {args.gms_json}: indexing without module interfaces / limitations")
    print(json.dumps(build(args.root, db_path, args.model, export), indent=2))


if __name__ == "__main__":
    main()
