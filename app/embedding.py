"""Embedders. The real one runs a small multilingual model on CPU; the hashing one needs no download."""

import hashlib
import re
from functools import lru_cache
from typing import Protocol

import numpy as np

DIM = 384


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """About 30% of descriptions are in Malay, so the default model is multilingual (384 dimensions, CPU)."""

    def __init__(self, model: str):
        from fastembed import TextEmbedding  # imported lazily: the API never needs the model

        self.name = model
        self._model = TextEmbedding(model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        if vectors.shape[1] != DIM:
            raise ValueError(f"{self.name} returns {vectors.shape[1]} dimensions; the schema expects {DIM}")
        return _normalise(vectors)


class HashingEmbedder:
    """Placeholder: hashed bag of words and character trigrams. Deterministic, no model, good enough for tests."""

    name = "hash"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            words = re.findall(r"\w+", text.lower())
            grams = [w[i : i + 3] for w in words for i in range(max(1, len(w) - 2))]
            for token in words + grams:
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % DIM
                out[row, index] += 1.0 if digest[4] & 1 else -1.0
        return _normalise(out)


def _normalise(vectors: np.ndarray) -> list[list[float]]:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).tolist()


@lru_cache(maxsize=1)
def get(model: str) -> Embedder:
    return HashingEmbedder() if model == "hash" else FastEmbedEmbedder(model)
