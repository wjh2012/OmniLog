"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api import api_router
from .config import Settings, load_settings
from .db import init_db
from .errors import WikiError

DESCRIPTION = """
A wiki served as JSON.

Page bodies are markdown with `[[wikilink]]` support, plus citations for
pointing a sentence at where it came from: `[^name]` with a definition line for
a one-off, or `[^@key]` for a source in the registry — a link, a quoted passage
or an uploaded file, managed apart from any page and correctable in one place.

Every save appends a revision; nothing is ever overwritten in place, so history
and diffs stay available.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(resolved)
        yield

    app = FastAPI(
        title="OmniLog Wiki",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = resolved

    @app.exception_handler(WikiError)
    async def handle_wiki_error(_: Request, exc: WikiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.get("/api/health", tags=["meta"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router, prefix="/api")
    return app
