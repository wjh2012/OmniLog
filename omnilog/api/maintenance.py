"""Offline storage upkeep.

Writes always take the cheap path and store a whole body; shrinking happens
here, separately, the way `git gc` is separate from `git commit`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import repository
from ..auth import ADMIN
from ..deps import Config, Conn, Embedder, require_role
from ..schemas import CompactResult, EmbeddingReindexResult

router = APIRouter()

ADMIN_TAG = "role-admin"


@router.post(
    "/maintenance/compact",
    response_model=CompactResult,
    summary="Re-encode older revision bodies as deltas and bundle them",
    operation_id="compact_storage",
    tags=[ADMIN_TAG],
    dependencies=[Depends(require_role(ADMIN))],
)
def compact(
    conn: Conn,
    settings: Config,
    vacuum: bool = Query(
        default=True,
        description="Also VACUUM, which is what actually returns pages to the filesystem.",
    ),
) -> dict:
    file_before = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    totals = repository.compact_all(conn)

    if vacuum:
        # VACUUM cannot run inside a transaction, and rewrites the whole file.
        conn.execute("VACUUM")
        # Under WAL the rebuild lands in the -wal file first, so the main file
        # keeps its old size until a checkpoint folds the pages back in.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    file_after = settings.db_path.stat().st_size if settings.db_path.exists() else 0

    return {
        "pages": totals["pages"],
        "converted": totals["converted"],
        "bundled": totals["bundled"],
        "bytes_before": totals["before"],
        "bytes_after": totals["after"],
        "bytes_reclaimed": totals["reclaimed"],
        "file_before": file_before,
        "file_after": file_after,
    }


@router.post(
    "/maintenance/reindex-embeddings",
    response_model=EmbeddingReindexResult,
    summary="(Re)compute embeddings for pages that are missing one or out of date",
    operation_id="reindex_embeddings",
    tags=[ADMIN_TAG],
    dependencies=[Depends(require_role(ADMIN))],
)
def reindex_embeddings(
    conn: Conn,
    embedder: Embedder,
    limit: int | None = Query(
        default=None, ge=1, description="Cap how many pages this call processes."
    ),
) -> dict:
    """Bring `page_embedding` up to date under the currently configured model.

    Calls out to the embedding provider once per stale page, so unlike
    /maintenance/compact this is not free of external dependencies -- run it
    after edits, or after switching OMNILOG_EMBEDDING_* to a different model
    (every page looks stale to the new model and gets recomputed). Already
    current rows are left alone, so it is always safe to re-run.
    """
    return repository.reindex_embeddings(conn, embedder, limit=limit)
