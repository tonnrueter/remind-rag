"""Embedding reuse: a rebuild takes every vector whose exact embedded text is unchanged from the previous index."""

from remind_rag import server
from remind_rag.index import MAX_EMBED_CHARS, embed_key, embed_text, load_vectors


def test_key_depends_on_model_and_text():
    assert embed_key("bge", "x") == embed_key("bge", "x")
    assert embed_key("bge", "x") != embed_key("jina", "x")
    assert embed_key("bge", "x") != embed_key("bge", "x ")


def test_current_index_is_fully_reusable():
    db = server._default_db()
    cache = load_vectors(db, server.idx.model)
    # every chunk finds its vector from the stored header + text (identical texts share one entry)
    rows = server.idx.db.execute("SELECT header, text FROM chunks").fetchall()
    assert all(embed_key(server.idx.model, embed_text(h, t)[:MAX_EMBED_CHARS]) in cache for h, t in rows)
    assert load_vectors(db, "some-other-model") == {}


def test_unusable_file_means_full_build(tmp_path):
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database")
    assert load_vectors(bad, "bge") == {}
