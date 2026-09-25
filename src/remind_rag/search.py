"""Hybrid retrieval: BM25 (FTS5) + vector (sqlite-vec), merged by reciprocal rank fusion."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import numpy as np

from . import embeddings, store
from .scenarios import eval_condition
from .usage import eval_expr, switch_tests

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
        self._scenario_names: list[str] | None = None
        self._defaults: dict[str, str] | None = None

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
            # declarations: identical copies (same description, e.g. one per realization of a module) count once
            decl_ids, seen_desc = [], set()
            for path, line, desc in self.db.execute(
                    "SELECT path, line, description FROM symbols WHERE name = ?", (name,)):
                if desc in seen_desc:
                    continue
                seen_desc.add(desc)
                row = self.db.execute(
                    "SELECT id FROM chunks WHERE path = ? AND line_start <= ? AND line_end >= ? AND kind = 'declaration'",
                    (path, line, line),
                ).fetchone()
                if row:
                    decl_ids.append(row[0])
            # the providing module's interface record (who provides / consumes it), right after the first declaration
            iface_ids = [cid for (cid,) in self.db.execute(
                "SELECT id FROM chunks WHERE kind = 'module_interface' AND text LIKE 'Outputs of%' AND instr(text, ?) > 0",
                (f"\n{name}:",))]
            ids += decl_ids[:1] + iface_ids + decl_ids[1:]
        return list(dict.fromkeys(ids))

    def known_scenarios(self, query: str) -> list[int]:
        """Chunk ids of scenarios named in the query (e.g. 'SSP2-PkBudg1000'); longest names first."""
        if self._scenario_names is None:
            try:
                self._scenario_names = sorted({r[0] for r in self.db.execute("SELECT name FROM scenarios")},
                                              key=len, reverse=True)
            except sqlite3.OperationalError:  # index built before scenarios existed
                self._scenario_names = []
        found = [n for n in self._scenario_names
                 if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", query, re.I)]
        ids = []
        for name in found[:3]:
            # the same scenario is often defined in several config files; boost one, preferably the main one
            row = self.db.execute(
                "SELECT id FROM chunks WHERE kind = 'scenario' AND name = ? "
                "ORDER BY path = 'config/scenario_config.csv' DESC, id LIMIT 1", (name,)).fetchone()
            if row:
                ids.append(row[0])
        return ids

    # ---------------------------------------------------------------- public

    def scenario_values(self, scenario: str) -> tuple[dict[str, str], sqlite3.Row] | None:
        """Switch values of a scenario: main.gms values overridden by its scenario_config row."""
        row = self.db.execute(
            "SELECT * FROM scenarios WHERE name = ? COLLATE NOCASE ORDER BY path = 'config/scenario_config.csv' DESC",
            (scenario,)).fetchone()
        if row is None:
            return None
        values = {r["name"]: (r["value"] or "") for r in self.db.execute("SELECT name, value FROM switches")}
        values.update(json.loads(row["settings"]))
        return values, row

    def search(self, query: str, k: int = 6, module: str | None = None, realization: str | None = None,
               kind: str | None = None, phase: str | None = None, scenario: str | None = None,
               mode: str = "hybrid") -> list[sqlite3.Row]:
        values = None
        if scenario:
            resolved = self.scenario_values(scenario)
            if resolved is None:
                raise ValueError(f"unknown scenario {scenario!r}")
            values = resolved[0]
        filtered = bool(module or realization or kind or phase or scenario)
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
        if mode == "hybrid" and (scen_ids := self.known_scenarios(query)):
            rankings.append((scen_ids, 2.0))
        scores: dict[int, float] = {}
        for ranking, weight in rankings:
            for rank, cid in enumerate(ranking):
                scores[cid] = scores.get(cid, 0.0) + weight / (RRF_K + rank + 1)
        rows = self._rows(list(scores))
        if values is not None:
            for cid, r in rows.items():
                if _inactive_realization(r, values):
                    scores[cid] = 0.0  # dropped below
                elif r["conditions"] and any(eval_condition(c, values) is False for c in json.loads(r["conditions"])):
                    scores[cid] *= 0.3  # code compiled out in this scenario: keep, but rank low
        ordered = sorted(scores, key=scores.__getitem__, reverse=True)
        out = []
        for cid in ordered:
            r = rows[cid]
            if scores[cid] == 0.0:
                continue
            if module and not _module_match(module, r["module"]):
                continue
            if realization and (r["realization"] or "").lower() != realization.lower():
                continue
            if kind and r["kind"] != kind:
                continue
            if phase and (r["phase"] or "").lower() != phase.lower():
                continue
            out.append(r)
            if len(out) >= k:
                break
        return out

    # ---------------------------------------------------------------- default configuration

    @property
    def defaults(self) -> dict[str, str]:
        """Switch values of a default run (as assigned in main.gms)."""
        if self._defaults is None:
            self._defaults = {r["name"]: (r["value"] or "").strip().strip("\"'")
                              for r in self.db.execute("SELECT name, value FROM switches")}
        return self._defaults

    def default_realization(self, module: str | None) -> str | None:
        if not module or module == "core":
            return None
        return self.defaults.get(module.partition("_")[2])

    def realization_status(self, module: str | None, realization: str | None) -> str | None:
        default = self.default_realization(module)
        if not realization or not default:
            return None
        if default.lower() == realization.lower():
            return f"realization {realization}: DEFAULT"
        if self.module_off_by_default(module):
            return (f"realization {realization}: NOT DEFAULT (module {module} is switched off by default: "
                    f"${module.partition('_')[2]} = {default})")
        return (f"realization {realization}: NOT DEFAULT (default: {default}; selected by "
                f"${module.partition('_')[2]})")

    def module_off_by_default(self, module: str | None) -> bool:
        return (self.default_realization(module) or "").lower() in {"off", "none"}

    def condition_status(self, cond: str, what: str) -> str:
        v = eval_expr(cond, self.defaults)
        label = {True: "active by default", False: "INACTIVE by default"}.get(v, "depends on non-default settings")
        return f"{what} {' '.join(cond.split())}: {label}"

    def default_status(self, r: sqlite3.Row, max_tests: int = 4) -> list[str]:
        """Lines saying whether the chunk's code runs in a default configuration: realization, $ifthen
        conditions, and switch tests inside the code (if(...) blocks, $(...) equation domains)."""
        out = []
        if s := self.realization_status(r["module"], r["realization"]):
            out.append(s)
        for c in json.loads(r["conditions"] or "[]"):
            out.append(self.condition_status(c, "compiled only if"))
        if r["kind"] in ("equation", "gams_block", "declaration"):
            tests = switch_tests(r["text"], self.defaults)
            if tests:
                shown = [f"{t} → {'true' if v else 'false' if v is False else '?'}" for t, v in tests[:max_tests]]
                more = f" (+{len(tests) - max_tests} more)" if len(tests) > max_tests else ""
                out.append("switch tests in this code (with default values): " + "; ".join(shown) + more)
        return out

    def _rows(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        q = f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(ids))})"
        return {r["id"]: r for r in self.db.execute(q, ids)}


def _inactive_realization(r: sqlite3.Row, values: dict[str, str]) -> bool:
    """Chunk of a module realization that the scenario does not select (module switch = module name)."""
    if not r["realization"] or not r["module"] or r["module"] == "core":
        return False
    selected = values.get(r["module"].partition("_")[2])
    return selected is not None and selected.lower() != r["realization"].lower()


def _module_match(wanted: str, module: str | None) -> bool:
    """Accept '33', 'carbonRemoval', '33_carbonRemoval' or 'core'."""
    if not module:
        return False
    w = wanted.lower()
    num, _, name = module.lower().partition("_")
    return w in {module.lower(), num, name} or (w.isdigit() and w.zfill(2) == num)
