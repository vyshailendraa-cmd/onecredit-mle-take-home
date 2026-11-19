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
from collections import Counter

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


def _metrics(vectors: np.ndarray, labels: list[str | None], is_malay: np.ndarray) -> dict[str, float]:
    top = neighbours(vectors, K)
    labelled = [i for i, ind in enumerate(labels) if ind]
    return {
        "industry@4": float(np.mean([np.mean([labels[j] == labels[i] for j in top[i]]) for i in labelled])),
        "malay->english@4": float(np.mean([np.mean(~is_malay[top[i]]) for i in np.where(is_malay)[0]])),
        "english->malay@4": float(np.mean([np.mean(is_malay[top[i]]) for i in np.where(~is_malay)[0]])),
    }


def report(model: str, texts: list[str], industries: list[str | None]) -> dict[str, dict[str, float]]:
    """Scores per company, and per *distinct* description.

    Only ~174 distinct descriptions cover 2,000 companies, so a company's top-4 is mostly companies with the exact
    same text, which is always the same language. The per-description view removes those exact duplicates, which is
    the fair test of whether a model can match across languages at all.
    """
    vectors = np.asarray(embedding.get(model).embed(texts), dtype=np.float32)
    is_malay = np.array([bool(MALAY.search(t)) for t in texts])
    per_company = _metrics(vectors, industries, is_malay)

    distinct: dict[str, list[str | None]] = {}
    for text, industry in zip(texts, industries, strict=True):
        distinct.setdefault(text, []).append(industry)
    unique = list(distinct)
    labels = [
        Counter(i for i in distinct[t] if i).most_common(1)[0][0] if any(distinct[t]) else None for t in unique
    ]
    index = {t: texts.index(t) for t in unique}
    unique_vectors = vectors[[index[t] for t in unique]]
    unique_malay = np.array([bool(MALAY.search(t)) for t in unique])
    per_description = _metrics(unique_vectors, labels, unique_malay)

    top = neighbours(unique_vectors, 1)
    examples = [(t, unique[top[i][0]]) for i, t in enumerate(unique) if unique_malay[i]][:6]
    return {"per company": per_company, "per distinct description": per_description, "examples": examples}


def main() -> None:
    texts, industries = load(config.load().database_url)
    print(f"{len(texts)} active companies with a description, k={K}\n")
    print(f"{len(set(texts))} distinct descriptions\n")
    for model in sys.argv[1:] or DEFAULT_MODELS:
        try:
            result = report(model, texts, industries)
        except Exception as exc:  # e.g. model can't be downloaded
            print(f"{model}: skipped ({exc})")
            continue
        print(model)
        for view in ("per company", "per distinct description"):
            print(f"  {view}")
            for name, value in result[view].items():
                print(f"    {name:<18} {value:.3f}")
        print("  nearest neighbour of some Malay descriptions:")
        for text, nearest in result["examples"]:
            print(f"    {text}  ->  {nearest}")
        print()


if __name__ == "__main__":
    main()
