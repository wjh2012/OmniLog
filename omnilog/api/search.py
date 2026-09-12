"""Search routes: text, semantic, and the two fused."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import repository
from ..auth import VIEWER
from ..deps import Conn, Embedder, OptionalEmbedder, require_role
from ..schemas import (
    HybridHit,
    HybridSearchResult,
    SearchHit,
    SearchResult,
    SemanticHit,
    SemanticSearchResult,
)

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
                anchor=row["anchor"],
                excerpt=row["chunk_text"][:_EXCERPT_CHARS],
                score=score,
            )
            for row, score in hits
        ],
    }


@router.get(
    "/search/hybrid",
    response_model=HybridSearchResult,
    summary="Search pages by words and by meaning at once",
    operation_id="hybrid_search_pages",
    dependencies=[Depends(require_role(VIEWER))],
)
def hybrid_search(
    conn: Conn,
    embedder: OptionalEmbedder,
    q: str = Query(min_length=1, max_length=2000, description="Search text"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    """`/search` and `/search/semantic` fused into one ranking.

    The two miss in opposite directions: trigram cannot match a synonym it
    never sees, and cosine similarity is vague about an exact string -- a
    product name or an error code that has to match character for character.
    Reciprocal Rank Fusion merges them on rank position, so a page both
    rankings place well beats one that only a single ranking loved.

    No `offset`: past the fusion depth the merged order stops meaning
    anything, so this endpoint pages no further than `limit`.
    """
    result = repository.hybrid_search(conn, q, embedder, limit)
    return {
        "query": q,
        "limit": limit,
        "model_id": result["model_id"],
        "items": [
            HybridHit(
                slug=item["slug"],
                title=item["title"],
                updated_at=item["updated_at"],
                matched_by=item["matched_by"],
                score=item["score"],
                text_rank=item["text_rank"],
                semantic_rank=item["semantic_rank"],
                snippet=(
                    repository.highlight_to_html(item["snippet"])
                    if item["snippet"] is not None
                    else None
                ),
                anchor=item["anchor"],
                excerpt=(
                    item["chunk_text"][:_EXCERPT_CHARS]
                    if item["chunk_text"] is not None
                    else None
                ),
            )
            for item in result["items"]
        ],
    }
