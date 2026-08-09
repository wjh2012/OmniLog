from __future__ import annotations

import pytest


@pytest.fixture
def renamed(client, make_page):
    make_page("Old Name", "본문입니다.\n")
    response = client.post(
        "/api/pages/old-name/rename", json={"slug": "New Name", "author": "jin"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_rename_moves_the_page(client, renamed) -> None:
    assert renamed["slug"] == "new-name"
    assert renamed["changed"] is True
    assert client.get("/api/pages/new-name").status_code == 200


def test_old_name_still_resolves(client, renamed) -> None:
    body = client.get("/api/pages/old-name").json()
    assert body["slug"] == "new-name"
    assert body["redirected_from"] == "old-name"
    assert body["content"] == "본문입니다.\n"


def test_reaching_a_page_directly_reports_no_redirect(client, renamed) -> None:
    assert client.get("/api/pages/new-name").json()["redirected_from"] is None


def test_rename_is_recorded_in_history(client, renamed) -> None:
    items = client.get("/api/pages/new-name/revisions").json()["items"]
    assert [item["number"] for item in items] == [2, 1]
    assert "Renamed old-name → new-name" in items[0]["comment"]
    assert items[0]["author"] == "jin"


def test_move_revision_reuses_the_stored_body(client, renamed) -> None:
    """The body did not change, so the move must not diff against itself."""
    body = client.get("/api/pages/new-name/diff", params={"from": 1, "to": 2}).json()
    assert body["diff"] == ""
    assert body["byte_delta"] == 0


def test_title_only_rename_keeps_the_slug(client, make_page) -> None:
    make_page("Stable Slug", "x")
    body = client.post(
        "/api/pages/stable-slug/rename", json={"title": "Brand New Title"}
    ).json()
    assert body["slug"] == "stable-slug"
    assert body["title"] == "Brand New Title"
    assert client.get("/api/redirects").json()["total"] == 0


def test_rename_without_a_redirect_breaks_the_old_name(client, make_page) -> None:
    make_page("Disposable", "x")
    client.post(
        "/api/pages/disposable/rename",
        json={"slug": "kept", "leave_redirect": False},
    )
    assert client.get("/api/pages/disposable").status_code == 404
    assert client.get("/api/pages/kept").status_code == 200


def test_no_op_rename_does_not_grow_history(client, make_page) -> None:
    make_page("Same", "x")
    body = client.post("/api/pages/same/rename", json={"slug": "Same"}).json()
    assert body["changed"] is False
    assert body["revision"]["number"] == 1


def test_rename_onto_an_existing_page_is_409(client, make_page) -> None:
    make_page("First")
    make_page("Second")
    response = client.post("/api/pages/first/rename", json={"slug": "Second"})
    assert response.status_code == 409
    assert response.json()["code"] == "slug_conflict"


def test_rename_onto_another_pages_old_name_is_409(client, make_page) -> None:
    make_page("Alpha", "x")
    client.post("/api/pages/alpha/rename", json={"slug": "Alpha Two"})
    make_page("Beta", "y")
    response = client.post("/api/pages/beta/rename", json={"slug": "Alpha"})
    assert response.status_code == 409
    assert response.json()["code"] == "redirect_conflict"


def test_rename_back_reclaims_the_pages_own_old_name(client, renamed) -> None:
    body = client.post("/api/pages/new-name/rename", json={"slug": "Old Name"}).json()
    assert body["slug"] == "old-name"
    # The stale alias is gone and the reverse one took its place.
    aliases = client.get("/api/pages/old-name/redirects").json()["items"]
    assert [item["from_slug"] for item in aliases] == ["new-name"]


def test_creating_a_page_at_an_old_name_is_409(client, renamed) -> None:
    response = client.post("/api/pages", json={"title": "Old Name"})
    assert response.status_code == 409
    assert response.json()["code"] == "redirect_conflict"


def test_rename_needs_a_slug_or_a_title(client, make_page) -> None:
    make_page("Something")
    assert client.post("/api/pages/something/rename", json={}).status_code == 422


def test_chained_renames_do_not_form_a_chain(client, make_page) -> None:
    """Aliases store a page id, so every old name points straight at the page."""
    make_page("One", "body")
    client.post("/api/pages/one/rename", json={"slug": "Two"})
    client.post("/api/pages/two/rename", json={"slug": "Three"})

    for name in ("one", "two", "three"):
        body = client.get(f"/api/pages/{name}").json()
        assert body["slug"] == "three"

    redirects = client.get("/api/redirects").json()
    assert {item["from_slug"] for item in redirects["items"]} == {"one", "two"}
    assert {item["to_slug"] for item in redirects["items"]} == {"three"}


def test_editing_through_an_old_name_edits_the_page(client, renamed) -> None:
    response = client.put("/api/pages/old-name", json={"content": "고쳤습니다.\n"})
    assert response.status_code == 200
    assert client.get("/api/pages/new-name").json()["content"] == "고쳤습니다.\n"


def test_history_and_backlinks_reachable_through_an_old_name(client, renamed) -> None:
    assert client.get("/api/pages/old-name/revisions").json()["slug"] == "new-name"
    assert client.get("/api/pages/old-name/backlinks").json()["slug"] == "new-name"


def test_links_written_against_the_old_name_stay_blue(client, make_page) -> None:
    make_page("Target", "x")
    make_page("Referrer", "see [[Target]]")
    client.post("/api/pages/target/rename", json={"slug": "Moved Target"})

    body = client.get("/api/pages/referrer").json()
    assert body["links"] == [
        {"slug": "target", "exists": True, "via_redirect": True}
    ]
    assert 'data-redirect="true"' in body["html"]


def test_backlinks_follow_old_names(client, make_page) -> None:
    make_page("Target", "x")
    make_page("Referrer", "see [[Target]]")
    client.post("/api/pages/target/rename", json={"slug": "Moved Target"})

    body = client.get("/api/pages/moved-target/backlinks").json()
    assert [item["slug"] for item in body["items"]] == ["referrer"]


def test_deleting_a_page_takes_its_old_names_with_it(client, renamed) -> None:
    client.delete("/api/pages/new-name")
    assert client.get("/api/pages/old-name").status_code == 404
    assert client.get("/api/redirects").json()["total"] == 0


def test_search_index_follows_a_rename(client, make_page) -> None:
    make_page("Findable", "고유한 검색 대상")
    client.post("/api/pages/findable/rename", json={"slug": "Renamed Findable"})
    hits = client.get("/api/search", params={"q": "고유한 검색"}).json()["items"]
    assert [hit["slug"] for hit in hits] == ["renamed-findable"]
