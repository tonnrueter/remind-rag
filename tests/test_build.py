"""Build steps on a tiny hand-made index: no REMIND checkout, no embedding model."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(store, "PINNED", tmp_path / "pinned.db")
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


def test_indented_star_continues_a_formula(tmp_path):
    # only `*` in column 1 starts a GAMS comment; `  * p_b(t)` multiplies (REMIND 51 KWlikeItr/postsolve.gms)
    db = store.create(tmp_path / "t.db", 4)
    db.executemany("INSERT INTO symbols (name, kind) VALUES (?, 'parameter')", [(n,) for n in ("p_a", "p_b", "p_c")])
    text = "* p_c is mentioned in a comment\np_a(t) =\n    p_c(t)\n    * p_b(t)\n  ** 2;\n"
    index.build_uses(db, [("core/postsolve.gms", text)])
    rows = db.execute("SELECT name, line, role FROM symbol_uses ORDER BY line, name").fetchall()
    assert [tuple(r) for r in rows] == [("p_a", 2, "assigned"), ("p_c", 3, "read"), ("p_b", 4, "read")]


def test_declarations_with_data_assign(tmp_path):
    # a declaration with data gives the symbol its values; a switch inside the data is read (FINDINGS 19)
    db = store.create(tmp_path / "t.db", 4)
    db.executemany(
        "INSERT INTO symbols (name, kind) VALUES (?, 'parameter')", [(n,) for n in ("p_a", "p_b", "p_c", "f_d")]
    )
    db.execute("INSERT INTO switches (name) VALUES ('cm_x')")
    db.executemany(
        "INSERT INTO chunks (path, kind, line_start, line_end) VALUES ('core/datainput.gms', 'declaration', ?, ?)",
        [(1, 4), (5, 5), (6, 8)],
    )
    text = (
        'parameters\n  p_a(t) "with data" / 2020 1 /\n  p_b(t) "without"\n;\n'
        'parameter p_c(t) "switch carrier" / %cm_x% /;\n'
        'table f_d(t,r) "table"\n$include "./f_d.cs4r"\n;\n'
        '$if %cm_x% == "on" p_b(t) = 1;\n'
    )
    index.build_uses(db, [("core/datainput.gms", text)])
    rows = db.execute("SELECT name, line, role, conditions FROM symbol_uses ORDER BY line, rowid").fetchall()
    assert [tuple(r) for r in rows] == [
        ("p_a", 2, "assigned", "[]"),
        ("p_b", 3, "declared", "[]"),
        ("p_c", 5, "assigned", "[]"),
        ("cm_x", 5, "read", "[]"),
        ("f_d", 6, "assigned", "[]"),
        ("cm_x", 9, "read", '["%cm_x% == \\"on\\""]'),
        ("p_b", 9, "assigned", '["%cm_x% == \\"on\\""]'),
    ]


def test_ontext_blocks_are_no_uses(tmp_path):
    # switched-off code between $onText and $offText (REMIND 24_trade/se_trade/presolve.gms) assigns nothing
    db = store.create(tmp_path / "t.db", 4)
    db.executemany("INSERT INTO symbols (name, kind) VALUES (?, 'parameter')", [(n,) for n in ("p_a", "p_b")])
    text = "$onText\np_a(t) = p_b(t);\n$offText\np_b(t) = 1;\n"
    index.build_uses(db, [("core/presolve.gms", text)])
    rows = db.execute("SELECT name, line, role FROM symbol_uses").fetchall()
    assert [tuple(r) for r in rows] == [("p_b", 4, "assigned")]


def test_every_stored_condition_is_parsed_once(tmp_path):
    # 56: $ifthen conditions, if() guards and equation $-conditions get a tree in `conditions`; readers look it up
    from remind_rag.conditions import Trees, holds

    db = store.create(tmp_path / "t.db", 4)
    db.execute("INSERT INTO symbols (name, kind) VALUES ('p_x', 'parameter')")
    db.execute("INSERT INTO switches (name, value) VALUES ('cm_x', '2')")
    eq = "q_x(t)$(cm_x eq 2)..\n  v_x(t) =e= p_x(t);"
    db.execute(
        "INSERT INTO chunks (id, kind, name, path, text) VALUES (1, 'equation', 'q_x', 'core/equations.gms', ?)", (eq,)
    )
    gms = '$ifthen "%cm_y%" == "on"\nif(cm_x eq 1,\n  p_x(t) = 1;\n);\n$endif\n'
    index.build_uses(db, [("core/presolve.gms", gms)])
    n = index.build_conditions(db)
    stored = {r[0] for r in db.execute("SELECT text FROM conditions")}
    assert stored == {'"%cm_y%" == "on"', "cm_x eq 1", "(cm_x eq 2)"} and n == 3
    trees = Trees(db)
    assert holds(trees["(cm_x eq 2)"], {"cm_x": "2"}) is True
    assert trees["cm_x eq 1"] == ["sw", "cm_x", "==", "1", "r"]


def test_conditions_of_an_index_without_the_table_are_parsed_on_the_spot(tmp_path):
    # retrieval_eval reads old indexes (schema 1 has no conditions table)
    from remind_rag.conditions import Trees

    db = store.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    assert Trees(db)["not (cm_x eq 1)"] == ["not", ["sw", "cm_x", "==", "1", "r"]]


def test_code_after_an_ifthen_test_runs_in_its_branch(tmp_path):
    # 56: GAMS reads one test after $ifthen / $elseif and compiles the rest of the line in the branch (checked with
    # GAMS 51); 31_fossil MOFEX / timeDepGrades write equation terms there. The condition used to swallow the code.
    db = store.create(tmp_path / "t.db", 4)
    db.execute("INSERT INTO symbols (name, kind) VALUES ('p_a', 'parameter'), ('p_b', 'parameter')")
    gms = '$ifthen.s %x% == "a"\t  + p_a(t)\n$elseif.s %x% == "b"    + p_b(t)\n$endif.s\n'
    index.build_uses(db, [("core/equations.gms", gms)])
    conds = {r["name"]: json.loads(r["conditions"]) for r in db.execute("SELECT name, conditions FROM symbol_uses")}
    assert conds == {"p_a": ['%x% == "a"'], "p_b": ['not (%x% == "a") and (%x% == "b")']}
