from __future__ import annotations

import pytest

from omnilog.deps import get_embedder, get_optional_embedder
from omnilog.errors import EmbeddingUnavailable

from conftest import FakeEmbeddingProvider

#: 1/(k + rank) with repository._RRF_K, spelled out so a change to the
#: constant has to be a deliberate edit here too.
RRF_K = 60


def rrf(*ranks: int) -> float:
    return sum(1.0 / (RRF_K + rank) for rank in ranks)


class SynonymEmbeddingProvider(FakeEmbeddingProvider):
    """Knows one synonym the trigram index can never learn: "feline" embeds
    exactly like "cat". A query saying one, over a page saying the other, is
    the gap hybrid search exists to close.
    """

    def _vector(self, text: str) -> list[float]:
        return super()._vector(text.lower().replace("feline", "cat"))


@pytest.fixture
def embedder() -> SynonymEmbeddingProvider:
    return SynonymEmbeddingProvider()


@pytest.fixture
def client(client, embedder):
    client.app.dependency_overrides[get_embedder] = lambda: embedder
    client.app.dependency_overrides[get_optional_embedder] = lambda: embedder
    yield client
    client.app.dependency_overrides.pop(get_embedder, None)
    client.app.dependency_overrides.pop(get_optional_embedder, None)


@pytest.fixture
def corpus(client, make_page):
    make_page("Cat Care", "cat grooming notes")
    make_page("Feline Journal", "feline research journal")
    make_page("Wiki System", "wiki design notes")
    # A zero vector: the embedder has nothing to say about it, so only the
    # text half can ever find this page.
    make_page("Error Codes", "ERR_1234 troubleshooting")
    assert client.post("/api/maintenance/reindex-embeddings").status_code == 200


def _hybrid(client, q: str, **params) -> dict:
    response = client.get("/api/search/hybrid", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _by_slug(body: dict) -> dict[str, dict]:
    return {item["slug"]: item for item in body["items"]}


def test_hybrid_finds_what_only_the_semantic_half_can(client, corpus) -> None:
    """"feline" shares no characters with "cat", so trigram cannot reach the
    cat page at all -- and the whole point is that hybrid still does.
    """
    text_only = client.get("/api/search", params={"q": "feline"}).json()
    assert "cat-care" not in {item["slug"] for item in text_only["items"]}

    item = _by_slug(_hybrid(client, "feline"))["cat-care"]
    assert item["matched_by"] == ["semantic"]
    assert item["text_rank"] is None
    assert item["snippet"] is None
    assert item["anchor"] == ""
    assert "grooming" in item["excerpt"]


def test_hybrid_finds_what_only_the_text_half_can(client, corpus) -> None:
    """An exact string the embedder has no opinion about -- an error code --
    is the opposite miss, and the text half has to carry it.
    """
    body = _hybrid(client, "ERR_1234")
    item = _by_slug(body)["error-codes"]
    assert item["matched_by"] == ["text"]
    assert item["semantic_rank"] is None
    assert item["excerpt"] is None
    assert "<mark>ERR_1234</mark>" in item["snippet"]


def test_agreement_outranks_either_half_alone(client, corpus) -> None:
    """The page both halves place beats the page only one of them found."""
    body = _hybrid(client, "feline")
    items = body["items"]

    assert items[0]["slug"] == "feline-journal"
    assert items[0]["matched_by"] == ["text", "semantic"]
    assert items[0]["score"] > items[1]["score"]
    assert items[1]["slug"] == "cat-care"


def test_score_is_the_sum_of_reciprocal_ranks(client, corpus) -> None:
    item = _by_slug(_hybrid(client, "feline"))["feline-journal"]
    assert item["score"] == pytest.approx(rrf(item["text_rank"], item["semantic_rank"]))


def test_unrelated_pages_stay_out(client, corpus) -> None:
    """The brute-force scan scores every stored vector, so an orthogonal page
    comes back at 0.0. Fusing that in would hand a rank to the whole wiki.
    """
    assert "wiki-system" not in _by_slug(_hybrid(client, "feline"))


def test_a_page_takes_one_slot_even_when_two_sections_match(client, make_page) -> None:
    """Fusion ranks pages, not chunks: a page whose two sections both match
    must not collect the 1/(k+rank) bonus twice.
    """
    make_page("Cats", "## Care\ncat grooming\n\n## Kittens\ncat kittens")
    make_page("Wiki System", "wiki design notes")
    client.post("/api/maintenance/reindex-embeddings")

    items = _hybrid(client, "feline")["items"]
    assert [item["slug"] for item in items] == ["cats"]
    assert items[0]["semantic_rank"] == 1
    assert items[0]["anchor"] in {"care", "kittens"}


def test_ranking_is_stable_across_calls(client, corpus) -> None:
    first = [item["slug"] for item in _hybrid(client, "cat")["items"]]
    second = [item["slug"] for item in _hybrid(client, "cat")["items"]]
    assert first == second


def test_limit_caps_the_fused_list(client, corpus) -> None:
    body = _hybrid(client, "feline", limit=1)
    assert len(body["items"]) == 1
    assert body["limit"] == 1


def test_model_id_names_what_answered_the_semantic_half(client, corpus) -> None:
    assert _hybrid(client, "feline")["model_id"] == "fake-embed-v1"


def test_before_a_reindex_the_text_half_answers_alone(client, make_page) -> None:
    make_page("Feline Journal", "feline research journal")

    body = _hybrid(client, "feline")
    assert [item["matched_by"] for item in body["items"]] == [["text"]]


def test_no_provider_degrades_to_text_instead_of_failing(client, make_page) -> None:
    """Search is a read path: an unconfigured provider must not turn a
    perfectly answerable text query into a 502.
    """
    make_page("Feline Journal", "feline research journal")
    client.app.dependency_overrides[get_optional_embedder] = lambda: None

    body = _hybrid(client, "feline")
    assert body["model_id"] is None
    assert [item["slug"] for item in body["items"]] == ["feline-journal"]
    assert body["items"][0]["matched_by"] == ["text"]


def test_a_broken_provider_degrades_the_same_way(client, corpus) -> None:
    class BrokenProvider(SynonymEmbeddingProvider):
        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingUnavailable("upstream is down")

    client.app.dependency_overrides[get_optional_embedder] = BrokenProvider

    body = _hybrid(client, "feline")
    assert body["model_id"] is None
    assert [item["slug"] for item in body["items"]] == ["feline-journal"]


def test_the_semantic_endpoint_still_fails_loudly(client, corpus) -> None:
    """Degrading is hybrid's call, not a project-wide rule: asking for
    semantic search specifically must still report that it could not run.
    """

    class BrokenProvider(SynonymEmbeddingProvider):
        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingUnavailable("upstream is down")

    client.app.dependency_overrides[get_embedder] = BrokenProvider

    response = client.get("/api/search/semantic", params={"q": "feline"})
    assert response.status_code == 502


def test_nothing_matching_is_an_empty_list(client, corpus) -> None:
    assert _hybrid(client, "zzzznothing")["items"] == []
