from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings

VIEWER_KEY = "sk-viewer"
EDITOR_KEY = "sk-editor"
ADMIN_KEY = "sk-admin"


@pytest.fixture
def guarded_client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "wiki.db",
        compress_min_bytes=64,
        api_keys={
            VIEWER_KEY: ("viewer",),
            EDITOR_KEY: ("viewer", "editor"),
            ADMIN_KEY: ("viewer", "editor", "admin"),
        },
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_open_by_default(client, make_page) -> None:
    """No api_keys configured: every route is open, matching pre-auth behaviour."""
    make_page("Open Door")
    assert client.get("/api/pages/open-door").status_code == 200


def test_write_without_token_is_401(guarded_client) -> None:
    response = guarded_client.post("/api/pages", json={"title": "X"})
    assert response.status_code == 401


def test_write_with_viewer_token_is_403(guarded_client) -> None:
    response = guarded_client.post(
        "/api/pages", json={"title": "X"}, headers=auth(VIEWER_KEY)
    )
    assert response.status_code == 403


def test_write_with_editor_token_succeeds(guarded_client) -> None:
    response = guarded_client.post(
        "/api/pages", json={"title": "X"}, headers=auth(EDITOR_KEY)
    )
    assert response.status_code == 201


def test_read_with_viewer_token_succeeds(guarded_client) -> None:
    guarded_client.post("/api/pages", json={"title": "X"}, headers=auth(EDITOR_KEY))
    response = guarded_client.get("/api/pages/x", headers=auth(VIEWER_KEY))
    assert response.status_code == 200


def test_maintenance_requires_admin(guarded_client) -> None:
    response = guarded_client.post(
        "/api/maintenance/compact", headers=auth(EDITOR_KEY)
    )
    assert response.status_code == 403

    response = guarded_client.post("/api/maintenance/compact", headers=auth(ADMIN_KEY))
    assert response.status_code == 200


def test_health_stays_open(guarded_client) -> None:
    assert guarded_client.get("/api/health").status_code == 200


def test_source_write_requires_editor(guarded_client) -> None:
    payload = {"title": "Mediawiki", "kind": "link", "url": "https://example.org/mw"}
    assert guarded_client.post("/api/sources", json=payload).status_code == 401
    assert (
        guarded_client.post(
            "/api/sources", json=payload, headers=auth(VIEWER_KEY)
        ).status_code
        == 403
    )
    assert (
        guarded_client.post(
            "/api/sources", json=payload, headers=auth(EDITOR_KEY)
        ).status_code
        == 201
    )


def test_source_read_requires_viewer(guarded_client) -> None:
    payload = {"title": "Mediawiki", "kind": "link", "url": "https://example.org/mw"}
    guarded_client.post("/api/sources", json=payload, headers=auth(EDITOR_KEY))
    assert guarded_client.get("/api/sources/mediawiki").status_code == 401
    assert (
        guarded_client.get(
            "/api/sources/mediawiki", headers=auth(VIEWER_KEY)
        ).status_code
        == 200
    )


def test_citations_read_requires_viewer(guarded_client) -> None:
    assert guarded_client.get("/api/citations").status_code == 401
    assert guarded_client.get("/api/citations", headers=auth(VIEWER_KEY)).status_code == 200


#: Routes that are deliberately open to anyone.
OPEN_ROUTES = {("/api/health", "GET")}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ROLE_TAGS = {"role-editor", "role-admin"}
#: Stand-ins for path parameters; a gated route rejects before it looks them up.
PLACEHOLDERS = {"{slug}": "nope", "{key}": "nope", "{number}": "1"}


def _api_operations(app):
    """(path, METHOD, tags) for every documented /api route."""
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method, operation in operations.items():
            if method.upper() in WRITE_METHODS | {"GET"}:
                yield path, method.upper(), set(operation.get("tags", []))


def test_every_api_route_requires_a_token(guarded_client) -> None:
    """A new route that forgets its role gate is open to the world; catch it here."""
    open_to_anyone = []
    for path, method, _ in _api_operations(guarded_client.app):
        if (path, method) in OPEN_ROUTES:
            continue
        url = path
        for parameter, value in PLACEHOLDERS.items():
            url = url.replace(parameter, value)
        status = guarded_client.request(method, url).status_code
        if status != 401:
            open_to_anyone.append(f"{method} {path} -> {status}")
    assert open_to_anyone == []


def test_every_write_route_is_tagged_for_mcp(guarded_client) -> None:
    """The MCP side gates on the tag, not the dependency: the two must agree."""
    untagged = [
        f"{method} {path}"
        for path, method, tags in _api_operations(guarded_client.app)
        if method in WRITE_METHODS and not tags & ROLE_TAGS
    ]
    assert untagged == []
