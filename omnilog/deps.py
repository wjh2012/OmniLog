"""FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .auth import has_role
from .config import Settings
from .db import connect
from .embeddings import EmbeddingProvider, get_embedding_provider
from .errors import EmbeddingUnavailable


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request; SQLite makes that cheap."""
    conn = connect(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


def get_embedder(settings: Settings = Depends(get_settings)) -> EmbeddingProvider:
    """One provider instance per request -- cheap; it just wraps an HTTP client.

    Construction itself can fail (e.g. the OpenAI SDK raises immediately when
    no API key is configured), so that failure is caught here and turned into
    the same WikiError shape every other route error takes, rather than
    surfacing as a bare 500.
    """
    try:
        return get_embedding_provider(settings)
    except EmbeddingUnavailable:
        raise
    except Exception as exc:
        raise EmbeddingUnavailable(str(exc)) from exc


def get_optional_embedder(
    settings: Settings = Depends(get_settings),
) -> EmbeddingProvider | None:
    """`get_embedder`, but None instead of an error when none can be built.

    For routes that still have an answer without embeddings -- hybrid search
    falls back to its text half -- so an unconfigured or broken provider
    degrades the result rather than failing the request.
    """
    try:
        return get_embedding_provider(settings)
    except Exception:
        return None


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Config = Annotated[Settings, Depends(get_settings)]
Embedder = Annotated[EmbeddingProvider, Depends(get_embedder)]
OptionalEmbedder = Annotated[EmbeddingProvider | None, Depends(get_optional_embedder)]


def require_role(role: str):
    """Route dependency: reject unless the bearer token holds `role`.

    Reads the header off `Request` rather than declaring it as a `Header()`
    parameter, so it stays out of the route's OpenAPI schema — otherwise it
    would surface as a caller-fillable argument on every generated MCP tool,
    letting a caller override the internal bridge's own Authorization header
    (see omnilog/mcp.py).
    """

    def _check(settings: Config, request: Request) -> None:
        authorization = request.headers.get("authorization")
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[len("bearer ") :].strip()
        if not has_role(settings, token, role):
            code = status.HTTP_401_UNAUTHORIZED if token is None else status.HTTP_403_FORBIDDEN
            raise HTTPException(status_code=code, detail=f"Requires {role!r} access.")

    return _check
