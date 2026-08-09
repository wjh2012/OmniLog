"""Full-text search route."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import repository
from ..deps import Conn
from ..schemas import SearchHit, SearchResult

router = APIRouter()


@router.get("/search", response_model=SearchResult, summary="Search pages")
def search(
    conn: Conn,
    q: str = Query(min_length=1, max_length=200, description="Search text"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    hits = repository.search(conn, q, limit, offset)
    return {
        "query": q,
        "limit": limit,
        "offset": offset,
        "items": [
            SearchHit(
                slug=hit["slug"],
                title=hit["title"],
                updated_at=hit["updated_at"],
                snippet=repository.highlight_to_html(hit["snippet"]),
                score=hit["score"],
            )
            for hit in hits
        ],
    }
