from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    # A low compression threshold so both the plain and gzip paths get exercised.
    return Settings(db_path=tmp_path / "wiki.db", compress_min_bytes=64)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class FakeEmbeddingProvider:
    """Two orthogonal "topics" (wiki / cat), so ranking is exact and cheap
    to reason about -- no real embedding model, no network call.
    """

    model_id = "fake-embed-v1"
    dimensions = 2

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def _vector(self, text: str) -> list[float]:
        text = text.lower()
        return [1.0 if "wiki" in text else 0.0, 1.0 if "cat" in text else 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def fake_embedder() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


@pytest.fixture
def make_page(client):
    def _make(title: str, content: str = "", **kwargs):
        response = client.post(
            "/api/pages", json={"title": title, "content": content, **kwargs}
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make
