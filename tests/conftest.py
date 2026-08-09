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


@pytest.fixture
def make_page(client):
    def _make(title: str, content: str = "", **kwargs):
        response = client.post(
            "/api/pages", json={"title": title, "content": content, **kwargs}
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make
