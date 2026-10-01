"""Embedding reuse: a rebuild takes every vector whose exact embedded text is unchanged from the previous index."""

import shutil

from remind_rag import server, store
from remind_rag.index import embed_key, embed_text, load_vectors, seen_text


def test_key_depends_on_model_and_text():
    assert embed_key("bge", "x") == embed_key("bge", "x")
    assert embed_key("bge", "x") != embed_key("jina", "x")
    assert embed_key("bge", "x") != embed_key("bge", "x ")


def test_current_index_is_fully_reusable():
    db, model = server._default_db(), server.idx.model
    rule = server.idx.meta.get("embed_rule", "chars1000")
    cache = load_vectors(db, model)
    # every chunk finds its vector from the stored header + text (identical texts share one entry)
    rows = server.idx.db.execute("SELECT header, text FROM chunks").fetchall()
    assert all(embed_key(model, seen_text(rule, model, embed_text(h, t))) in cache for h, t in rows)
    assert load_vectors(db, "some-other-model") == {}


def test_old_cut_rule_is_not_reused_for_longer_text(tmp_path):
    """A vector computed from the first 1,000 characters must not count as the vector of a longer cut."""
    old = tmp_path / "old.db"
    shutil.copy(server._default_db(), old)
    db = store.connect(old)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_rule', 'chars1000')")
    db.commit()
    db.close()
    cache = load_vectors(old, "bge")
    rows = server.idx.db.execute("SELECT header, text FROM chunks").fetchall()
    long = [embed_text(h, t) for h, t in rows if len(embed_text(h, t)) > 1500]
    short = [embed_text(h, t) for h, t in rows if len(embed_text(h, t)) < 600]
    assert not any(embed_key("bge", seen_text("tokens", "bge", e)) in cache for e in long[:50])
    assert all(embed_key("bge", seen_text("tokens", "bge", e)) in cache for e in short[:50])


def test_unusable_file_means_full_build(tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database")
    assert load_vectors(bad, "bge") == {}
