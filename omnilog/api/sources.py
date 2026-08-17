"""The source registry: sources managed apart from any page.

A page body only ever holds a key (``[^@mw-text-table]``). Everything about the
source — title, URL, the quoted passage, the file — lives here, so correcting
it corrects every page at once.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from .. import repository
from ..deps import Config, Conn
from ..errors import FileTooLarge, SourceNotFound
from ..repository import SourceView
from ..schemas import (
    MAX_FILENAME_LENGTH,
    FileRef,
    SourceCitations,
    SourceCreate,
    SourceDetail,
    SourceList,
    SourceSummary,
    SourceUpdate,
    SourceUse,
)
from ..slug import slugify

router = APIRouter()

KeyPath = Path(min_length=1, max_length=300, description="Source key")

#: Types safe to render in the browser tab. Anything else is handed over as a
#: download: an uploaded .html or .svg served inline would be a script running
#: on this origin, which is the one thing the markup rules already forbid.
_INLINE_SAFE = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
)
_MEDIA_TYPE = re.compile(r"^[\w.+-]+/[\w.+-]+$")
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


def _normalise(raw_key: str) -> str:
    """Accept a title-ish path segment as well as an exact key."""
    key = slugify(raw_key, strict=False)
    if not key:
        raise SourceNotFound(raw_key)
    return key


def _safe_media_type(declared: str) -> str:
    """Keep only a well-formed type; it ends up in a response header."""
    media_type = declared.split(";")[0].strip().lower()
    return media_type if _MEDIA_TYPE.match(media_type) else _DEFAULT_MEDIA_TYPE


def _safe_filename(raw: str) -> str:
    """Strip anything that would make a filename mean a path."""
    name = raw.replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"')
    return name[:MAX_FILENAME_LENGTH]


def _detail(view: SourceView) -> dict:
    return {
        "key": view.key,
        "kind": view.kind,
        "title": view.title,
        "url": view.url,
        "host": view.host,
        "author": view.author,
        "published": view.published,
        "locator": view.locator,
        "note": view.note,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
        "text": view.text,
        "file": None if view.file is None else FileRef(**view.file._asdict()),
        "page_count": view.page_count,
    }


@router.post(
    "/sources",
    response_model=SourceDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Register a source",
    description=(
        "kind='link' needs a url, kind='text' needs its text, kind='file' takes "
        "its bytes afterwards through PUT /sources/{key}/file."
    ),
)
def create_source(payload: SourceCreate, conn: Conn, settings: Config) -> dict:
    title = payload.title.strip()
    view = repository.create_source(
        conn,
        key=slugify(payload.key or title),
        kind=payload.kind,
        title=title,
        url=payload.url,
        text=payload.text,
        author=payload.author.strip(),
        published=payload.published.strip(),
        locator=payload.locator.strip(),
        note=payload.note,
        settings=settings,
    )
    return _detail(view)


@router.get("/sources", response_model=SourceList, summary="List registered sources")
def list_sources(
    conn: Conn,
    kind: str | None = Query(default=None, description="link, text or file"),
    host: str | None = Query(default=None, max_length=300, description="Exact hostname"),
    q: str | None = Query(
        default=None, max_length=300, description="Substring of key, title, author or url"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = repository.list_sources(
        conn, kind=kind, host=host, query=q, limit=limit, offset=offset
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [SourceSummary(**dict(row)) for row in rows],
    }


@router.get("/sources/{key}", response_model=SourceDetail, summary="Read a source")
def read_source(conn: Conn, key: str = KeyPath) -> dict:
    return _detail(repository.get_source(conn, _normalise(key)))


@router.put("/sources/{key}", response_model=SourceDetail, summary="Correct a source")
def update_source(
    payload: SourceUpdate, conn: Conn, settings: Config, key: str = KeyPath
) -> dict:
    view = repository.update_source(
        conn,
        key=_normalise(key),
        title=payload.title,
        url=payload.url,
        text=payload.text,
        author=payload.author,
        published=payload.published,
        locator=payload.locator,
        note=payload.note,
        settings=settings,
    )
    return _detail(view)


@router.delete(
    "/sources/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a source",
    description=(
        "Page bodies still say [^@key], so their citations start rendering as "
        "missing — the same thing that happens to a wikilink when its page goes. "
        "X-Dangling-Citations reports how many pages that is."
    ),
)
def delete_source(conn: Conn, key: str = KeyPath) -> Response:
    dangling = repository.delete_source(conn, _normalise(key))
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Dangling-Citations": str(dangling)},
    )


@router.get(
    "/sources/{key}/citations",
    response_model=SourceCitations,
    summary="Pages citing this source",
)
def source_citations(conn: Conn, key: str = KeyPath) -> dict:
    resolved = _normalise(key)
    rows = repository.source_citations(conn, resolved)
    return {"key": resolved, "items": [SourceUse(**dict(row)) for row in rows]}


@router.put(
    "/sources/{key}/file",
    response_model=SourceDetail,
    summary="Attach or replace the file of a file source",
    description=(
        "The request body is the file itself, and Content-Type is taken as its "
        "media type. Raw bytes rather than multipart: this API speaks JSON and "
        "one upload does not justify a form parser."
    ),
)
async def upload_source_file(
    request: Request,
    conn: Conn,
    settings: Config,
    key: str = KeyPath,
    filename: str = Query(default="", max_length=MAX_FILENAME_LENGTH),
) -> dict:
    limit = settings.max_file_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise FileTooLarge(int(declared), limit)

    # Counted while streaming, so a lying Content-Length cannot get past it.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise FileTooLarge(size, limit)
        chunks.append(chunk)

    resolved = _normalise(key)
    view = await run_in_threadpool(
        repository.attach_file,
        conn,
        key=resolved,
        data=b"".join(chunks),
        filename=_safe_filename(filename) or f"{resolved}.bin",
        media_type=_safe_media_type(request.headers.get("content-type", "")),
        settings=settings,
    )
    return _detail(view)


@router.get(
    "/sources/{key}/file",
    response_class=Response,
    summary="Download the file of a file source",
    responses={200: {"content": {"*/*": {}}, "description": "The stored bytes"}},
)
def download_source_file(conn: Conn, key: str = KeyPath) -> Response:
    data, info = repository.load_file(conn, _normalise(key))
    disposition = "inline" if info.media_type in _INLINE_SAFE else "attachment"
    return Response(
        content=data,
        media_type=info.media_type,
        headers={
            # RFC 5987 form, so Korean filenames survive the trip.
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(info.filename)}",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{info.sha256}"',
        },
    )
