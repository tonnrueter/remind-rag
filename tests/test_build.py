"""Build steps on a tiny hand-made index: no REMIND checkout, no embedding model."""

from __future__ import annotations

import os
import subprocess
import sys

from remind_rag import index, store


def test_keyword_index_keeps_no_copy_of_the_text(tmp_path):
    # #52: chunks holds the text; the FTS table only needs term statistics for BM25
    db = store.create(tmp_path / "t.db", 4)
    db.execute("INSERT INTO chunks (id, header, kind, text) VALUES (1, 'REMIND core', 'gams_block', 'vm_cap.fx = 0;')")
    store.build_fts(db)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts_content'").fetchone()
    hits = db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'vm_cap' ORDER BY bm25(chunks_fts, 3.0, 1.0)"
    )
    assert [r[0] for r in hits] == [1]


def test_server_refuses_an_index_without_schema_version(tmp_path):
    db = store.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("model", "bge"), ("root", str(tmp_path))])
    db.commit()
    db.close()
    r = subprocess.run(
        [sys.executable, "-c", "import remind_rag.server"],
        capture_output=True,
        text=True,
        env={**os.environ, "REMIND_RAG_DB": str(tmp_path / "old.db")},
    )
    assert r.returncode != 0
    assert "older remind-rag (index schema 0" in r.stderr and "--reuse" in r.stderr


def test_promote_refuses_parser_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PINNED", tmp_path / "pinned.db")
    built = tmp_path / "v.db"
    built.write_bytes(b"x")
    assert index.promote(built, ["equation q_x has no definition chunk"]) == built
    assert not (tmp_path / "pinned.db").exists()


def test_uses_in_order_of_appearance(tmp_path):
    # links lists a statement's inputs in this order; a set made it differ from build to build
    db = store.create(tmp_path / "t.db", 4)
    db.executemany("INSERT INTO symbols (name, kind) VALUES (?, 'parameter')", [(n,) for n in ("p_z", "p_a", "p_m")])
    db.execute("INSERT INTO switches (name) VALUES ('cm_x')")
    index.build_uses(db, [("core/presolve.gms", "p_z(t) = p_m(t) * p_a(t) + cm_x;\n")])
    names = [r["name"] for r in db.execute("SELECT name FROM symbol_uses ORDER BY rowid")]
    assert names == ["p_z", "p_m", "p_a", "cm_x"]
