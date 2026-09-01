"""Brute-force cosine search over `page_embedding`.

No dedicated vector index -- every stored vector is compared in Python at
query time. That is the same scale bet the FTS trigram index makes: fine
while a wiki's corpus fits comfortably in SQLite, and the first thing to
replace (with sqlite-vec, Qdrant, ...) if the corpus outgrows a full scan.
Nothing outside this module would need to change: callers deal in
`(row, score)` pairs, not in how the score was computed.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable


def pack(vector: list[float]) -> bytes:
    """Encode a vector as little-endian float32, for storage in a BLOB column."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_by_similarity(
    query: list[float], rows: Iterable[sqlite3.Row], *, limit: int
) -> list[tuple[sqlite3.Row, float]]:
    """Score every row's `embedding` column against `query`, best first."""
    scored = [(row, cosine_similarity(query, unpack(row["embedding"]))) for row in rows]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]
