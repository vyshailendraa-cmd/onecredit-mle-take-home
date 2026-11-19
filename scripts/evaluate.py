"""Offline recommendation-quality report: compare embedding models on the companies currently in Postgres.

There are no click labels yet, so this uses two proxies:
- industry@k: share of the top-k neighbours in the same industry (companies with a known industry only).
- cross-language@k: for Malay descriptions, share of neighbours with English descriptions, and vice versa.
  About 30% of descriptions are in Malay; a model that can't match "Hotel butik" with "Boutique hotel"
  splits the catalogue in two, and industry@k alone would hide that.

Usage: uv run python -m scripts.evaluate [model ...]   (default: hash, bge-small-en, multilingual MiniLM)
"""

import re
import sys

import numpy as np
import psycopg

from app import config, embedding

DEFAULT_MODELS = [
    "hash",
    "BAAI/bge-small-en-v1.5",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
]
MALAY = re.compile(
    r"\b(dan|di|untuk|perkhidmatan|kerja|kerja-kerja|pembekal|jualan|kedai|syarikat|pengeluaran|perunding|"
    r"pengilangan|agensi|pusat|kilang|ladang|pembinaan|pengangkutan|makanan|klinik|sekolah|tadika)\b",
    re.IGNORECASE,
)
K = 4


def load(url: str) -> tuple[list[str], list[str | None]]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT embedded_text, industry FROM companies"
            " WHERE status = 'active' AND nature_of_business IS NOT NULL"
        ).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def neighbours(vectors: np.ndarray, k: int) -> np.ndarray:
    sims = vectors @ vectors.T
    np.fill_diagonal(sims, -np.inf)
    return np.argsort(-sims, axis=1)[:, :k]


def report(model: str, texts: list[str], industries: list[str | None]) -> dict[str, float]:
    vectors = np.asarray(embedding.get(model).embed(texts), dtype=np.float32)
    top = neighbours(vectors, K)
    labelled = [i for i, ind in enumerate(industries) if ind]
    industry_at_k = np.mean([np.mean([industries[j] == industries[i] for j in top[i]]) for i in labelled])
    is_malay = np.array([bool(MALAY.search(t)) for t in texts])
    malay_to_en = np.mean([np.mean(~is_malay[top[i]]) for i in np.where(is_malay)[0]])
    en_to_malay = np.mean([np.mean(is_malay[top[i]]) for i in np.where(~is_malay)[0]])
    return {
        "industry@4": industry_at_k,
        "malay->english@4": malay_to_en,
        "english->malay@4": en_to_malay,
        "malay share": is_malay.mean(),
    }


def main() -> None:
    texts, industries = load(config.load().database_url)
    print(f"{len(texts)} active companies with a description, k={K}\n")
    for model in sys.argv[1:] or DEFAULT_MODELS:
        try:
            scores = report(model, texts, industries)
        except Exception as exc:  # e.g. model can't be downloaded
            print(f"{model}: skipped ({exc})")
            continue
        print(model)
        for name, value in scores.items():
            print(f"  {name:<18} {value:.3f}")


if __name__ == "__main__":
    main()
