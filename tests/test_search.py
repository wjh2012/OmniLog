from __future__ import annotations

import pytest


@pytest.fixture
def corpus(make_page):
    make_page("위키 시스템", "이것은 위키시스템 설계 문서입니다.")
    make_page("FastAPI Guide", "Building a wiki engine with FastAPI and SQLite.")
    make_page("Unrelated", "완전히 다른 내용.")


def _slugs(client, query: str, **params) -> list[str]:
    body = client.get("/api/search", params={"q": query, **params}).json()
    return [item["slug"] for item in body["items"]]


def test_english_search(client, corpus) -> None:
    assert _slugs(client, "FastAPI") == ["fastapi-guide"]


def test_korean_substring_search(client, corpus) -> None:
    """trigram indexing means a partial word inside 위키시스템 still matches."""
    assert _slugs(client, "위키시") == ["위키-시스템"]


def test_short_korean_query_uses_the_like_fallback(client, corpus) -> None:
    """Two characters is below trigram's minimum, so LIKE has to answer."""
    hits = _slugs(client, "위키")
    assert "위키-시스템" in hits


def test_snippet_marks_the_match(client, corpus) -> None:
    body = client.get("/api/search", params={"q": "설계"}).json()
    assert "<mark>설계</mark>" in body["items"][0]["snippet"]


def test_snippet_escapes_html(client, make_page) -> None:
    make_page("Injection", "danger <script>alert(1)</script> marker")
    body = client.get("/api/search", params={"q": "marker"}).json()
    assert "<script>" not in body["items"][0]["snippet"]


def test_fts_operators_in_query_are_literal(client, corpus) -> None:
    """A bare OR / quote must not blow up the FTS parser."""
    response = client.get("/api/search", params={"q": 'wiki OR "engine'})
    assert response.status_code == 200


def test_search_misses_return_nothing(client, corpus) -> None:
    assert _slugs(client, "존재하지않는단어") == []


def test_index_follows_edits(client, make_page) -> None:
    make_page("Mutable", "원래 내용")
    assert _slugs(client, "원래 내용") == ["mutable"]
    client.put("/api/pages/mutable", json={"content": "바뀐 내용"})
    assert _slugs(client, "원래 내용") == []
    assert _slugs(client, "바뀐 내용") == ["mutable"]


def test_deleted_pages_leave_the_index(client, make_page) -> None:
    make_page("Ephemeral", "찾을 수 있는 내용")
    client.delete("/api/pages/ephemeral")
    assert _slugs(client, "찾을 수 있는") == []
