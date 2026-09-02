"""Embeddings via the OpenAI API -- or anything that speaks the same wire format.

`base_url` is the whole story: point it at OpenAI and you get OpenAI, point it
at a self-hosted OpenAI-compatible server (vLLM, TEI, Ollama) and you get a
local model, with no code change either way. What does *not* transfer is
per-model calling convention -- e.g. a model that wants an instruction prefix
on queries only. That belongs in a provider of its own once such a model is
actually in use; bolting an `if model_id.startswith(...)` branch in here would
just be a second provider wearing this one's name.
"""

from __future__ import annotations

from openai import OpenAI


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        model_id: str,
        dimensions: int,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.dimensions = dimensions
        # api_key=None makes the SDK fall back to OPENAI_API_KEY -- fine for
        # OpenAI itself; a local server behind base_url usually ignores the key.
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(
            model=self.model_id, input=texts, dimensions=self.dimensions
        )
        by_index = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in by_index]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
