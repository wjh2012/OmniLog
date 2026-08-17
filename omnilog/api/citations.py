"""Wiki-wide view of the external sources pages cite."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import repository
from ..deps import Conn
from ..schemas import CitationList

router = APIRouter()


@router.get(
    "/citations",
    response_model=CitationList,
    summary="List everything the wiki cites, grouped by source",
    description=(
        "Registered sources and inline definitions side by side, most-cited "
        "first, each with the pages citing it. `key` or `url` narrows it to one "
        "source, which is the reverse lookup: who cites this? `host` gathers a "
        "whole domain, which is where a link-rot sweep starts."
    ),
)
def list_citations(
    conn: Conn,
    key: str | None = Query(default=None, max_length=300, description="Exact source key"),
    host: str | None = Query(default=None, max_length=300, description="Exact hostname"),
    url: str | None = Query(default=None, max_length=2000, description="Exact URL"),
    q: str | None = Query(
        default=None, max_length=300, description="Substring of the key, URL or title"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = repository.list_citations(
        conn, host=host, url=url, key=key, query=q, limit=limit, offset=offset
    )
    return {"total": total, "limit": limit, "offset": offset, "items": items}
