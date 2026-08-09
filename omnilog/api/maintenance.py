"""Offline storage upkeep.

Writes always take the cheap path and store a whole body; shrinking happens
here, separately, the way `git gc` is separate from `git commit`.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import repository
from ..deps import Config, Conn
from ..schemas import CompactResult

router = APIRouter()


@router.post(
    "/maintenance/compact",
    response_model=CompactResult,
    summary="Re-encode older revision bodies as deltas and bundle them",
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
