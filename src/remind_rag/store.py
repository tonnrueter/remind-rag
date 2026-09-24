"""SQLite schema: chunks + FTS5 (BM25) + sqlite-vec vectors + symbol tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    path TEXT, kind TEXT, name TEXT, detail TEXT,
    module TEXT, realization TEXT, phase TEXT,
    line_start INTEGER, line_end INTEGER,
    header TEXT, text TEXT,
    conditions TEXT  -- JSON list of enclosing $ifthen conditions
);
CREATE INDEX chunks_path ON chunks(path);
CREATE TABLE symbols (
    name TEXT, kind TEXT, domain TEXT, description TEXT,
    path TEXT, line INTEGER, module TEXT, realization TEXT
);
CREATE INDEX symbols_name ON symbols(name COLLATE NOCASE);
-- symbol_uses is (re)created by index.build_uses
CREATE TABLE switches (
    name TEXT, default_value TEXT, allowed TEXT, value TEXT, path TEXT, line INTEGER, chunk_id INTEGER
);
CREATE INDEX switches_name ON switches(name COLLATE NOCASE);
-- from gms::codeCheck (r/export_gms.R); module = folder name, e.g. 33_carbonRemoval or core
CREATE TABLE module_interfaces (module TEXT, name TEXT, direction TEXT);
CREATE INDEX module_interfaces_name ON module_interfaces(name COLLATE NOCASE);
CREATE TABLE not_used (module TEXT, realization TEXT, name TEXT, type TEXT, reason TEXT);
CREATE TABLE scenarios (name TEXT, path TEXT, line INTEGER, settings TEXT, description TEXT);
CREATE INDEX scenarios_name ON scenarios(name COLLATE NOCASE);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path), check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.row_factory = sqlite3.Row
    return db


def fts_title(header: str, kind: str, name: str | None) -> str:
    """Keyword-search title: the header without the 'Uses:' line (which would make every equation
    that merely uses a symbol outrank the symbol's declaration), plus the declared names."""
    lines = [line for line in header.splitlines() if not line.startswith("Uses:")]
    if kind == "declaration" and name:
        lines.append("Declares: " + name)
    return "\n".join(lines)


def build_fts(db: sqlite3.Connection) -> None:
    # '_' is a token character so identifiers like vm_cap stay single tokens
    db.execute("DROP TABLE IF EXISTS chunks_fts")
    db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(title, text, tokenize=\"unicode61 tokenchars '_'\")")
    rows = db.execute("SELECT id, header, kind, name, text FROM chunks").fetchall()
    db.executemany("INSERT INTO chunks_fts(rowid, title, text) VALUES (?, ?, ?)",
                   [(r["id"], fts_title(r["header"], r["kind"], r["name"]), r["text"]) for r in rows])
    db.commit()


def create(db_path: Path, dim: int) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    db = connect(db_path)
    db.executescript(SCHEMA)
    db.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{dim}] distance_metric=cosine)")
    return db
