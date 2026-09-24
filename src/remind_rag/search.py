"""Hybrid retrieval: BM25 (FTS5) + vector (sqlite-vec), merged by reciprocal rank fusion."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import numpy as np

from . import embeddings, store

RRF_K = 60
STOPWORDS = set(
    "a an and are as at be by can do does for from how i in is it of on or should the this to what when where "
    "which who why with without into there their than then these those its my me we you your get gets got used "
    "use using mean means much many work works".split()
)


class Index:
    def __init__(self, db_path: Path | str):
        self.db: sqlite3.Connection = store.connect(db_path)
        self.meta = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}
        self.model = self.meta["model"]
        self.root = Path(self.meta["root"])

    # ---------------------------------------------------------------- rankers

    def bm25(self, query: str, n: int) -> list[int]:
        tokens = [t for t in re.findall(r"[A-Za-z0-9_]+", query) if t.lower() not in STOPWORDS]
        if not tokens:
            return []
        fts = " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))
        rows = self.db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 3.0, 1.0) LIMIT ?", (fts, n)
        )
        return [r[0] for r in rows]

    def vector(self, query: str, n: int) -> list[int]:
        q = np.asarray(embeddings.embed_query(self.model, query), dtype=np.float32)
        rows = self.db.execute(
            "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (q.tobytes(), n)
        )
        return [r[0] for r in rows]

    def known_identifiers(self, query: str) -> list[str]:
        """Tokens of the query that are declared GAMS symbols or main.gms switches."""
        # REMIND identifiers always carry a prefix with '_' (vm_, cm_, q33_); plain words like "tax" or
        # "biomass" are also module-selection switch names and must not count
        tokens = list(dict.fromkeys(re.findall(r"[A-Za-z]\w*_\w+", query)))
        if not tokens:
            return []
        marks = ",".join("?" * len(tokens))
        rows = self.db.execute(
            f"SELECT name FROM symbols WHERE name IN ({marks}) UNION SELECT name FROM switches WHERE name IN ({marks})",
            tokens + tokens,
        )
        return [r[0] for r in rows]

    def definition_chunks(self, names: list[str]) -> list[int]:
        """Chunks declaring / defining the given symbols: declarations, equation bodies, switch docs."""
        ids: list[int] = []
        for name in names:
            for (cid,) in self.db.execute("SELECT chunk_id FROM switches WHERE name = ?", (name,)):
                ids.append(cid)
            for (cid,) in self.db.execute(
                "SELECT id FROM chunks WHERE kind = 'equation' AND name = ?", (name,)
            ):
                ids.append(cid)
            for path, line in self.db.execute("SELECT path, line FROM symbols WHERE name = ?", (name,)):
                row = self.db.execute(
                    "SELECT id FROM chunks WHERE path = ? AND line_start <= ? AND line_end >= ? AND kind = 'declaration'",
                    (path, line, line),
                ).fetchone()
                if row:
                    ids.append(row[0])
        return list(dict.fromkeys(ids))

    # ---------------------------------------------------------------- public

    def search(self, query: str, k: int = 6, module: str | None = None, realization: str | None = None,
               kind: str | None = None, mode: str = "hybrid") -> list[sqlite3.Row]:
        filtered = bool(module or realization or kind)
        n = 400 if filtered else 60
        idents = self.known_identifiers(query)
        rankings: list[tuple[list[int], float]] = []
        if mode in ("hybrid", "bm25"):
            # plain-language questions: BM25 over generic words is noisy, lean on the vectors
            rankings.append((self.bm25(query, n), 1.0 if idents or mode == "bm25" else 0.5))
        if mode in ("hybrid", "vector"):
            rankings.append((self.vector(query, n), 1.0))
        if mode == "hybrid" and idents:
            # a known identifier in the query: its declaration / definition goes first
            rankings.append((self.definition_chunks(idents), 2.0))
        scores: dict[int, float] = {}
        for ranking, weight in rankings:
            for rank, cid in enumerate(ranking):
                scores[cid] = scores.get(cid, 0.0) + weight / (RRF_K + rank + 1)
        ordered = sorted(scores, key=scores.__getitem__, reverse=True)
        rows = self._rows(ordered)
        out = []
        for cid in ordered:
            r = rows[cid]
            if module and not _module_match(module, r["module"]):
                continue
            if realization and (r["realization"] or "").lower() != realization.lower():
                continue
            if kind and r["kind"] != kind:
                continue
            out.append(r)
            if len(out) >= k:
                break
        return out

    def _rows(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        q = f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(ids))})"
        return {r["id"]: r for r in self.db.execute(q, ids)}


def _module_match(wanted: str, module: str | None) -> bool:
    """Accept '33', 'carbonRemoval', '33_carbonRemoval' or 'core'."""
    if not module:
        return False
    w = wanted.lower()
    num, _, name = module.lower().partition("_")
    return w in {module.lower(), num, name} or (w.isdigit() and w.zfill(2) == num)
