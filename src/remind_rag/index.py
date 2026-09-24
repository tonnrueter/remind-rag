"""Build the index: walk corpus -> chunk -> enrich -> embed -> SQLite.

    uv run python -m remind_rag.index --root ../remind --model jina
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np

from . import embeddings, store
from .chunkers import chunk_file, path_meta
from .corpus import iter_files
from .enrich import IDENT_RE, USE_KINDS, make_header, symbol_descriptions

DEFAULT_DB_DIR = Path(__file__).resolve().parents[2] / "data"
MAX_EMBED_CHARS = 1000


def embed_text(header: str, text: str) -> str:
    return header + "\n" + re.sub(r"[ \t]+", " ", text)


def build_uses(db, files: list[tuple[str, str]]) -> int:
    """Where-used: every non-comment GAMS/R line mentioning a known (non-set) symbol or switch,
    tagged with the equation it sits in (if any)."""
    usable = {r[0] for r in db.execute(
        f"SELECT name FROM symbols WHERE kind IN ({','.join('?' * len(USE_KINDS))})", sorted(USE_KINDS))}
    usable |= {r[0] for r in db.execute("SELECT name FROM switches")}
    eq_ranges: dict[str, list[tuple[int, int, str]]] = {}
    for path, s, e, name in db.execute(
            "SELECT path, line_start, line_end, name FROM chunks WHERE kind = 'equation'"):
        eq_ranges.setdefault(path, []).append((s, e, name))
    db.execute("DROP TABLE IF EXISTS symbol_uses")
    db.execute("CREATE TABLE symbol_uses (name TEXT, path TEXT, line INTEGER, module TEXT, realization TEXT, "
               "phase TEXT, context TEXT, snippet TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS symbol_uses_name ON symbol_uses(name COLLATE NOCASE)")
    uses = []
    for rel, text in files:
        if not rel.lower().endswith((".gms", ".r")):
            continue
        meta = path_meta(rel)
        comment = "*" if rel.endswith(".gms") else "#"
        ranges = eq_ranges.get(rel, [])
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(comment):
                continue
            idents = set(IDENT_RE.findall(line)) & usable
            if not idents:
                continue
            ctx = next((name for s, e, name in ranges if s <= n <= e), None)
            for ident in idents:
                uses.append((ident, rel, n, meta["module"], meta["realization"], meta["phase"], ctx,
                             line.strip()[:200]))
    db.executemany("INSERT INTO symbol_uses VALUES (?,?,?,?,?,?,?,?)", uses)
    db.commit()
    return len(uses)


def build(root: Path, db_path: Path, model: str) -> dict:
    t0 = time.perf_counter()
    files = list(iter_files(root))
    parsed = {rel: chunk_file(rel, text) for rel, text in files}
    chunks = [c for pf in parsed.values() for c in pf.chunks]
    symbols = [s for pf in parsed.values() for s in pf.symbols]
    switches = {sw.name: sw for pf in parsed.values() for sw in pf.switches}
    sym_desc = symbol_descriptions(symbols)
    for c in chunks:
        c.header = make_header(c, sym_desc, switches)
    t_chunk = time.perf_counter() - t0

    dim = embeddings.MODELS[model][1]
    db = store.create(db_path, dim)
    db.executemany(
        "INSERT INTO chunks (id, path, kind, name, detail, module, realization, phase, line_start, line_end, header, text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i + 1, c.path, c.kind, c.name, c.detail, c.module, c.realization, c.phase,
          c.line_start, c.line_end, c.header, c.text) for i, c in enumerate(chunks)],
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
        "INSERT INTO switches VALUES (?,?,?,?,?,?)",
        [(sw.name, sw.default, sw.allowed, sw.path, sw.line, first_id[sw.path] + sw.chunk_index)
         for pf in parsed.values() for sw in pf.switches],
    )

    n_uses = build_uses(db, files)

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
        "switches": len(switches),
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
    ap.add_argument("--model", choices=sorted(embeddings.MODELS), default="jina")
    ap.add_argument("--db", type=Path, help="default: data/remind-<model>.db")
    ap.add_argument("--no-embed", action="store_true",
                    help="existing db: only rebuild keyword index and where-used table (no re-embedding)")
    args = ap.parse_args()
    db_path = args.db or DEFAULT_DB_DIR / f"remind-{args.model}.db"
    if args.no_embed:
        db = store.connect(db_path)
        store.build_fts(db)
        n = build_uses(db, list(iter_files(args.root)))
        print(f"rebuilt keyword index and {n} symbol uses in {db_path}")
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(args.root, db_path, args.model), indent=2))


if __name__ == "__main__":
    main()
