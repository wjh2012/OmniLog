"""Maintenance routes for the aliases left behind by renames."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response, status

from .. import repository
from ..auth import EDITOR, VIEWER
from ..deps import Conn, require_role
from ..errors import PageNotFound
from ..schemas import RedirectList, RedirectRef
from ..slug import slugify

router = APIRouter()

AliasPath = Path(min_length=1, max_length=300, description="The old slug")

EDITOR_TAG = "role-editor"


@router.get(
    "/redirects",
    response_model=RedirectList,
    summary="List every old name",
    operation_id="list_redirects",
    dependencies=[Depends(require_role(VIEWER))],
)
def list_redirects(
    conn: Conn,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = repository.list_redirects(conn, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [RedirectRef(**dict(row)) for row in rows],
    }


@router.delete(
    "/redirects/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop an old name",
    operation_id="delete_redirect",
    tags=[EDITOR_TAG],
    dependencies=[Depends(require_role(EDITOR))],
)
def delete_redirect(conn: Conn, slug: str = AliasPath) -> Response:
    normalised = slugify(slug, strict=False)
    if not normalised:
        raise PageNotFound(slug)
    repository.remove_redirect(conn, normalised)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
