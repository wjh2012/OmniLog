"""Data access for pages, revisions, links and search."""

from __future__ import annotations

import difflib
import gzip
import hashlib
import html
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

from . import markup, vectorstore
from .config import Settings
from .db import write_tx
from .embeddings.base import EmbeddingProvider
from .errors import (
    ContentTooLarge,
    EditConflict,
    EmbeddingUnavailable,
    FileNotAttached,
    FileTooLarge,
    InvalidSource,
    PageNotFound,
    RedirectConflict,
    RedirectNotFound,
    RevisionNotFound,
    SectionNotFound,
    SlugConflict,
    SourceConflict,
    SourceNotFound,
)
from .delta import DeltaError, make_delta
from .markup import STATUS_PAGE, STATUS_REDIRECT, Citation
from .textstore import (
    delete_texts,
    encoded_delta_size,
    is_delta,
    is_packed,
    load_text,
    pack_into_blob,
    replace_with_delta,
    store_text,
    storage_bytes,
    stored_size,
)

#: Compaction keeps every Nth body whole so a read is at most one delta away.
KEYFRAME_INTERVAL = 16

#: Markers handed to FTS5 snippet(), swapped for <mark> after HTML-escaping.
_HL_OPEN = "\x02"
_HL_CLOSE = "\x03"
_IN_CLAUSE_CHUNK = 400


class LinkInfo(NamedTuple):
    slug: str
    exists: bool
    #: True when the target is an old name rather than the page's own slug.
    via_redirect: bool


@dataclass(frozen=True)
class PageView:
    """A page plus the rendered form of one of its revisions."""

    id: int
    slug: str
    title: str
    created_at: str
    updated_at: str
    revision: sqlite3.Row
    content: str
    html: str
    links: tuple[LinkInfo, ...]
    #: External sources this revision cites, numbered.
    citations: tuple[Citation, ...] = ()
    #: Set when the caller asked for one of the page's old names.
    redirected_from: str | None = None


@dataclass(frozen=True)
class SectionView:
    """One heading of a page, sliced out by itself and rendered on its own."""

    slug: str
    anchor: str
    level: int
    title: str
    content: str
    html: str
    #: Set when the caller asked for one of the page's old names.
    redirected_from: str | None = None


