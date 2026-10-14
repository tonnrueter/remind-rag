"""Build steps on a tiny hand-made index: no REMIND checkout, no embedding model."""

from __future__ import annotations

from remind_rag import index, store


def test_uses_in_order_of_appearance(tmp_path):
    # links lists a statement's inputs in this order; a set made it differ from build to build
    db = store.create(tmp_path / "t.db", 4)
    db.executemany("INSERT INTO symbols (name, kind) VALUES (?, 'parameter')",
                   [(n,) for n in ("p_z", "p_a", "p_m")])
    db.execute("INSERT INTO switches (name) VALUES ('cm_x')")
    index.build_uses(db, [("core/presolve.gms", "p_z(t) = p_m(t) * p_a(t) + cm_x;\n")])
    names = [r["name"] for r in db.execute("SELECT name FROM symbol_uses ORDER BY rowid")]
    assert names == ["p_z", "p_m", "p_a", "cm_x"]
