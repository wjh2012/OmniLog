from __future__ import annotations


def test_add_an_alias_by_hand(client, make_page) -> None:
    make_page("Canonical", "x")
    response = client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})
    assert response.status_code == 201
    assert [item["from_slug"] for item in response.json()["items"]] == ["nickname"]
    assert client.get("/api/pages/nickname").json()["slug"] == "canonical"


def test_alias_cannot_shadow_an_existing_page(client, make_page) -> None:
    make_page("One")
    make_page("Two")
    response = client.post("/api/pages/one/redirects", json={"slug": "Two"})
    assert response.status_code == 409
    assert response.json()["code"] == "slug_conflict"


def test_alias_cannot_be_stolen_from_another_page(client, make_page) -> None:
    make_page("One", "x")
    make_page("Two", "y")
    client.post("/api/pages/one/redirects", json={"slug": "Shared"})
    response = client.post("/api/pages/two/redirects", json={"slug": "Shared"})
    assert response.status_code == 409
    assert response.json()["code"] == "redirect_conflict"


def test_alias_cannot_be_a_pages_own_slug(client, make_page) -> None:
    make_page("Self", "x")
    response = client.post("/api/pages/self/redirects", json={"slug": "Self"})
    assert response.status_code == 409
    assert response.json()["code"] == "redirect_conflict"


def test_adding_the_same_alias_twice_is_idempotent(client, make_page) -> None:
    make_page("Canonical", "x")
    client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})
    response = client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})
    assert response.status_code == 201
    assert len(response.json()["items"]) == 1


def test_delete_an_alias(client, make_page) -> None:
    make_page("Canonical", "x")
    client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})
    assert client.delete("/api/redirects/nickname").status_code == 204
    assert client.get("/api/pages/nickname").status_code == 404
    assert client.get("/api/pages/canonical").status_code == 200


def test_deleting_a_missing_alias_is_404(client) -> None:
    response = client.delete("/api/redirects/nothing-here")
    assert response.status_code == 404
    assert response.json()["code"] == "redirect_not_found"


def test_deleting_by_page_route_removes_only_the_alias(client, make_page) -> None:
    """DELETE /pages/<old-name> must not destroy the page behind the alias."""
    make_page("Canonical", "x")
    client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})

    response = client.delete("/api/pages/nickname")
    assert response.status_code == 204
    assert response.headers["x-deleted-kind"] == "redirect"
    assert client.get("/api/pages/canonical").status_code == 200


def test_deleting_a_real_page_reports_its_kind(client, make_page) -> None:
    make_page("Doomed")
    response = client.delete("/api/pages/doomed")
    assert response.headers["x-deleted-kind"] == "page"


def test_list_redirects(client, make_page) -> None:
    make_page("Canonical", "x")
    client.post("/api/pages/canonical/redirects", json={"slug": "A"})
    client.post("/api/pages/canonical/redirects", json={"slug": "B"})
    body = client.get("/api/redirects").json()
    assert body["total"] == 2
    assert {item["to_slug"] for item in body["items"]} == {"canonical"}


def test_alias_added_after_render_invalidates_the_cache(client, make_page) -> None:
    """A red link must turn blue when an alias, not a page, appears under it."""
    make_page("Canonical", "x")
    make_page("Referrer", "see [[Nickname]]")
    assert client.get("/api/pages/referrer").json()["links"] == [
        {"slug": "nickname", "exists": False, "via_redirect": False}
    ]

    client.post("/api/pages/canonical/redirects", json={"slug": "Nickname"})
    assert client.get("/api/pages/referrer").json()["links"] == [
        {"slug": "nickname", "exists": True, "via_redirect": True}
    ]


def test_korean_alias(client, make_page) -> None:
    make_page("위키 시스템", "본문")
    client.post("/api/pages/위키-시스템/redirects", json={"slug": "위키 엔진"})
    assert client.get("/api/pages/위키-엔진").json()["slug"] == "위키-시스템"
