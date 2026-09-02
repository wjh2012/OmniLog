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

-- Bytes of an uploaded source file. Keyed by content hash, so the same file
-- attached to two sources is stored once.
CREATE TABLE IF NOT EXISTS file (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    filename   TEXT NOT NULL,
    byte_size  INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);

-- Sources managed apart from any page: a link, a passage of text, or a file.
-- `id` is the identity and `key` the address, the same split page makes. Pages
-- cite a key, so editing a source's title fixes it everywhere at once.
--
-- Deliberately not versioned. A page is a work whose history matters; a source
-- is a record of what something points at, and only the current answer is
-- interesting. Quoted passages still land in the immutable text store.
CREATE TABLE IF NOT EXISTS source (
    id         INTEGER PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,            -- 'link' | 'text' | 'file'
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    host       TEXT NOT NULL DEFAULT '', -- denormalised from url, for per-domain lookups
    text_id    INTEGER REFERENCES text(id) ON DELETE SET NULL,  -- kind='text'
    file_id    INTEGER REFERENCES file(id) ON DELETE SET NULL,  -- kind='file'
    author     TEXT NOT NULL DEFAULT '',
    published  TEXT NOT NULL DEFAULT '',  -- free-form; sources date themselves however they like
    locator    TEXT NOT NULL DEFAULT '',  -- page number, chapter, timestamp
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_kind ON source (kind);
CREATE INDEX IF NOT EXISTS idx_source_host ON source (host);

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

-- Sources cited by each page's current revision: the outward-facing twin of
-- pagelink. Same lifetime, replaced wholesale on every edit. Only definitions
-- the body actually refers to get a row.
--
-- A row is one of two things. `[^name]: <url>` defines a source inline and
-- fills url/title/host. `[^@key]` points at the registry and fills source_key
-- alone -- storing a copy of the source's fields here would go stale the
-- moment someone corrected the source. Like pagelink.to_slug, the key may name
-- a source that does not exist, which is how a dangling citation is found.
CREATE TABLE IF NOT EXISTS citation (
    from_page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,      -- the [^name] as written, unique within a page
    ordinal      INTEGER NOT NULL,   -- 1-based, by first reference in the body
    source_key   TEXT NOT NULL DEFAULT '',  -- '' for an inline definition
    url          TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    host         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (from_page_id, name)
) WITHOUT ROWID;

-- Reverse index: which pages cite this source, URL, or host.
CREATE INDEX IF NOT EXISTS idx_citation_source ON citation (source_key);
CREATE INDEX IF NOT EXISTS idx_citation_url ON citation (url);
CREATE INDEX IF NOT EXISTS idx_citation_host ON citation (host);

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
-- cite_state does the same for registered sources, which live outside the body
-- and can be corrected or deleted under a page's feet. Inline definitions need
-- neither: they resolve out of the body alone.
--
-- Only a page's current revision is kept. Caching every revision ever viewed
-- made this table grow without bound and dominate the database file.
CREATE TABLE IF NOT EXISTS render_cache (
    rev_id     INTEGER PRIMARY KEY REFERENCES revision(id) ON DELETE CASCADE,
    html_gz    BLOB NOT NULL,
    links_json TEXT NOT NULL,
    link_state TEXT NOT NULL,
    cites_json TEXT NOT NULL,   -- citations of this revision, as rendered
    cite_state TEXT NOT NULL,   -- fingerprint of the registered sources they resolved to
    created_at TEXT NOT NULL
);

-- Vector index for semantic search (see omnilog/vectorstore.py). One row per
-- (page, anchor); anchor is '' for the whole page today -- section-level
-- chunks can land here later without a schema change. model_id/dimensions are
-- stamped on every row for the same reason link_state/cite_state exist on
-- render_cache: a provider or model swap must be told apart from a merely
-- stale row, never silently mixed into the same search. Keyed by page_id, not
-- rev_id, so a rename (which never touches page_id) leaves embeddings valid;
-- only an edit (a new rev_id) makes a row stale.
CREATE TABLE IF NOT EXISTS page_embedding (
    page_id    INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    anchor     TEXT NOT NULL DEFAULT '',
    rev_id     INTEGER NOT NULL REFERENCES revision(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding  BLOB NOT NULL,    -- little-endian float32, `dimensions` long
    model_id   TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (page_id, anchor)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_page_embedding_model ON page_embedding (model_id);
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

    # citation rows are rebuilt from the body on the next edit, so an added
    # column only has to be valid for the rows already there -- and '' is
    # exactly what an inline definition should have.
    citation_columns = _columns(conn, "citation")
    if citation_columns and "source_key" not in citation_columns:
        conn.execute("ALTER TABLE citation ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")

    # render_cache holds nothing that cannot be recomputed, so a shape change is
    # a drop rather than a data migration.
    cache_columns = _columns(conn, "render_cache")
    if cache_columns and not {"html_gz", "cites_json", "cite_state"} <= cache_columns:
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
