"""SQLite connection handling and schema.

The layout follows MediaWiki's split: ``revision`` carries metadata only and
points at an immutable ``text`` row holding the body. Keeping bodies out of the
revision table is what makes history cheap to list and lets bodies be
compressed (or, later, moved to external storage) without touching metadata.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import Settings

SCHEMA = """
-- Body store. Logically immutable: the string a row resolves to never changes.
-- Physically it can be re-encoded by compaction, which may rewrite `data` as a
-- delta against `base_id` -- the same trade `git repack` makes.
CREATE TABLE IF NOT EXISTS text (
    id      INTEGER PRIMARY KEY,
    flags   TEXT NOT NULL,   -- 'utf-8' plus optional ',gzip' ',delta' ',blob'
    data    BLOB NOT NULL,   -- the payload, or empty when it lives in a blob
    base_id INTEGER REFERENCES text(id) ON DELETE SET NULL,  -- delta rows only
    blob_id     INTEGER REFERENCES blob(id) ON DELETE SET NULL,
    blob_offset INTEGER,
    blob_length INTEGER
);

-- One gzip stream shared by a whole window of revisions. Compressing them
-- together lets the window see across revisions instead of paying a fresh
-- gzip header and an empty dictionary for each one.
CREATE TABLE IF NOT EXISTS blob (
    id         INTEGER PRIMARY KEY,
    data       BLOB NOT NULL,
    items      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page (
    id            INTEGER PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    latest_rev_id INTEGER REFERENCES revision(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Metadata only; the body lives in text.id = revision.text_id.
CREATE TABLE IF NOT EXISTS revision (
    id         INTEGER PRIMARY KEY,
    page_id    INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    number     INTEGER NOT NULL,   -- 1-based, per page
    text_id    INTEGER NOT NULL REFERENCES text(id),
    title      TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    author     TEXT NOT NULL DEFAULT 'anonymous',
    byte_size  INTEGER NOT NULL,
    parent_id  INTEGER REFERENCES revision(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE (page_id, number)
);

CREATE INDEX IF NOT EXISTS idx_revision_page ON revision (page_id, number DESC);

-- Outgoing [[wikilinks]] of each page's current revision. to_slug may point at
-- a page that does not exist yet, which is how red links are found.
CREATE TABLE IF NOT EXISTS pagelink (
    from_page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    to_slug      TEXT NOT NULL,
    PRIMARY KEY (from_page_id, to_slug)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_pagelink_to ON pagelink (to_slug);

-- Slugs a page used to live at. Targets are page ids, not slugs, so renaming
-- A -> B -> C leaves every old name pointing straight at the page: redirect
-- chains and loops cannot form. A from_slug must never also be a page.slug;
-- repository.py upholds that, SQLite cannot express it.
CREATE TABLE IF NOT EXISTS redirect (
    from_slug  TEXT PRIMARY KEY,
    to_page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'anonymous'
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_redirect_target ON redirect (to_page_id);

-- Rendered HTML, gzipped. Safe to key on rev_id alone because bodies are
-- immutable; link_state additionally tracks how link targets resolved at
-- render time, so red links turning blue still invalidates the entry.
--
-- Only a page's current revision is kept. Caching every revision ever viewed
-- made this table grow without bound and dominate the database file.
CREATE TABLE IF NOT EXISTS render_cache (
    rev_id     INTEGER PRIMARY KEY REFERENCES revision(id) ON DELETE CASCADE,
    html_gz    BLOB NOT NULL,
    links_json TEXT NOT NULL,
    link_state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Created separately so the tokenizer can be probed first.
_FTS_TABLE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS page_fts "
    "USING fts5(slug UNINDEXED, title, body, tokenize='{tokenizer}')"
)


def connect(settings: Settings) -> sqlite3.Connection:
    """Open a connection. Cheap enough to do per request under SQLite."""
    conn = sqlite3.connect(settings.db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction.

    BEGIN IMMEDIATE takes the write lock up front so two concurrent editors
    cannot both read-then-upgrade and deadlock.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _pick_tokenizer(conn: sqlite3.Connection) -> str:
    """Prefer trigram: it gives substring matching, which Korean needs.

    unicode61 only matches whole space-delimited tokens, so a search for
    '위키' would miss '위키시스템'.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts_probe USING fts5(x, tokenize='trigram')")
    except sqlite3.OperationalError:
        return "unicode61"
    conn.execute("DROP TABLE temp.fts_probe")
    return "trigram"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    Small enough to do inline; there is no migration framework here on purpose.
    """
    text_columns = _columns(conn, "text")
    if text_columns and "base_id" not in text_columns:
        conn.execute(
            "ALTER TABLE text ADD COLUMN base_id INTEGER "
            "REFERENCES text(id) ON DELETE SET NULL"
        )
    if text_columns and "blob_id" not in text_columns:
        conn.execute(
            "ALTER TABLE text ADD COLUMN blob_id INTEGER "
            "REFERENCES blob(id) ON DELETE SET NULL"
        )
        conn.execute("ALTER TABLE text ADD COLUMN blob_offset INTEGER")
        conn.execute("ALTER TABLE text ADD COLUMN blob_length INTEGER")

    # render_cache holds nothing that cannot be recomputed, so a shape change is
    # a drop rather than a data migration.
    cache_columns = _columns(conn, "render_cache")
    if cache_columns and "html_gz" not in cache_columns:
        conn.execute("DROP TABLE render_cache")


def init_db(settings: Settings) -> None:
    """Create the schema if it is not there yet, and migrate an older one."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _migrate(conn)
        # executescript issues its own COMMIT, so it must not sit inside write_tx.
        conn.executescript(SCHEMA)
        conn.execute(_FTS_TABLE.format(tokenizer=_pick_tokenizer(conn)))
    finally:
        conn.close()