def _now() -> str:
    # Milliseconds, so edits landing in the same second still sort deterministically.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(mapping: dict[str, str]) -> str:
    payload = json.dumps(sorted(mapping.items()), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _link_state(status: dict[str, str]) -> str:
    """Fingerprint of how a body's links resolved.

    Covers redirect-ness as well as existence, so pointing an old name
    somewhere else invalidates the cached HTML too.
    """
    return _fingerprint(status)


def _cite_state(sources: dict[str, str]) -> str:
    """Fingerprint of the registered sources a body cited.

    The same idea as `_link_state`, for the other thing a body points at from
    outside itself. A source that gets retitled, repointed, deleted or finally
    registered all show up here as a changed value.
    """
    return _fingerprint(sources)


def _chunks(values: Sequence[str]):
    for start in range(0, len(values), _IN_CLAUSE_CHUNK):
        chunk = list(values[start : start + _IN_CLAUSE_CHUNK])
        yield chunk, ",".join("?" * len(chunk))


def resolve_links(conn: sqlite3.Connection, slugs: Sequence[str]) -> dict[str, str]:
    """Classify link targets as a live page, an old name, or missing.

    Old names count as existing: renaming a page must not turn every link
    written against its previous name red.
    """
    unique = list(dict.fromkeys(slugs))
    status: dict[str, str] = {}
    for chunk, placeholders in _chunks(unique):
        for row in conn.execute(f"SELECT slug FROM page WHERE slug IN ({placeholders})", chunk):
            status[row["slug"]] = STATUS_PAGE

    unresolved = [slug for slug in unique if slug not in status]
    for chunk, placeholders in _chunks(unresolved):
        rows = conn.execute(
            f"SELECT from_slug FROM redirect WHERE from_slug IN ({placeholders})", chunk
        )
        for row in rows:
            status[row["from_slug"]] = STATUS_REDIRECT
    return status


def resolve_sources(
    conn: sqlite3.Connection, keys: Sequence[str]
) -> dict[str, markup.SourceRef]:
    """Look up registered sources by key, for rendering ``[^@key]``.

    Keys with no source are simply absent, the way a missing page is absent
    from `resolve_links`.
    """
    unique = list(dict.fromkeys(keys))
    found: dict[str, markup.SourceRef] = {}
    for chunk, placeholders in _chunks(unique):
        rows = conn.execute(
            f"SELECT key, kind, title, url, host, author, published, locator, updated_at "
            f"FROM source WHERE key IN ({placeholders})",
            chunk,
        )
        for row in rows:
            found[row["key"]] = markup.SourceRef(
                key=row["key"],
                kind=row["kind"],
                title=row["title"],
                url=row["url"],
                host=row["host"],
                author=row["author"],
                published=row["published"],
                locator=row["locator"],
                # Everything the render depends on, so an edit inside the same
                # millisecond still counts as a change.
                fingerprint="|".join(
                    (
                        row["updated_at"],
                        row["kind"],
                        row["title"],
                        row["url"],
                        row["author"],
                        row["published"],
                        row["locator"],
                    )
                ),
            )
    return found


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _link_infos(slugs: Sequence[str], status: dict[str, str]) -> tuple[LinkInfo, ...]:
    return tuple(
        LinkInfo(slug, slug in status, status.get(slug) == STATUS_REDIRECT) for slug in slugs
    )


def render_revision(
    conn: sqlite3.Connection,
    rev_id: int,
    content: str,
    settings: Settings,
    *,
    cacheable: bool = True,
) -> tuple[str, tuple[LinkInfo, ...], tuple[Citation, ...]]:
    """Render a revision, going through render_cache.

    A cache hit is still checked against how the links resolve right now: the
    body is immutable but a red link turns blue the moment its target is
    created, and a live link becomes a redirect the moment its target is
    renamed. Registered sources get the same check, for the same reason —
    someone can correct or delete one without touching this page.

    `cacheable` is false for historical revisions. Storing those made the cache
    grow with the number of revisions ever *viewed*, which is unbounded; only a
    page's current revision earns a row.
    """
    cached = conn.execute(
        "SELECT html_gz, links_json, link_state, cites_json, cite_state "
        "FROM render_cache WHERE rev_id = ?",
        (rev_id,),
    ).fetchone()
    if cached is not None:
        slugs: list[str] = json.loads(cached["links_json"])
        citations = tuple(Citation(**row) for row in json.loads(cached["cites_json"]))
        keys = [cite.source_key for cite in citations if cite.source_key]
        status = resolve_links(conn, slugs)
        sources = resolve_sources(conn, keys) if keys else {}
        state = {key: sources[key].fingerprint if key in sources else "" for key in keys}
        if (
            _link_state(status) == cached["link_state"]
            and _cite_state(state) == cached["cite_state"]
        ):
            html = gzip.decompress(cached["html_gz"]).decode("utf-8")
            return html, _link_infos(slugs, status), citations

    rendered = markup.render(
        content,
        lambda targets: resolve_links(conn, targets),
        settings.link_base,
        resolve_sources=lambda keys: resolve_sources(conn, keys),
        source_base=settings.source_base,
    )
    if cacheable:
        try:
            conn.execute(
                """
                INSERT INTO render_cache
                    (rev_id, html_gz, links_json, link_state, cites_json, cite_state,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (rev_id) DO UPDATE SET
                    html_gz = excluded.html_gz,
                    links_json = excluded.links_json,
                    link_state = excluded.link_state,
                    cites_json = excluded.cites_json,
                    cite_state = excluded.cite_state,
                    created_at = excluded.created_at
                """,
                (
                    rev_id,
                    gzip.compress(rendered.html.encode("utf-8"), 6, mtime=0),
                    json.dumps(list(rendered.slugs), ensure_ascii=False),
                    _link_state(dict(rendered.status)),
                    json.dumps(
                        [cite._asdict() for cite in rendered.citations], ensure_ascii=False
                    ),
                    _cite_state(dict(rendered.sources)),
                    _now(),
                ),
            )
        except sqlite3.OperationalError:
            # Cache writes are best effort; a locked database must not fail a read.
            pass
    return (
        rendered.html,
        _link_infos(rendered.slugs, dict(rendered.status)),
        rendered.citations,
    )


def _drop_stale_cache(conn: sqlite3.Connection, page_id: int, keep_rev_id: int) -> None:
    """Keep at most one cached render per page."""
    conn.execute(
        "DELETE FROM render_cache WHERE rev_id IN "
        "(SELECT id FROM revision WHERE page_id = ? AND id <> ?)",
        (page_id, keep_rev_id),
    )


# --------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------


def _page_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    """The page living at exactly `slug`. Does not follow old names."""
    row = conn.execute("SELECT * FROM page WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise PageNotFound(slug)
    return row


def _resolve_slug(conn: sqlite3.Connection, slug: str) -> tuple[sqlite3.Row, str | None]:
    """Find the page at `slug`, following an old name if that is what it is.

    Returns ``(page, redirected_from)``. Exactly one hop is possible: redirects
    store a page id, so there is nothing to chain to.
    """
    row = conn.execute("SELECT * FROM page WHERE slug = ?", (slug,)).fetchone()
    if row is not None:
        return row, None

    alias = conn.execute(
        "SELECT to_page_id FROM redirect WHERE from_slug = ?", (slug,)
    ).fetchone()
    if alias is None:
        raise PageNotFound(slug)
    target = conn.execute("SELECT * FROM page WHERE id = ?", (alias["to_page_id"],)).fetchone()
    if target is None:  # pragma: no cover - the FK cascade rules this out
        raise PageNotFound(slug)
    return target, slug


def _revision_row(conn: sqlite3.Connection, page_id: int, number: int, slug: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM revision WHERE page_id = ? AND number = ?", (page_id, number)
    ).fetchone()
    if row is None:
        raise RevisionNotFound(slug, number)
    return row


def _latest_revision(conn: sqlite3.Connection, page: sqlite3.Row) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM revision WHERE id = ?", (page["latest_rev_id"],)).fetchone()
    if row is None:  # pragma: no cover - only reachable if the DB is corrupt
        raise PageNotFound(page["slug"])
    return row


def _view(
    conn: sqlite3.Connection,
    page: sqlite3.Row,
    revision: sqlite3.Row,
    settings: Settings,
    redirected_from: str | None = None,
) -> PageView:
    content = load_text(conn, revision["text_id"])
    html, links, citations = render_revision(
        conn,
        revision["id"],
        content,
        settings,
        cacheable=revision["id"] == page["latest_rev_id"],
    )
    return PageView(
        id=page["id"],
        slug=page["slug"],
        title=page["title"],
        created_at=page["created_at"],
        updated_at=page["updated_at"],
        revision=revision,
        content=content,
        html=html,
        links=links,
        citations=citations,
        redirected_from=redirected_from,
    )


def get_page(conn: sqlite3.Connection, slug: str, settings: Settings) -> PageView:
    page, redirected_from = _resolve_slug(conn, slug)
    return _view(conn, page, _latest_revision(conn, page), settings, redirected_from)


def get_revision(
    conn: sqlite3.Connection, slug: str, number: int, settings: Settings
) -> PageView:
    page, redirected_from = _resolve_slug(conn, slug)
    revision = _revision_row(conn, page["id"], number, page["slug"])
    return _view(conn, page, revision, settings, redirected_from)


def get_outline(conn: sqlite3.Connection, slug: str) -> tuple[tuple[markup.Heading, ...], str]:
    """The table of contents of the page reachable from `slug`.

    Loads the body but never renders it, so a caller can see a page's shape --
    and decide whether a section of it is worth reading in full -- for the
    cost of a text load rather than a render.
    """
    page, _ = _resolve_slug(conn, slug)
    content = load_text(conn, _latest_revision(conn, page)["text_id"])
    return markup.extract_headings(content), page["slug"]


def _next_boundary(
    headings: Sequence[markup.Heading], target: markup.Heading
) -> int | None:
    """Line the section after `target` starts on, or None if it runs to the end."""
    for heading in headings:
        if heading.line > target.line and heading.level <= target.level:
            return heading.line
    return None


def get_section(
    conn: sqlite3.Connection, slug: str, anchor: str, settings: Settings
) -> SectionView:
    """One heading of the page reachable from `slug`, plus everything under it.

    A section runs until the next heading at the same level or shallower, so
    it always carries its own subsections along -- the same rule Wikipedia's
    "edit section" link uses. Rendered fresh, the way a historical revision is:
    a fragment is not worth a render_cache row of its own.
    """
    page, redirected_from = _resolve_slug(conn, slug)
    content = load_text(conn, _latest_revision(conn, page)["text_id"])
    headings = markup.extract_headings(content)
    target = next((heading for heading in headings if heading.anchor == anchor), None)
    if target is None:
        raise SectionNotFound(page["slug"], anchor)

    lines = content.splitlines()
    section_content = "\n".join(lines[target.line : _next_boundary(headings, target)])
    rendered = markup.render(
        section_content,
        lambda targets: resolve_links(conn, targets),
        settings.link_base,
        resolve_sources=lambda keys: resolve_sources(conn, keys),
        source_base=settings.source_base,
    )
    html = rendered.html
    if rendered.headings:
        # The slice is parsed on its own, so its first heading -- always
        # `target` itself -- gets renumbered from a clean slate and may not
        # land on the same anchor a duplicate heading earned in the full
        # document. Force the id back to the one `anchor` actually names.
        local_id = rendered.headings[0].anchor
        if local_id != target.anchor:
            html = html.replace(f'id="{local_id}"', f'id="{target.anchor}"', 1)
    return SectionView(
        slug=page["slug"],
        anchor=target.anchor,
        level=target.level,
        title=target.text,
        content=section_content,
        html=html,
        redirected_from=redirected_from,
    )


def list_pages(conn: sqlite3.Connection, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
    total = conn.execute("SELECT COUNT(*) AS n FROM page").fetchone()["n"]
    rows = conn.execute(
        """
        SELECT p.slug, p.title, p.created_at, p.updated_at,
               r.number AS revision_number, r.byte_size, r.author
        FROM page p
        LEFT JOIN revision r ON r.id = p.latest_rev_id
        ORDER BY p.updated_at DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return rows, total


def list_revisions(
    conn: sqlite3.Connection, slug: str, limit: int, offset: int
) -> tuple[list[sqlite3.Row], int, str]:
    page, _ = _resolve_slug(conn, slug)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM revision WHERE page_id = ?", (page["id"],)
    ).fetchone()["n"]
    rows = conn.execute(
        """
        SELECT id, number, title, comment, author, byte_size, parent_id, created_at
        FROM revision WHERE page_id = ?
        ORDER BY number DESC LIMIT ? OFFSET ?
        """,
        (page["id"], limit, offset),
    ).fetchall()
    return rows, total, page["slug"]


def _names_of(conn: sqlite3.Connection, page: sqlite3.Row) -> list[str]:
    """Every slug that reaches this page: its own, plus all its old names."""
    aliases = conn.execute(
        "SELECT from_slug FROM redirect WHERE to_page_id = ?", (page["id"],)
    ).fetchall()
    return [page["slug"], *(row["from_slug"] for row in aliases)]


def backlinks(conn: sqlite3.Connection, slug: str) -> tuple[list[sqlite3.Row], str]:
    """Pages linking here, including links written against an old name."""
    page, _ = _resolve_slug(conn, slug)
    names = _names_of(conn, page)
    placeholders = ",".join("?" * len(names))
    rows = conn.execute(
        f"""
        SELECT DISTINCT p.slug, p.title, p.updated_at
        FROM pagelink l JOIN page p ON p.id = l.from_page_id
        WHERE l.to_slug IN ({placeholders}) ORDER BY p.title
        """,
        names,
    ).fetchall()
    return rows, page["slug"]


# --------------------------------------------------------------------------
# citations
# --------------------------------------------------------------------------


# A citation row carries either a registry key or an inline definition, so
# every read resolves it the same way: prefer the registered source, fall back
# to what the body said. Repeated rather than hidden in a view, because SQLite
# views cannot be parameterised and this has to sit in several queries.
_RESOLVED_CITATION = """
    CASE WHEN c.source_key = '' THEN 'inline' ELSE COALESCE(s.kind, 'missing') END AS kind,
    COALESCE(NULLIF(s.title, ''), c.title) AS title,
    COALESCE(NULLIF(s.url, ''), c.url) AS url,
    COALESCE(NULLIF(s.host, ''), c.host) AS host,
    -- Bibliographic detail exists only in the registry; an inline definition
    -- has nowhere to write it.
    COALESCE(s.author, '') AS author,
    COALESCE(s.published, '') AS published,
    COALESCE(s.locator, '') AS locator
"""

#: What counts as one source when citations are grouped: the registry entry if
#: there is one, the URL otherwise.
_CITATION_IDENTITY = (
    "CASE WHEN c.source_key = '' THEN 'url:' || c.url ELSE 'source:' || c.source_key END"
)


def citations_of(conn: sqlite3.Connection, slug: str) -> tuple[list[sqlite3.Row], str]:
    """Sources cited by the page reachable from `slug`, in reference order.

    Answered from the index, so listing them costs no body read and no render.
    """
    page, _ = _resolve_slug(conn, slug)
    rows = conn.execute(
        f"""
        SELECT c.ordinal, c.name, c.source_key, {_RESOLVED_CITATION}
        FROM citation c LEFT JOIN source s ON s.key = c.source_key
        WHERE c.from_page_id = ? ORDER BY c.ordinal
        """,
        (page["id"],),
    ).fetchall()
    return rows, page["slug"]


def _citing_pages(
    conn: sqlite3.Connection, identities: Sequence[str]
) -> dict[str, list[sqlite3.Row]]:
    """For each source, the pages citing it. One query instead of one per source."""
    grouped: dict[str, list[sqlite3.Row]] = {identity: [] for identity in identities}
    for chunk, placeholders in _chunks(identities):
        rows = conn.execute(
            f"""
            SELECT DISTINCT {_CITATION_IDENTITY} AS identity, p.slug, p.title
            FROM citation c JOIN page p ON p.id = c.from_page_id
            WHERE {_CITATION_IDENTITY} IN ({placeholders}) ORDER BY p.title
            """,
            chunk,
        )
        for row in rows:
            grouped[row["identity"]].append(row)
    return grouped


def list_citations(
    conn: sqlite3.Connection,
    *,
    host: str | None,
    url: str | None,
    key: str | None,
    query: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    """Everything the wiki cites, grouped by source.

    This is the reverse index the `citation` table exists for. `key` or `url`
    narrows it to one source and answers "which pages cite this?", while `host`
    gathers a whole domain, which is where a link-rot sweep starts. Registered
    sources and inline definitions appear side by side because the question
    being asked — what is this wiki leaning on? — does not care which is which.
    """
    conditions: list[str] = []
    params: list[str] = []
    if host:
        conditions.append("COALESCE(NULLIF(s.host, ''), c.host) = ?")
        params.append(host.strip().lower())
    if url:
        conditions.append("COALESCE(NULLIF(s.url, ''), c.url) = ?")
        params.append(url)
    if key:
        conditions.append("c.source_key = ?")
        params.append(key)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            r"(COALESCE(NULLIF(s.url, ''), c.url) LIKE ? ESCAPE '\' "
            r"OR COALESCE(NULLIF(s.title, ''), c.title) LIKE ? ESCAPE '\' "
            r"OR c.source_key LIKE ? ESCAPE '\')"
        )
        params += [f"%{escaped}%"] * 3
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    joined = f"FROM citation c LEFT JOIN source s ON s.key = c.source_key {where}"

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM "
        f"(SELECT {_CITATION_IDENTITY} AS identity {joined} GROUP BY identity)",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT {_CITATION_IDENTITY} AS identity, c.source_key,
               MAX(CASE WHEN c.source_key = '' THEN 'inline'
                        ELSE COALESCE(s.kind, 'missing') END) AS kind,
               -- a non-empty title beats a blank one
               MAX(COALESCE(NULLIF(s.title, ''), c.title)) AS title,
               MAX(COALESCE(NULLIF(s.url, ''), c.url)) AS url,
               MAX(COALESCE(NULLIF(s.host, ''), c.host)) AS host,
               COUNT(DISTINCT c.from_page_id) AS page_count
        {joined}
        GROUP BY identity
        ORDER BY page_count DESC, identity
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    grouped = _citing_pages(conn, [row["identity"] for row in rows]) if rows else {}
    items = [
        {
            "source_key": row["source_key"],
            "kind": row["kind"],
            "url": row["url"],
            "host": row["host"],
            "title": row["title"],
            "page_count": row["page_count"],
            "pages": [
                {"slug": page["slug"], "title": page["title"]}
                for page in grouped.get(row["identity"], [])
            ],
        }
        for row in rows
    ]
    return items, total


# --------------------------------------------------------------------------
# the source registry
# --------------------------------------------------------------------------

#: A source is one of three things, and the kind decides which fields it needs.
KIND_LINK = "link"
KIND_TEXT = "text"
KIND_FILE = "file"
SOURCE_KINDS = (KIND_LINK, KIND_TEXT, KIND_FILE)


class FileInfo(NamedTuple):
    filename: str
    media_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class SourceView:
    """A registered source, with whatever it carries loaded."""

    key: str
    kind: str
    title: str
    url: str
    host: str
    author: str
    published: str
    locator: str
    note: str
    created_at: str
    updated_at: str
    #: The quoted passage, for kind='text'.
    text: str | None = None
    #: Set for kind='file', once bytes have been attached.
    file: FileInfo | None = None
    #: Pages citing this source right now.
    page_count: int = 0


def _source_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM source WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise SourceNotFound(key)
    return row


def _source_url(url: str) -> str:
    """Validate an optional URL on a source. Same rule the body's citations use."""
    url = url.strip()
    if url and not markup.external_url(url):
        raise InvalidSource("A source url must be http or https.", url=url)
    return url


def _citing_page_count(conn: sqlite3.Connection, key: str) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT from_page_id) AS n FROM citation WHERE source_key = ?",
        (key,),
    ).fetchone()["n"]


def _file_info(conn: sqlite3.Connection, file_id: int | None) -> FileInfo | None:
    if file_id is None:
        return None
    row = conn.execute(
        "SELECT filename, media_type, byte_size, sha256 FROM file WHERE id = ?", (file_id,)
    ).fetchone()
    return None if row is None else FileInfo(**dict(row))


def _source_view(conn: sqlite3.Connection, row: sqlite3.Row) -> SourceView:
    return SourceView(
        key=row["key"],
        kind=row["kind"],
        title=row["title"],
        url=row["url"],
        host=row["host"],
        author=row["author"],
        published=row["published"],
        locator=row["locator"],
        note=row["note"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        text=None if row["text_id"] is None else load_text(conn, row["text_id"]),
        file=_file_info(conn, row["file_id"]),
        page_count=_citing_page_count(conn, row["key"]),
    )


def get_source(conn: sqlite3.Connection, key: str) -> SourceView:
    return _source_view(conn, _source_row(conn, key))


def create_source(
    conn: sqlite3.Connection,
    *,
    key: str,
    kind: str,
    title: str,
    url: str,
    text: str,
    author: str,
    published: str,
    locator: str,
    note: str,
    settings: Settings,
) -> SourceView:
    """Register a source. The kind decides what it must carry."""
    if kind not in SOURCE_KINDS:
        raise InvalidSource(
            f"Unknown source kind {kind!r}; expected one of {', '.join(SOURCE_KINDS)}.",
            kind=kind,
        )
    url = _source_url(url)
    if kind == KIND_LINK and not url:
        raise InvalidSource("A link source needs a url.", kind=kind)
    if kind == KIND_TEXT and not text.strip():
        raise InvalidSource("A text source needs its text.", kind=kind)
    if kind != KIND_TEXT and text:
        raise InvalidSource(f"Only a text source carries text; this is a {kind} source.", kind=kind)
    _check_size(text, settings)

    now = _now()
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM source WHERE key = ?", (key,)).fetchone():
            raise SourceConflict(key)
        # A file source starts empty; the bytes arrive in a second request.
        text_id = (
            store_text(conn, text, settings.compress_min_bytes)[0]
            if kind == KIND_TEXT
            else None
        )
        conn.execute(
            """
            INSERT INTO source
                (key, kind, title, url, host, text_id, author, published, locator, note,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                kind,
                title,
                url,
                markup.url_host(url),
                text_id,
                author,
                published,
                locator,
                note,
                now,
                now,
            ),
        )
    return get_source(conn, key)


def update_source(
    conn: sqlite3.Connection,
    *,
    key: str,
    title: str | None,
    url: str | None,
    text: str | None,
    author: str | None,
    published: str | None,
    locator: str | None,
    note: str | None,
    settings: Settings,
) -> SourceView:
    """Correct a source in place. Fields left out keep their value.

    Every page citing it shows the correction on the next read — that is the
    reason the registry exists. The `kind` and the `key` are not editable: the
    kind is what the source *is*, and the key is the name bodies wrote down.
    """
    now = _now()
    with write_tx(conn):
        row = _source_row(conn, key)
        if text is not None and row["kind"] != KIND_TEXT:
            raise InvalidSource(
                f"Only a text source carries text; {key!r} is a {row['kind']} source.",
                key=key,
                kind=row["kind"],
            )

        updates: dict[str, object] = {}
        if title is not None:
            updates["title"] = title
        if url is not None:
            checked = _source_url(url)
            if row["kind"] == KIND_LINK and not checked:
                raise InvalidSource("A link source needs a url.", key=key)
            updates["url"] = checked
            updates["host"] = markup.url_host(checked)
        for column, value in (
            ("author", author),
            ("published", published),
            ("locator", locator),
            ("note", note),
        ):
            if value is not None:
                updates[column] = value

        old_text_id = row["text_id"]
        if text is not None:
            if not text.strip():
                raise InvalidSource("A text source needs its text.", key=key)
            _check_size(text, settings)
            updates["text_id"] = store_text(conn, text, settings.compress_min_bytes)[0]

        if updates:
            updates["updated_at"] = now
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"UPDATE source SET {assignments} WHERE id = ?",
                (*updates.values(), row["id"]),
            )
        if text is not None and old_text_id is not None:
            # Nothing else can point at a source's body, so it goes with it.
            delete_texts(conn, [old_text_id])
    return get_source(conn, key)


def delete_source(conn: sqlite3.Connection, key: str) -> int:
    """Unregister a source. Returns how many pages are left citing nothing.

    Citations are not rewritten — the bodies still say ``[^@key]``, and they
    now render as missing. Deleting a page a wikilink points at does the same
    thing, and for the same reason: the body owns its own text.
    """
    with write_tx(conn):
        row = _source_row(conn, key)
        dangling = _citing_page_count(conn, key)
        conn.execute("DELETE FROM source WHERE id = ?", (row["id"],))
        if row["text_id"] is not None:
            delete_texts(conn, [row["text_id"]])
        if row["file_id"] is not None:
            _drop_orphan_file(conn, row["file_id"])
    return dangling


def list_sources(
    conn: sqlite3.Connection,
    *,
    kind: str | None,
    host: str | None,
    query: str | None,
    limit: int,
    offset: int,
) -> tuple[list[sqlite3.Row], int]:
    conditions: list[str] = []
    params: list[str] = []
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if host:
        conditions.append("host = ?")
        params.append(host.strip().lower())
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(
            r"(key LIKE ? ESCAPE '\' OR title LIKE ? ESCAPE '\' "
            r"OR author LIKE ? ESCAPE '\' OR url LIKE ? ESCAPE '\')"
        )
        params += [f"%{escaped}%"] * 4
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = conn.execute(f"SELECT COUNT(*) AS n FROM source {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT s.key, s.kind, s.title, s.url, s.host, s.author, s.published,
               s.locator, s.note, s.created_at, s.updated_at,
               (SELECT COUNT(DISTINCT from_page_id) FROM citation
                 WHERE source_key = s.key) AS page_count
        FROM source s {where}
        ORDER BY s.updated_at DESC, s.key
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return rows, total


def source_citations(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """Pages citing this source. The reverse index, one source at a time."""
    _source_row(conn, key)
    return conn.execute(
        """
        SELECT DISTINCT p.slug, p.title, p.updated_at
        FROM citation c JOIN page p ON p.id = c.from_page_id
        WHERE c.source_key = ? ORDER BY p.title
        """,
        (key,),
    ).fetchall()


# --------------------------------------------------------------------------
# source files
# --------------------------------------------------------------------------


def _drop_orphan_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Delete a file row no source points at any more.

    File rows are content-addressed and therefore shared, so this has to ask
    rather than assume.
    """
    if conn.execute("SELECT 1 FROM source WHERE file_id = ?", (file_id,)).fetchone() is None:
        conn.execute("DELETE FROM file WHERE id = ?", (file_id,))


def attach_file(
    conn: sqlite3.Connection,
    *,
    key: str,
    data: bytes,
    filename: str,
    media_type: str,
    settings: Settings,
) -> SourceView:
    """Store the bytes of a file source, replacing anything already there."""
    if len(data) > settings.max_file_bytes:
        raise FileTooLarge(len(data), settings.max_file_bytes)
    if not data:
        raise InvalidSource("An upload cannot be empty.", key=key)

    digest = hashlib.sha256(data).hexdigest()
    now = _now()
    with write_tx(conn):
        row = _source_row(conn, key)
        if row["kind"] != KIND_FILE:
            raise InvalidSource(
                f"Only a file source takes an upload; {key!r} is a {row['kind']} source.",
                key=key,
                kind=row["kind"],
            )
        existing = conn.execute("SELECT id FROM file WHERE sha256 = ?", (digest,)).fetchone()
        if existing is None:
            file_id = int(
                conn.execute(
                    """
                    INSERT INTO file
                        (sha256, media_type, filename, byte_size, data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (digest, media_type, filename, len(data), data, now),
                ).lastrowid
            )
        else:
            # Same bytes already here; the first upload's name and type stand.
            file_id = int(existing["id"])

        previous = row["file_id"]
        conn.execute(
            "UPDATE source SET file_id = ?, updated_at = ? WHERE id = ?",
            (file_id, now, row["id"]),
        )
        if previous is not None and previous != file_id:
            _drop_orphan_file(conn, previous)
    return get_source(conn, key)


def load_file(conn: sqlite3.Connection, key: str) -> tuple[bytes, FileInfo]:
    """The bytes of a file source, for handing back over HTTP."""
    row = _source_row(conn, key)
    if row["file_id"] is None:
        raise FileNotAttached(key)
    stored = conn.execute(
        "SELECT data, filename, media_type, byte_size, sha256 FROM file WHERE id = ?",
        (row["file_id"],),
    ).fetchone()
    if stored is None:  # pragma: no cover - only reachable if the DB is corrupt
        raise FileNotAttached(key)
    return bytes(stored["data"]), FileInfo(
        filename=stored["filename"],
        media_type=stored["media_type"],
        byte_size=stored["byte_size"],
        sha256=stored["sha256"],
    )


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def _index_page(conn: sqlite3.Connection, page_id: int, slug: str, title: str, body: str) -> None:
    # FTS5 has no UPSERT, so replace the row outright.
    conn.execute("DELETE FROM page_fts WHERE rowid = ?", (page_id,))
    conn.execute(
        "INSERT INTO page_fts (rowid, slug, title, body) VALUES (?, ?, ?, ?)",
        (page_id, slug, title, body),
    )


def _record_links(conn: sqlite3.Connection, page_id: int, content: str) -> None:
    conn.execute("DELETE FROM pagelink WHERE from_page_id = ?", (page_id,))
    targets = markup.extract_links(content)
    if targets:
        conn.executemany(
            "INSERT INTO pagelink (from_page_id, to_slug) VALUES (?, ?)",
            [(page_id, target) for target in targets],
        )


def _record_citations(conn: sqlite3.Connection, page_id: int, content: str) -> None:
    """Replace this page's cited sources. Mirrors _record_links exactly.

    A registry reference stores its key and nothing else. Copying the source's
    title in here would be a second copy to keep correct, and the whole point
    of the registry is that there is one.
    """
    conn.execute("DELETE FROM citation WHERE from_page_id = ?", (page_id,))
    citations = markup.extract_citations(content)
    if citations:
        conn.executemany(
            """
            INSERT INTO citation
                (from_page_id, name, ordinal, source_key, url, title, host)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    page_id,
                    cite.name,
                    cite.ordinal,
                    cite.source_key,
                    cite.url,
                    cite.title,
                    cite.host,
                )
                for cite in citations
            ],
        )


def _insert_revision_row(
    conn: sqlite3.Connection,
    *,
    page_id: int,
    number: int,
    parent_id: int | None,
    text_id: int,
    byte_size: int,
    title: str,
    author: str,
    comment: str,
    now: str,
) -> int:
    """Append a revision pointing at an already-stored body.

    A rename reuses its predecessor's text_id: bodies are immutable, so two
    revisions sharing one is safe and saves storing the same text twice.
    """
    cursor = conn.execute(
        """
        INSERT INTO revision
            (page_id, number, text_id, title, comment, author, byte_size, parent_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (page_id, number, text_id, title, comment, author, byte_size, parent_id, now),
    )
    return int(cursor.lastrowid)


def _insert_revision(
    conn: sqlite3.Connection,
    *,
    page_id: int,
    number: int,
    parent_id: int | None,
    title: str,
    content: str,
    author: str,
    comment: str,
    now: str,
    settings: Settings,
) -> int:
    """Store a new body and append a revision pointing at it."""
    text_id, byte_size = store_text(conn, content, settings.compress_min_bytes)
    return _insert_revision_row(
        conn,
        page_id=page_id,
        number=number,
        parent_id=parent_id,
        text_id=text_id,
        byte_size=byte_size,
        title=title,
        author=author,
        comment=comment,
        now=now,
    )


def _check_size(content: str, settings: Settings) -> None:
    size = len(content.encode("utf-8"))
    if size > settings.max_content_bytes:
        raise ContentTooLarge(size, settings.max_content_bytes)


def create_page(
    conn: sqlite3.Connection,
    *,
    slug: str,
    title: str,
    content: str,
    author: str,
    comment: str,
    settings: Settings,
) -> PageView:
    _check_size(content, settings)
    now = _now()
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM page WHERE slug = ?", (slug,)).fetchone():
            raise SlugConflict(slug)
        if conn.execute("SELECT 1 FROM redirect WHERE from_slug = ?", (slug,)).fetchone():
            # Taking the name silently would break the trail left by a rename.
            raise RedirectConflict(
                slug, "it is an old name of another page; remove the redirect first"
            )
        page_id = int(
            conn.execute(
                "INSERT INTO page (slug, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (slug, title, now, now),
            ).lastrowid
        )
        rev_id = _insert_revision(
            conn,
            page_id=page_id,
            number=1,
            parent_id=None,
            title=title,
            content=content,
            author=author,
            comment=comment or "Created page",
            now=now,
            settings=settings,
        )
        conn.execute("UPDATE page SET latest_rev_id = ? WHERE id = ?", (rev_id, page_id))
        _index_page(conn, page_id, slug, title, content)
        _record_links(conn, page_id, content)
        _record_citations(conn, page_id, content)
    return get_page(conn, slug, settings)


def update_page(
    conn: sqlite3.Connection,
    *,
    slug: str,
    title: str | None,
    content: str,
    author: str,
    comment: str,
    base_revision: int | None,
    settings: Settings,
) -> tuple[PageView, bool]:
    """Save a new revision. Returns ``(view, changed)``.

    An edit that changes neither title nor body is a no-op — MediaWiki calls
    this a null edit — and does not grow the history.
    """
    _check_size(content, settings)
    now = _now()
    with write_tx(conn):
        # Editing through an old name edits the page it points at.
        page, _ = _resolve_slug(conn, slug)
        slug = page["slug"]
        latest = _latest_revision(conn, page)
        if base_revision is not None and base_revision != latest["number"]:
            raise EditConflict(slug, base_revision, latest["number"])

        new_title = page["title"] if title is None else title
        if content == load_text(conn, latest["text_id"]) and new_title == page["title"]:
            changed = False
        else:
            rev_id = _insert_revision(
                conn,
                page_id=page["id"],
                number=latest["number"] + 1,
                parent_id=latest["id"],
                title=new_title,
                content=content,
                author=author,
                comment=comment,
                now=now,
                settings=settings,
            )
            conn.execute(
                "UPDATE page SET title = ?, latest_rev_id = ?, updated_at = ? WHERE id = ?",
                (new_title, rev_id, now, page["id"]),
            )
            _index_page(conn, page["id"], slug, new_title, content)
            _record_links(conn, page["id"], content)
            _record_citations(conn, page["id"], content)
            _drop_stale_cache(conn, page["id"], rev_id)
            changed = True
    return get_page(conn, slug, settings), changed


def revert_page(
    conn: sqlite3.Connection,
    *,
    slug: str,
    number: int,
    author: str,
    comment: str,
    settings: Settings,
) -> tuple[PageView, bool]:
    """Re-save an old revision as a new one. History is never rewritten."""
    with write_tx(conn):
        page, _ = _resolve_slug(conn, slug)
        slug = page["slug"]
        target = _revision_row(conn, page["id"], number, slug)
        content = load_text(conn, target["text_id"])
        title = target["title"]
    return update_page(
        conn,
        slug=slug,
        title=title,
        content=content,
        author=author,
        comment=comment or f"Reverted to revision {number}",
        base_revision=None,
        settings=settings,
    )


def delete_page(conn: sqlite3.Connection, slug: str) -> str:
    """Delete whatever lives at exactly `slug`. Returns what that was.

    Deliberately does not follow old names: ``DELETE /pages/<old-name>`` drops
    the alias, never the page it points at. Destroying content through a stale
    name would be the worse surprise of the two.
    """
    with write_tx(conn):
        alias = conn.execute("SELECT 1 FROM redirect WHERE from_slug = ?", (slug,)).fetchone()
        if alias is not None:
            conn.execute("DELETE FROM redirect WHERE from_slug = ?", (slug,))
            return "redirect"

        page = _page_row(conn, slug)
        text_ids = [
            row["text_id"]
            for row in conn.execute(
                "SELECT text_id FROM revision WHERE page_id = ?", (page["id"],)
            )
        ]
        # Clear the pointer first so the page -> revision FK cannot trip.
        conn.execute("UPDATE page SET latest_rev_id = NULL WHERE id = ?", (page["id"],))
        conn.execute("DELETE FROM page WHERE id = ?", (page["id"],))
        conn.execute("DELETE FROM page_fts WHERE rowid = ?", (page["id"],))
        # Old names go with it, via the redirect -> page foreign key cascade.
        delete_texts(conn, text_ids)
    return "page"


# --------------------------------------------------------------------------
# rename and redirects
# --------------------------------------------------------------------------


def rename_page(
    conn: sqlite3.Connection,
    *,
    slug: str,
    new_slug: str | None,
    new_title: str | None,
    author: str,
    comment: str,
    leave_redirect: bool,
    settings: Settings,
) -> tuple[PageView, bool]:
    """Move a page to a new slug and/or title. Returns ``(view, changed)``.

    The move is recorded as a revision so history shows it, but that revision
    reuses its predecessor's text_id rather than storing the body again.
    """
    now = _now()
    with write_tx(conn):
        page, _ = _resolve_slug(conn, slug)
        old_slug = page["slug"]
        target_slug = old_slug if new_slug is None else new_slug
        target_title = page["title"] if new_title is None else new_title

        if target_slug == old_slug and target_title == page["title"]:
            changed = False
        else:
            if target_slug != old_slug:
                if conn.execute(
                    "SELECT 1 FROM page WHERE slug = ? AND id <> ?", (target_slug, page["id"])
                ).fetchone():
                    raise SlugConflict(target_slug)
                alias = conn.execute(
                    "SELECT to_page_id FROM redirect WHERE from_slug = ?", (target_slug,)
                ).fetchone()
                if alias is not None and alias["to_page_id"] != page["id"]:
                    raise RedirectConflict(
                        target_slug, "it is an old name of a different page"
                    )
                # Moving back onto one of this page's own old names: the real
                # page reclaims the slug and the now-pointless alias goes.
                conn.execute("DELETE FROM redirect WHERE from_slug = ?", (target_slug,))

            latest = _latest_revision(conn, page)
            if not comment:
                comment = (
                    f"Renamed {old_slug} → {target_slug}"
                    if target_slug != old_slug
                    else f"Retitled to {target_title}"
                )
            rev_id = _insert_revision_row(
                conn,
                page_id=page["id"],
                number=latest["number"] + 1,
                parent_id=latest["id"],
                text_id=latest["text_id"],
                byte_size=latest["byte_size"],
                title=target_title,
                author=author,
                comment=comment,
                now=now,
            )
            conn.execute(
                "UPDATE page SET slug = ?, title = ?, latest_rev_id = ?, updated_at = ? "
                "WHERE id = ?",
                (target_slug, target_title, rev_id, now, page["id"]),
            )
            if target_slug != old_slug and leave_redirect:
                conn.execute(
                    """
                    INSERT INTO redirect (from_slug, to_page_id, created_at, created_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (from_slug) DO UPDATE SET
                        to_page_id = excluded.to_page_id,
                        created_at = excluded.created_at,
                        created_by = excluded.created_by
                    """,
                    (old_slug, page["id"], now, author),
                )
            _index_page(
                conn,
                page["id"],
                target_slug,
                target_title,
                load_text(conn, latest["text_id"]),
            )
            _drop_stale_cache(conn, page["id"], rev_id)
            changed = True

    return get_page(conn, target_slug, settings), changed


def add_redirect(conn: sqlite3.Connection, *, slug: str, alias: str, author: str) -> str:
    """Point `alias` at the page reachable from `slug`. Returns the target slug."""
    now = _now()
    with write_tx(conn):
        page, _ = _resolve_slug(conn, slug)
        if alias == page["slug"]:
            raise RedirectConflict(alias, "it is the page's own slug")
        if conn.execute("SELECT 1 FROM page WHERE slug = ?", (alias,)).fetchone():
            raise SlugConflict(alias)
        existing = conn.execute(
            "SELECT to_page_id FROM redirect WHERE from_slug = ?", (alias,)
        ).fetchone()
        if existing is not None and existing["to_page_id"] != page["id"]:
            raise RedirectConflict(alias, "it is already an old name of a different page")
        conn.execute(
            """
            INSERT INTO redirect (from_slug, to_page_id, created_at, created_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (from_slug) DO NOTHING
            """,
            (alias, page["id"], now, author),
        )
        return page["slug"]


def remove_redirect(conn: sqlite3.Connection, alias: str) -> None:
    with write_tx(conn):
        if not conn.execute("SELECT 1 FROM redirect WHERE from_slug = ?", (alias,)).fetchone():
            raise RedirectNotFound(alias)
        conn.execute("DELETE FROM redirect WHERE from_slug = ?", (alias,))


def list_redirects(
    conn: sqlite3.Connection, limit: int, offset: int
) -> tuple[list[sqlite3.Row], int]:
    total = conn.execute("SELECT COUNT(*) AS n FROM redirect").fetchone()["n"]
    rows = conn.execute(
        """
        SELECT r.from_slug, p.slug AS to_slug, r.created_at, r.created_by
        FROM redirect r JOIN page p ON p.id = r.to_page_id
        ORDER BY r.created_at DESC, r.from_slug
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return rows, total


def redirects_to(conn: sqlite3.Connection, slug: str) -> tuple[list[sqlite3.Row], str]:
    """Every old name of the page reachable from `slug`."""
    page, _ = _resolve_slug(conn, slug)
    rows = conn.execute(
        """
        SELECT from_slug, ? AS to_slug, created_at, created_by
        FROM redirect WHERE to_page_id = ? ORDER BY created_at DESC, from_slug
        """,
        (page["slug"], page["id"]),
    ).fetchall()
    return rows, page["slug"]


# --------------------------------------------------------------------------
# compaction
# --------------------------------------------------------------------------


def _page_text_ids(conn: sqlite3.Connection, page_id: int) -> list[int]:
    """Distinct body rows of one page, oldest revision first.

    Distinct because a rename points its revision at the previous body rather
    than storing it again.
    """
    seen: list[int] = []
    for row in conn.execute(
        "SELECT text_id FROM revision WHERE page_id = ? ORDER BY number", (page_id,)
    ):
        if row["text_id"] not in seen:
            seen.append(row["text_id"])
    return seen


def _deltify(conn: sqlite3.Connection, text_ids: list[int]) -> int:
    """Re-encode older bodies as deltas against their window's keyframe."""
    converted = 0
    for position, text_id in enumerate(text_ids):
        is_keyframe = position % KEYFRAME_INTERVAL == 0
        is_newest = position == len(text_ids) - 1
        if is_keyframe or is_newest or is_delta(conn, text_id) or is_packed(conn, text_id):
            continue

        base_id = text_ids[(position // KEYFRAME_INTERVAL) * KEYFRAME_INTERVAL]
        body = load_text(conn, text_id)
        payload = make_delta(load_text(conn, base_id), body)
        if encoded_delta_size(payload) >= stored_size(conn, text_id):
            # Rewriting would cost more than it saves; leave it whole.
            continue

        replace_with_delta(conn, text_id, base_id, payload)
        if load_text(conn, text_id) != body:  # pragma: no cover - guards a codec bug
            raise DeltaError(f"delta for text row {text_id} did not round-trip")
        converted += 1
    return converted


def _bundle(conn: sqlite3.Connection, text_ids: list[int], now: str) -> int:
    """Pack each window's payloads into one shared gzip stream.

    A window holds a keyframe and the deltas taken against it, so compressing
    them together lets gzip carry its dictionary across the whole group instead
    of restarting — and pays one gzip header instead of sixteen.

    The newest body is left out so reading the current page never unpacks a
    bundle.
    """
    packable = text_ids[:-1]
    bundled = 0
    for start in range(0, len(packable), KEYFRAME_INTERVAL):
        window = packable[start : start + KEYFRAME_INTERVAL]
        if len(window) < 2:
            continue
        expected = {text_id: load_text(conn, text_id) for text_id in window}
        if pack_into_blob(conn, window, now) <= 0:
            continue
        for text_id, body in expected.items():
            if load_text(conn, text_id) != body:  # pragma: no cover - guards a packing bug
                raise DeltaError(f"text row {text_id} did not survive bundling")
        bundled += 1
    return bundled


def compact_page(conn: sqlite3.Connection, page_id: int) -> dict[str, int]:
    """Shrink a page's stored history.

    Two independent passes. Deltas remove the redundancy *between* revisions;
    bundling removes the overhead of compressing each one separately. Either
    helps on its own, and MediaWiki's concatenated blobs show the second is
    worth doing even with no deltas at all.
    """
    text_ids = _page_text_ids(conn, page_id)
    now = _now()
    with write_tx(conn):
        converted = _deltify(conn, text_ids)
        bundled = _bundle(conn, text_ids, now)
    return {"converted": converted, "bundled": bundled}


def compact_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Run compaction across every page."""
    page_ids = [row["id"] for row in conn.execute("SELECT id FROM page ORDER BY id")]
    before = storage_bytes(conn)
    totals = {"pages": len(page_ids), "converted": 0, "bundled": 0}
    for page_id in page_ids:
        result = compact_page(conn, page_id)
        for key in ("converted", "bundled"):
            totals[key] += result[key]
    totals["before"] = before
    totals["after"] = storage_bytes(conn)
    totals["reclaimed"] = totals["before"] - totals["after"]
    return totals


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def diff_revisions(
    conn: sqlite3.Connection, slug: str, from_number: int, to_number: int
) -> dict[str, object]:
    page, _ = _resolve_slug(conn, slug)
    slug = page["slug"]
    left = _revision_row(conn, page["id"], from_number, slug)
    right = _revision_row(conn, page["id"], to_number, slug)

    left_lines = load_text(conn, left["text_id"]).splitlines(keepends=True)
    right_lines = load_text(conn, right["text_id"]).splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile=f"{slug}@{from_number}",
            tofile=f"{slug}@{to_number}",
            lineterm="\n",
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {
        "slug": slug,
        "from_revision": from_number,
        "to_revision": to_number,
        "diff": "".join(lines),
        "added_lines": added,
        "removed_lines": removed,
        "byte_delta": right["byte_size"] - left["byte_size"],
    }


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def _excerpt(body: str, needle: str, width: int = 160) -> str:
    """Plain-text excerpt with the match highlighted, for the LIKE fallback."""
    position = body.lower().find(needle.lower())
    if position == -1:
        return body[:width]
    start = max(0, position - width // 3)
    chunk = body[start : start + width]
    local = chunk.lower().find(needle.lower())
    if local == -1:
        return chunk
    highlighted = (
        chunk[:local] + _HL_OPEN + chunk[local : local + len(needle)] + _HL_CLOSE
        + chunk[local + len(needle) :]
    )
    return ("…" if start else "") + highlighted + ("…" if start + width < len(body) else "")


def _like_search(
    conn: sqlite3.Connection, query: str, limit: int, offset: int
) -> list[dict[str, object]]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = conn.execute(
        r"""
        SELECT p.slug, p.title, p.updated_at, f.body
        FROM page_fts f JOIN page p ON p.id = f.rowid
        WHERE f.title LIKE ? ESCAPE '\' OR f.body LIKE ? ESCAPE '\'
        ORDER BY p.updated_at DESC LIMIT ? OFFSET ?
        """,
        (pattern, pattern, limit, offset),
    ).fetchall()
    return [
        {
            "slug": row["slug"],
            "title": row["title"],
            "updated_at": row["updated_at"],
            "snippet": _excerpt(row["body"], query),
            "score": None,
        }
        for row in rows
    ]


def search(
    conn: sqlite3.Connection, query: str, limit: int, offset: int
) -> list[dict[str, object]]:
    """Full-text search.

    Tries FTS5 first, falling back to a LIKE scan for queries too short for the
    trigram index or that the index simply misses.
    """
    query = query.strip()
    if not query:
        return []

    hits: list[dict[str, object]] = []
    if len(query) >= 3:
        # Quote the whole query so FTS5 operators in user input stay literal.
        match = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                """
                SELECT p.slug, p.title, p.updated_at,
                       snippet(page_fts, 2, ?, ?, '…', 16) AS snippet,
                       bm25(page_fts, 0.0, 5.0, 1.0) AS score
                FROM page_fts JOIN page p ON p.id = page_fts.rowid
                WHERE page_fts MATCH ?
                ORDER BY score LIMIT ? OFFSET ?
                """,
                (_HL_OPEN, _HL_CLOSE, match, limit, offset),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        hits = [dict(row) for row in rows]

    # Only fall back on the first page, so paging past the end of a real FTS
    # result set does not suddenly start showing LIKE matches.
    if hits or offset:
        return hits
    return _like_search(conn, query, limit, offset)


def highlight_to_html(snippet: str) -> str:
    """Escape a snippet, then turn the internal markers into <mark> tags."""
    return html.escape(snippet).replace(_HL_OPEN, "<mark>").replace(_HL_CLOSE, "</mark>")


# --------------------------------------------------------------------------
# semantic search (see omnilog/embeddings and omnilog/vectorstore)
# --------------------------------------------------------------------------

#: Characters sent to the embedding provider per page. A generous safety rail
#: well under typical provider context limits, not real chunking -- splitting
#: a page into several embedded sections is future work, not done here (the
#: page_embedding schema already has room for it via its `anchor` column).
_MAX_EMBEDDING_CHARS = 24000


def _embedding_stale(row: sqlite3.Row | None, rev_id: int, provider: EmbeddingProvider) -> bool:
    """A row is stale once its page has moved on, or the model/provider has.

    Mirrors why render_cache checks link_state/cite_state on every read: the
    stored answer must be told apart from an answer to a different question,
    not just from "no answer yet".
    """
    return (
        row is None
        or row["rev_id"] != rev_id
        or row["model_id"] != provider.model_id
        or row["dimensions"] != provider.dimensions
    )


def index_page_embedding(
    conn: sqlite3.Connection, page_id: int, provider: EmbeddingProvider
) -> bool:
    """(Re)compute a page's embedding if it is missing or stale.

    Called from reindex_embeddings, never from create_page/update_page: an
    embedding call is outbound HTTP to whatever the provider is, and this
    project keeps that kind of dependency out of the write path on purpose
    (the same reasoning as citation link-rot checks -- see the README).
    """
    page = conn.execute(
        "SELECT id, latest_rev_id FROM page WHERE id = ?", (page_id,)
    ).fetchone()
    if page is None or page["latest_rev_id"] is None:
        return False
    rev_id = page["latest_rev_id"]

    existing = conn.execute(
        "SELECT rev_id, model_id, dimensions FROM page_embedding "
        "WHERE page_id = ? AND anchor = ''",
        (page_id,),
    ).fetchone()
    if not _embedding_stale(existing, rev_id, provider):
        return False

    revision = conn.execute(
        "SELECT text_id FROM revision WHERE id = ?", (rev_id,)
    ).fetchone()
    chunk = load_text(conn, revision["text_id"])[:_MAX_EMBEDDING_CHARS]
    try:
        (vector,) = provider.embed_documents([chunk])
    except EmbeddingUnavailable:
        raise
    except Exception as exc:  # provider SDKs each raise their own types
        raise EmbeddingUnavailable(str(exc)) from exc

    conn.execute(
        """
        INSERT INTO page_embedding
            (page_id, anchor, rev_id, chunk_text, embedding, model_id, dimensions, created_at)
        VALUES (?, '', ?, ?, ?, ?, ?, ?)
        ON CONFLICT (page_id, anchor) DO UPDATE SET
            rev_id = excluded.rev_id,
            chunk_text = excluded.chunk_text,
            embedding = excluded.embedding,
            model_id = excluded.model_id,
            dimensions = excluded.dimensions,
            created_at = excluded.created_at
        """,
        (
            page_id,
            rev_id,
            chunk,
            vectorstore.pack(vector),
            provider.model_id,
            provider.dimensions,
            _now(),
        ),
    )
    return True


def reindex_embeddings(
    conn: sqlite3.Connection, provider: EmbeddingProvider, *, limit: int | None = None
) -> dict[str, object]:
    """(Re)compute every page's embedding that is missing or stale.

    Each page commits in its own transaction, so a provider failure partway
    through a large wiki leaves the pages already indexed in place rather
    than rolling everything back. Safe to re-run any time -- rows that are
    already current under the configured model are left untouched. Mirrors
    compact_all in spirit: explicit, offline upkeep, not part of the edit path.
    """
    page_ids = [row["id"] for row in conn.execute("SELECT id FROM page ORDER BY id")]
    if limit is not None:
        page_ids = page_ids[:limit]
    updated = 0
    for page_id in page_ids:
        with write_tx(conn):
            if index_page_embedding(conn, page_id, provider):
                updated += 1
    return {
        "pages": len(page_ids),
        "updated": updated,
        "model_id": provider.model_id,
        "dimensions": provider.dimensions,
    }


def semantic_search(
    conn: sqlite3.Connection, query: str, provider: EmbeddingProvider, limit: int
) -> list[tuple[sqlite3.Row, float]]:
    """Rank pages by cosine similarity to `query`.

    Only rows stamped with the *current* provider's model_id/dimensions are
    considered -- comparing vectors from two different models would produce
    numbers that look like scores but mean nothing next to each other.
    """
    query = query.strip()
    if not query:
        return []
    try:
        vector = provider.embed_query(query)
    except EmbeddingUnavailable:
        raise
    except Exception as exc:
        raise EmbeddingUnavailable(str(exc)) from exc

    rows = conn.execute(
        """
        SELECT pe.page_id, pe.anchor, pe.chunk_text, pe.embedding,
               p.slug, p.title, p.updated_at
        FROM page_embedding pe JOIN page p ON p.id = pe.page_id
        WHERE pe.model_id = ? AND pe.dimensions = ?
        """,
        (provider.model_id, provider.dimensions),
    ).fetchall()
    return vectorstore.rank_by_similarity(vector, rows, limit=limit)
