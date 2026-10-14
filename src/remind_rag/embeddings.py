"""Local CPU embedding models via fastembed (ONNX).

The model folder is REMIND_RAG_MODELS if set (one shared, group-readable copy on the HPC), else rag/data/models.
A shared folder is only read, never downloaded into: compute nodes have no internet. Fill it once with

    REMIND_RAG_MODELS=<dir> uv run python -m remind_rag.embeddings download
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

MODELS = {
    "jina": ("jinaai/jina-embeddings-v2-base-code", 768),
    "bge": ("BAAI/bge-small-en-v1.5", 384),
}
MODELS_ENV = "REMIND_RAG_MODELS"
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "models"


class ModelMissing(RuntimeError):
    """The model isn't in the shared folder (REMIND_RAG_MODELS); the message says how to fill it."""


def models_dir() -> Path:
    """Where models are read from (and downloaded to): REMIND_RAG_MODELS, else rag/data/models."""
    return Path(os.environ.get(MODELS_ENV) or DEFAULT_DIR)


def _open(alias: str, local_only: bool):
    from fastembed import TextEmbedding

    if not local_only:
        import truststore  # PIK's TLS proxy: trust the system cert store for the download

        truststore.inject_into_ssl()
    return TextEmbedding(MODELS[alias][0], cache_dir=str(models_dir()), local_files_only=local_only)


@lru_cache(maxsize=2)
def load(alias: str):
    """The model, from disk if it is there. Missing in a shared folder: ModelMissing; missing in the default
    folder: download (fastembed checks its cache before it goes online)."""
    if not os.environ.get(MODELS_ENV):
        return _open(alias, local_only=False)
    try:
        return _open(alias, local_only=True)
    except ValueError as e:
        raise ModelMissing(
            f"embedding model {MODELS[alias][0]} not found in {models_dir()} ({MODELS_ENV}); "
            f"fill it once with: {MODELS_ENV}={models_dir()} uv run python -m remind_rag.embeddings "
            "download"
        ) from e


def model_file(alias: str) -> Path | None:
    """The model's .onnx file in the model folder, found without loading it; None if it isn't there."""
    from fastembed import TextEmbedding

    desc = next(m for m in TextEmbedding.list_supported_models() if m["model"] == MODELS[alias][0])
    repo = "models--" + desc["sources"]["hf"].replace("/", "--")
    return next((models_dir() / repo / "snapshots").glob(f"*/{desc['model_file']}"), None)


def fingerprint(alias: str) -> str | None:
    """sha256 of the model's .onnx file; None if the model isn't in the model folder. The Hugging Face cache names
    the file's blob by this hash, so a symlinked snapshot gives it without reading 64 MB."""
    path = model_file(alias)
    if path is None:
        return None
    if path.is_symlink() and re.fullmatch(r"[0-9a-f]{64}", name := Path(os.readlink(path)).name):
        return name
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mismatch(meta: dict[str, str]) -> str:
    """One sentence for the server instructions when this machine's model isn't the one that built the index;
    '' when it is, or when the index doesn't record it."""
    built = meta.get("model_sha256")
    here = fingerprint(meta["model"]) if built else None
    if not built or here == built:
        return ""
    found = f"here {here[:12]}" if here else f"not found in {models_dir()}"
    return (
        f"The embedding model differs from the one that built this index (index {built[:12]}, {found}): "
        "search ranks conceptual questions less reliably; exact lookups are unaffected."
    )


def visible(alias: str, text: str) -> str:
    """The part of text the model actually reads: everything up to its token limit (512 incl. [CLS]/[SEP] for bge)."""
    enc = load(alias).model.tokenizer.encode(text)
    if not enc.overflowing:
        return text
    ends = [end for (start, end), special in zip(enc.offsets, enc.special_tokens_mask, strict=True) if not special]
    return text[: ends[-1]]


def embed_passages(alias: str, texts: list[str], batch_size: int = 16):
    """Vectors for chunk texts, one per text, in order (a generator: the build stores them in batches).

    "Passage" vs "query" is fastembed's split for models that embed questions and documents differently
    (a prefix such as "query: "). For bge-small both are plain embeddings: bge v1.5 works without one."""
    yield from load(alias).passage_embed(texts, batch_size=batch_size)


def embed_query(alias: str, text: str) -> np.ndarray:
    """The vector of one search question (see embed_passages)."""
    return next(iter(load(alias).query_embed(text)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill the model folder (REMIND_RAG_MODELS or rag/data/models) once.")
    ap.add_argument("command", choices=["download"])
    ap.add_argument("--model", choices=sorted(MODELS), default="bge")
    args = ap.parse_args()
    _open(args.model, local_only=False)
    print(f"{MODELS[args.model][0]} in {models_dir()}\nsha256 {fingerprint(args.model)}")


if __name__ == "__main__":
    main()
