"""Embedding providers, swappable without touching indexing or search.

`get_embedding_provider` reads `Settings.embedding_*` and returns whichever
provider it names. Callers only ever see the `EmbeddingProvider` protocol, so
switching from OpenAI to a local model later is a config change, not a code
change -- the same seam `omnilog.textstore` gives storage encoding.
"""

from __future__ import annotations

from ..config import Settings
from .base import EmbeddingProvider
from .openai import OpenAIEmbeddingProvider

#: Registered by Settings.embedding_provider. Add an entry here for a new
#: backend; nothing else in the codebase needs to know it exists.
_PROVIDERS = {
    "openai": OpenAIEmbeddingProvider,
}


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    try:
        factory = _PROVIDERS[settings.embedding_provider]
    except KeyError:
        raise ValueError(
            f"Unknown embedding provider {settings.embedding_provider!r}; "
            f"expected one of {sorted(_PROVIDERS)}"
        ) from None
    return factory(
        model_id=settings.embedding_model_id,
        dimensions=settings.embedding_dimensions,
        api_key=settings.embedding_api_key or None,
        base_url=settings.embedding_base_url or None,
    )


__all__ = ["EmbeddingProvider", "get_embedding_provider"]
