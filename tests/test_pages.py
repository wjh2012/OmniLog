from __future__ import annotations


def test_create_and_read(client, make_page) -> None:
    created = make_page("Getting Started", "# Hi\n\nwelcome", author="jin")
    assert created["slug"] == "getting-started"
    assert created["revision"]["number"] == 1
    assert created["revision"]["author"] == "jin"
    assert "<h1>Hi</h1>" in created["html"]

    fetched = client.get("/api/pages/getting-started").json()
    assert fetched["content"] == "# Hi\n\nwelcome"
    assert fetched["title"] == "Getting Started"


def test_korean_title_round_trips(client, make_page) -> None:
    make_page("위키 시스템", "본문입니다.")
    response = client.get("/api/pages/위키-시스템")
    assert response.status_code == 200
    assert response.json()["title"] == "위키 시스템"


def test_path_accepts_title_form(client, make_page) -> None:
    """A space-separated title in the path resolves to the same page."""
    make_page("Getting Started")
    assert client.get("/api/pages/Getting Started").status_code == 200


def test_missing_page_is_404(client) -> None:
    response = client.get("/api/pages/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "page_not_found"


def test_duplicate_slug_is_409(client, make_page) -> None:
    make_page("Duplicate")
    response = client.post("/api/pages", json={"title": "Duplicate"})
    assert response.status_code == 409
    assert response.json()["code"] == "slug_conflict"


def test_unusable_title_is_400(client) -> None:
    response = client.post("/api/pages", json={"title": "!!!"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_slug"


def test_update_creates_a_revision(client, make_page) -> None:
    make_page("Notes", "first")
    response = client.put(
        "/api/pages/notes", json={"content": "second", "comment": "expand"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["revision"]["number"] == 2
    assert body["revision"]["comment"] == "expand"
    assert body["content"] == "second"


def test_null_edit_does_not_grow_history(client, make_page) -> None:
    make_page("Notes", "same")
    body = client.put("/api/pages/notes", json={"content": "same"}).json()
    assert body["changed"] is False
    assert body["revision"]["number"] == 1


def test_stale_base_revision_is_409(client, make_page) -> None:
    make_page("Notes", "v1")
    client.put("/api/pages/notes", json={"content": "v2"})
    response = client.put(
        "/api/pages/notes", json={"content": "v3", "base_revision": 1}
    )
    assert response.status_code == 409
    assert response.json()["details"]["latest_revision"] == 2


def test_delete_removes_the_page(client, make_page) -> None:
    make_page("Temporary")
    assert client.delete("/api/pages/temporary").status_code == 204
    assert client.get("/api/pages/temporary").status_code == 404


def test_list_pages_is_newest_first(client, make_page) -> None:
    make_page("Alpha")
    make_page("Beta")
    body = client.get("/api/pages").json()
    assert body["total"] == 2
    assert [item["slug"] for item in body["items"]] == ["beta", "alpha"]
    assert body["items"][0]["revision_number"] == 1


def test_red_link_turns_blue_when_target_is_created(client, make_page) -> None:
    """The render cache is keyed on rev_id, so this is the case it must not miss."""
    make_page("Source", "go to [[Destination]]")
    assert client.get("/api/pages/source").json()["links"] == [
        {"slug": "destination", "exists": False, "via_redirect": False}
    ]

    make_page("Destination", "here")
    fetched = client.get("/api/pages/source").json()
    assert fetched["links"] == [
        {"slug": "destination", "exists": True, "via_redirect": False}
    ]
    assert 'data-exists="true"' in fetched["html"]


def test_backlinks(client, make_page) -> None:
    make_page("Target", "hi")
    make_page("Referrer", "see [[Target]]")
    body = client.get("/api/pages/target/backlinks").json()
    assert [item["slug"] for item in body["items"]] == ["referrer"]


def test_backlinks_survive_target_deletion(client, make_page) -> None:
    make_page("Target", "hi")
    make_page("Referrer", "see [[Target]]")
    client.delete("/api/pages/target")
    # The referrer keeps its link; it just goes red again.
    assert client.get("/api/pages/referrer").json()["links"] == [
        {"slug": "target", "exists": False, "via_redirect": False}
    ]
