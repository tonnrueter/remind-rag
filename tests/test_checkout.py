"""Which checkout an index belongs to: the staleness line in the server instructions."""

from pathlib import Path

from remind_rag.checkout import served_root, staleness

META = {"root": "/nonexistent/remind", "built_at": "2026-09-25 23:05:19"}


def test_unstamped_index_says_so():
    assert "doesn't record which REMIND commit" in staleness(META, None)


def test_missing_checkout_with_commit():
    assert "checkout's commit is unknown" in staleness({**META, "remind_commit": "e50744c05" + "0" * 31}, None)


def test_other_commit_is_named(tmp_path, monkeypatch):
    import remind_rag.checkout as c
    monkeypatch.setattr(c, "git_head", lambda root: ("c5ada203f" + "0" * 31, "master"))
    line = staleness({**META, "remind_commit": "e50744c05" + "0" * 31, "remind_branch": "develop"}, tmp_path)
    assert "e50744c05 (develop)" in line and "c5ada203f (master)" in line


def test_same_commit_is_silent(tmp_path, monkeypatch):
    import remind_rag.checkout as c
    monkeypatch.setattr(c, "git_head", lambda root: ("a" * 40, "develop"))
    assert staleness({**META, "remind_commit": "a" * 40}, tmp_path) == ""


def test_env_root_overrides_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("REMIND_RAG_ROOT", str(tmp_path))
    assert served_root(META) == tmp_path.resolve()
    monkeypatch.delenv("REMIND_RAG_ROOT")
    assert served_root(META) is None
