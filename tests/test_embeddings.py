"""Shared embedding model (#39): model folder, offline load, fingerprint. No network: the shared-folder cases use an
empty temporary folder, the fingerprint cases the model already in rag/data/models (skipped if it isn't there)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from remind_rag import embeddings, index


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv(embeddings.MODELS_ENV, str(tmp_path))
    embeddings.load.cache_clear()
    yield tmp_path
    embeddings.load.cache_clear()


@pytest.fixture
def local_model(monkeypatch):
    monkeypatch.delenv(embeddings.MODELS_ENV, raising=False)
    path = embeddings.model_file("bge")
    if path is None:
        pytest.skip(f"bge model not in {embeddings.DEFAULT_DIR}")
    return path


def test_folder_follows_the_variable(shared, monkeypatch):
    assert embeddings.models_dir() == shared
    monkeypatch.delenv(embeddings.MODELS_ENV)
    assert embeddings.models_dir() == embeddings.DEFAULT_DIR


def test_missing_model_in_shared_folder_says_so_and_downloads_nothing(shared):
    with pytest.raises(embeddings.ModelMissing, match="python -m remind_rag.embeddings download"):
        embeddings.load("bge")
    assert not any(shared.iterdir())
    assert embeddings.fingerprint("bge") is None


def test_fingerprint_is_the_sha256_of_the_onnx_file(local_model):
    sha = embeddings.fingerprint("bge")
    assert sha == hashlib.sha256(local_model.read_bytes()).hexdigest()


def test_mismatch_sentence(local_model):
    sha = embeddings.fingerprint("bge")
    assert embeddings.mismatch({"model": "bge"}) == ""  # index without the row: silent
    assert embeddings.mismatch({"model": "bge", "model_sha256": sha}) == ""
    other = embeddings.mismatch({"model": "bge", "model_sha256": "0" * 64})
    assert "differs from the one that built this index" in other and sha[:12] in other


def test_mismatch_when_model_missing(shared):
    assert f"not found in {shared}" in embeddings.mismatch({"model": "bge", "model_sha256": "0" * 64})


def test_registration_passes_the_shared_folder(shared):
    line = index.registration(Path("."), Path("x.db"))
    assert f"-e {embeddings.MODELS_ENV}={shared.resolve().as_posix()} " in line


def test_registration_without_shared_folder(monkeypatch):
    monkeypatch.delenv(embeddings.MODELS_ENV, raising=False)
    assert embeddings.MODELS_ENV not in index.registration(Path("."), Path("x.db"))
