"""Page, revision, diff and backlink routes."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Path, Query, Response, status

from .. import repository
from ..auth import EDITOR, VIEWER
from ..deps import Config, Conn, require_role
from ..errors import PageNotFound
from ..repository import PageView
from ..schemas import (
    BacklinkList,
    BacklinkRef,
    DiffResult,
    LinkRef,
    PageCreate,
    PageDetail,
    PageList,
    PageRedirects,
    PageSaved,
    PageSummary,
    PageUpdate,
    RedirectCreate,
    RedirectRef,
    RenameRequest,
    RevertRequest,
    RevisionList,
    RevisionMeta,
)
from ..slug import slugify

router = APIRouter()

SlugPath = Path(min_length=1, max_length=300, description="Page slug")

# Tags double as the MCP scope gate: see omnilog.mcp.restrict_tag wiring.
EDITOR_TAG = "role-editor"
_editor = [Depends(require_role(EDITOR))]
_viewer = [Depends(require_role(VIEWER))]


def _normalise(raw_slug: str) -> str:
    """Accept a title-ish path segment as well as an exact slug."""
    slug = slugify(raw_slug, strict=False)
    if not slug:
        raise PageNotFound(raw_slug)
    return slug


def _revision_meta(row: sqlite3.Row) -> RevisionMeta:
    return RevisionMeta(
        id=row["id"],
        number=row["number"],
        title=row["title"],
        comment=row["comment"],
        author=row["author"],
        byte_size=row["byte_size"],
        parent_id=row["parent_id"],
        created_at=row["created_at"],
    )


def _detail(view: PageView) -> dict:
    return {
        "slug": view.slug,
        "title": view.title,
        "content": view.content,
        "html": view.html,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
        "revision": _revision_meta(view.revision),
        "links": [
            LinkRef(slug=link.slug, exists=link.exists, via_redirect=link.via_redirect)
            for link in view.links
        ],
        "redirected_from": view.redirected_from,
    }


@router.post(
    "/pages",
    response_model=PageDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a page",
    operation_id="create_page",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def create_page(payload: PageCreate, conn: Conn, settings: Config) -> dict:
    title = payload.title.strip()
    view = repository.create_page(
        conn,
        slug=slugify(payload.slug or title),
        title=title,
        content=payload.content,
        author=payload.author,
        comment=payload.comment,
        settings=settings,
    )
    return _detail(view)


@router.get(
    "/pages",
    response_model=PageList,
    summary="List pages",
    operation_id="list_pages",
    dependencies=_viewer,
)
def list_pages(
    conn: Conn,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = repository.list_pages(conn, limit, offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [PageSummary(**dict(row)) for row in rows],
    }


@router.get(
    "/pages/{slug}",
    response_model=PageDetail,
    summary="Read a page",
    operation_id="read_page",
    dependencies=_viewer,
)
def read_page(conn: Conn, settings: Config, slug: str = SlugPath) -> dict:
    return _detail(repository.get_page(conn, _normalise(slug), settings))


@router.put(
    "/pages/{slug}",
    response_model=PageSaved,
    summary="Save a new revision",
    operation_id="update_page",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def update_page(payload: PageUpdate, conn: Conn, settings: Config, slug: str = SlugPath) -> dict:
    view, changed = repository.update_page(
        conn,
        slug=_normalise(slug),
        title=payload.title.strip() if payload.title else None,
        content=payload.content,
        author=payload.author,
        comment=payload.comment,
        base_revision=payload.base_revision,
        settings=settings,
    )
    return {**_detail(view), "changed": changed}


@router.delete(
    "/pages/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a page, or an old name",
    description=(
        "Does not follow old names. If the slug is an alias left behind by a "
        "rename, only that alias is removed; the page it points at survives."
    ),
    operation_id="delete_page",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def delete_page(conn: Conn, slug: str = SlugPath) -> Response:
    deleted = repository.delete_page(conn, _normalise(slug))
    return Response(
        status_code=status.HTTP_204_NO_CONTENT, headers={"X-Deleted-Kind": deleted}
    )


@router.post(
    "/pages/{slug}/rename",
    response_model=PageSaved,
    summary="Move a page to a new slug and/or title",
    operation_id="rename_page",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def rename_page(
    payload: RenameRequest, conn: Conn, settings: Config, slug: str = SlugPath
) -> dict:
    view, changed = repository.rename_page(
        conn,
        slug=_normalise(slug),
        new_slug=slugify(payload.slug) if payload.slug else None,
        new_title=payload.title.strip() if payload.title else None,
        author=payload.author,
        comment=payload.comment,
        leave_redirect=payload.leave_redirect,
        settings=settings,
    )
    return {**_detail(view), "changed": changed}


@router.get(
    "/pages/{slug}/redirects",
    response_model=PageRedirects,
    summary="Old names pointing at this page",
    operation_id="list_page_redirects",
    dependencies=_viewer,
)
def page_redirects(conn: Conn, slug: str = SlugPath) -> dict:
    rows, resolved = repository.redirects_to(conn, _normalise(slug))
    return {"slug": resolved, "items": [RedirectRef(**dict(row)) for row in rows]}


@router.post(
    "/pages/{slug}/redirects",
    response_model=PageRedirects,
    status_code=status.HTTP_201_CREATED,
    summary="Point another slug at this page",
    operation_id="add_page_redirect",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def add_page_redirect(payload: RedirectCreate, conn: Conn, slug: str = SlugPath) -> dict:
    resolved = repository.add_redirect(
        conn,
        slug=_normalise(slug),
        alias=slugify(payload.slug),
        author=payload.author,
    )
    rows, _ = repository.redirects_to(conn, resolved)
    return {"slug": resolved, "items": [RedirectRef(**dict(row)) for row in rows]}


@router.get(
    "/pages/{slug}/revisions",
    response_model=RevisionList,
    summary="List revisions",
    operation_id="list_revisions",
    dependencies=_viewer,
)
def list_revisions(
    conn: Conn,
    slug: str = SlugPath,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total, resolved = repository.list_revisions(conn, _normalise(slug), limit, offset)
    return {
        "slug": resolved,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_revision_meta(row) for row in rows],
    }


@router.get(
    "/pages/{slug}/revisions/{number}",
    response_model=PageDetail,
    summary="Read one revision",
    operation_id="read_revision",
    dependencies=_viewer,
)
def read_revision(
    conn: Conn,
    settings: Config,
    slug: str = SlugPath,
    number: int = Path(ge=1),
) -> dict:
    return _detail(repository.get_revision(conn, _normalise(slug), number, settings))


@router.get(
    "/pages/{slug}/diff",
    response_model=DiffResult,
    summary="Diff two revisions",
    operation_id="diff_page",
    dependencies=_viewer,
)
def diff_page(
    conn: Conn,
    slug: str = SlugPath,
    from_revision: int = Query(alias="from", ge=1),
    to_revision: int = Query(alias="to", ge=1),
) -> dict:
    return repository.diff_revisions(conn, _normalise(slug), from_revision, to_revision)


@router.post(
    "/pages/{slug}/revisions/{number}/revert",
    response_model=PageSaved,
    summary="Restore an old revision as a new one",
    operation_id="revert_page",
    tags=[EDITOR_TAG],
    dependencies=_editor,
)
def revert_page(
    conn: Conn,
    settings: Config,
    payload: RevertRequest | None = None,
    slug: str = SlugPath,
    number: int = Path(ge=1),
) -> dict:
    request = payload or RevertRequest()
    view, changed = repository.revert_page(
        conn,
        slug=_normalise(slug),
        number=number,
        author=request.author,
        comment=request.comment,
        settings=settings,
    )
    return {**_detail(view), "changed": changed}


@router.get(
    "/pages/{slug}/backlinks",
    response_model=BacklinkList,
    summary="Pages linking here",
    operation_id="page_backlinks",
    dependencies=_viewer,
)
def page_backlinks(conn: Conn, slug: str = SlugPath) -> dict:
    rows, resolved = repository.backlinks(conn, _normalise(slug))
    return {"slug": resolved, "items": [BacklinkRef(**dict(row)) for row in rows]}
