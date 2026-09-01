"""Full-text search route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import repository
from ..auth import VIEWER
from ..deps import Conn, Embedder, require_role
from ..schemas import SearchHit, SearchResult, SemanticHit, SemanticSearchResult

router = APIRouter()

#: Chars of the embedded chunk shown back per hit, so a caller can sanity-check
#: a match without a second request. Not HTML-escaped -- this is plain text,
#: unlike SearchHit.snippet which carries <mark> tags meant for a browser.
_EXCERPT_CHARS = 200


@router.get(
    "/search",
    response_model=SearchResult,
    summary="Search pages",
    operation_id="search_pages",
    dependencies=[Depends(require_role(VIEWER))],
)
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


@router.get(
    "/search/semantic",
    response_model=SemanticSearchResult,
    summary="Search pages by meaning rather than matching words",
    operation_id="semantic_search_pages",
    dependencies=[Depends(require_role(VIEWER))],
)
def semantic_search(
    conn: Conn,
    embedder: Embedder,
    q: str = Query(min_length=1, max_length=2000, description="Search text"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Cosine search over `page_embedding`.

    Answers a query that shares no words with the target page -- the gap
    `/search`'s trigram index cannot close, since it only ever matches
    characters that are actually there. Requires `POST
    /maintenance/reindex-embeddings` to have run at least once; a page with no
    stored embedding simply cannot be found here yet.
    """
    hits = repository.semantic_search(conn, q, embedder, limit)
    return {
        "query": q,
        "model_id": embedder.model_id,
        "limit": limit,
        "items": [
            SemanticHit(
                slug=row["slug"],
                title=row["title"],
                updated_at=row["updated_at"],
                excerpt=row["chunk_text"][:_EXCERPT_CHARS],
                score=score,
            )
            for row, score in hits
        ],
    }
