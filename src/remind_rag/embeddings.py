"""Local CPU embedding models via fastembed (ONNX)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

MODELS = {
    "jina": ("jinaai/jina-embeddings-v2-base-code", 768),
    "bge": ("BAAI/bge-small-en-v1.5", 384),
}
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "models"


@lru_cache(maxsize=2)
def load(alias: str):
    import truststore  # PIK's TLS proxy: trust the Windows cert store for the first download

    truststore.inject_into_ssl()
    from fastembed import TextEmbedding

    return TextEmbedding(MODELS[alias][0], cache_dir=str(CACHE_DIR))


def visible(alias: str, text: str) -> str:
    """The part of text the model actually reads: everything up to its token limit (512 incl. [CLS]/[SEP] for bge)."""
    enc = load(alias).model.tokenizer.encode(text)
    if not enc.overflowing:
        return text
    ends = [end for (start, end), special in zip(enc.offsets, enc.special_tokens_mask) if not special]
    return text[:ends[-1]]


def embed_passages(alias: str, texts: list[str], batch_size: int = 16):
    yield from load(alias).passage_embed(texts, batch_size=batch_size)


def embed_query(alias: str, text: str) -> np.ndarray:
    return next(iter(load(alias).query_embed(text)))
