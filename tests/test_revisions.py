from __future__ import annotations

import pytest


@pytest.fixture
def page_with_history(client, make_page):
    make_page("History", "line one\n")
    client.put("/api/pages/history", json={"content": "line one\nline two\n"})
    client.put("/api/pages/history", json={"content": "line one\nline three\n"})
    return "history"


def test_history_is_newest_first(client, page_with_history) -> None:
    body = client.get(f"/api/pages/{page_with_history}/revisions").json()
    assert body["total"] == 3
    assert [item["number"] for item in body["items"]] == [3, 2, 1]


def test_revisions_chain_to_their_parent(client, page_with_history) -> None:
    items = client.get(f"/api/pages/{page_with_history}/revisions").json()["items"]
    by_number = {item["number"]: item for item in items}
    assert by_number[1]["parent_id"] is None
    assert by_number[2]["parent_id"] == by_number[1]["id"]
    assert by_number[3]["parent_id"] == by_number[2]["id"]


def test_read_old_revision_returns_its_body(client, page_with_history) -> None:
    body = client.get(f"/api/pages/{page_with_history}/revisions/1").json()
    assert body["content"] == "line one\n"
    assert body["revision"]["number"] == 1


def test_missing_revision_is_404(client, page_with_history) -> None:
    response = client.get(f"/api/pages/{page_with_history}/revisions/99")
    assert response.status_code == 404
    assert response.json()["code"] == "revision_not_found"


def test_diff_between_revisions(client, page_with_history) -> None:
    body = client.get(
        f"/api/pages/{page_with_history}/diff", params={"from": 2, "to": 3}
    ).json()
    assert body["added_lines"] == 1
    assert body["removed_lines"] == 1
    assert "+line three" in body["diff"]
    assert "-line two" in body["diff"]


def test_diff_of_a_revision_against_itself_is_empty(client, page_with_history) -> None:
    body = client.get(
        f"/api/pages/{page_with_history}/diff", params={"from": 1, "to": 1}
    ).json()
    assert body["diff"] == ""
    assert body["added_lines"] == 0


def test_revert_appends_rather_than_rewrites(client, page_with_history) -> None:
    body = client.post(f"/api/pages/{page_with_history}/revisions/1/revert").json()
    assert body["changed"] is True
    assert body["revision"]["number"] == 4
    assert body["content"] == "line one\n"
    assert "Reverted to revision 1" in body["revision"]["comment"]

    history = client.get(f"/api/pages/{page_with_history}/revisions").json()
    assert history["total"] == 4


def test_large_body_is_compressed_but_reads_back_intact(client, make_page) -> None:
    """compress_min_bytes is 64 in tests, so this takes the gzip path."""
    content = "가나다라마바사 " * 500
    make_page("Large", content)
    assert client.get("/api/pages/large").json()["content"] == content
