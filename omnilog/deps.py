"""FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .auth import has_role
from .config import Settings
from .db import connect


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request; SQLite makes that cheap."""
    conn = connect(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Config = Annotated[Settings, Depends(get_settings)]


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
