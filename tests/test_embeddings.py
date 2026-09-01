from __future__ import annotations

import pytest

from omnilog.deps import get_embedder


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
def client(client, fake_embedder):
    client.app.dependency_overrides[get_embedder] = lambda: fake_embedder
    yield client
    client.app.dependency_overrides.pop(get_embedder, None)


def _reindex(client) -> dict:
    response = client.post("/api/maintenance/reindex-embeddings")
    assert response.status_code == 200, response.text
    return response.json()


def _semantic(client, q: str) -> dict:
    response = client.get("/api/search/semantic", params={"q": q})
    assert response.status_code == 200, response.text
    return response.json()


def test_semantic_search_is_empty_before_reindex(client, make_page) -> None:
    make_page("Wiki System", "This is a wiki page.")
    assert _semantic(client, "wiki")["items"] == []


def test_reindex_then_semantic_search_ranks_by_meaning(client, make_page) -> None:
    make_page("Wiki System", "This is a wiki page about design.")
    make_page("Cats", "This page is about cats.")

    _reindex(client)
    result = _semantic(client, "wiki")

    assert result["model_id"] == "fake-embed-v1"
    assert [item["slug"] for item in result["items"]] == ["wiki-system", "cats"]
    assert result["items"][0]["score"] == pytest.approx(1.0)
    assert result["items"][1]["score"] == pytest.approx(0.0)


def test_reindex_is_idempotent(client, make_page) -> None:
    make_page("Wiki System", "wiki content")
    make_page("Cats", "cat content")

    first = _reindex(client)
    assert first == {"pages": 2, "updated": 2, "model_id": "fake-embed-v1", "dimensions": 2}

    second = _reindex(client)
    assert second["updated"] == 0


def test_reindex_recomputes_only_edited_pages(client, make_page) -> None:
    make_page("Wiki System", "wiki content")
    make_page("Cats", "cat content")
    _reindex(client)

    client.put("/api/pages/cats", json={"content": "updated cat content"})
    second = _reindex(client)
    assert second["updated"] == 1


def test_embedding_survives_rename(client, make_page) -> None:
    """page_embedding is keyed by page_id, like render_cache -- a rename
    changes only page.slug, so a page found before a rename must still be
    found (under its new slug) without a fresh reindex.
    """
    make_page("Wiki System", "wiki content")
    _reindex(client)

    client.post("/api/pages/wiki-system/rename", json={"slug": "omnilog-wiki"})

    result = _semantic(client, "wiki")
    assert [item["slug"] for item in result["items"]] == ["omnilog-wiki"]


def test_semantic_search_embeds_current_body_not_html(client, make_page, fake_embedder) -> None:
    make_page("Wiki System", "**wiki** _content_")
    _reindex(client)
    assert fake_embedder.embedded == ["**wiki** _content_"]
