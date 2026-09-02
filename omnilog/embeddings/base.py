"""The seam every embedding backend implements.

Keeping this to two methods is what makes a provider swap safe: a document
and a query are embedded through different calls because some models (asymmetric
ones, instruction-tuned ones) need different handling for each -- e.g. a query
gets a task instruction prefixed and a document does not. That difference lives
inside the provider, never in the code that calls it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    #: Identifies which model produced a vector. Stamped onto every stored
    #: embedding so a provider swap can be told apart from a stale one
    #: instead of silently mixed into the same search.
    model_id: str
    #: Length of every vector this provider returns.
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages meant to be *found*. Order matches `texts`."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed text meant to *find* passages."""
        ...
