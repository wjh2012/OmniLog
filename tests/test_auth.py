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
