"""Request and response bodies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

MAX_TITLE_LENGTH = 200
MAX_COMMENT_LENGTH = 500
MAX_AUTHOR_LENGTH = 100
MAX_URL_LENGTH = 2000
#: Publication date, page number and the like: short, free-form strings.
MAX_META_LENGTH = 200
MAX_NOTE_LENGTH = 2000
MAX_FILENAME_LENGTH = 255
DEFAULT_AUTHOR = "anonymous"

#: What a source is. A link points at the web, a text carries the passage
#: itself, a file carries bytes.
SourceKind = Literal["link", "text", "file"]


class PageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    content: str = ""
    #: Derived from the title when omitted.
    slug: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    author: str = Field(default=DEFAULT_AUTHOR, max_length=MAX_AUTHOR_LENGTH)
    comment: str = Field(default="", max_length=MAX_COMMENT_LENGTH)


class PageUpdate(BaseModel):
    content: str
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    author: str = Field(default=DEFAULT_AUTHOR, max_length=MAX_AUTHOR_LENGTH)
    comment: str = Field(default="", max_length=MAX_COMMENT_LENGTH)
    #: Revision the edit was based on. Set it to be told about lost updates.
    base_revision: int | None = Field(default=None, ge=1)


class RevertRequest(BaseModel):
    author: str = Field(default=DEFAULT_AUTHOR, max_length=MAX_AUTHOR_LENGTH)
    comment: str = Field(default="", max_length=MAX_COMMENT_LENGTH)


class RenameRequest(BaseModel):
    """Move a page. Supply a slug, a title, or both.

    Passing only a title retitles the page and leaves the URL alone; changing
    where the page lives is always explicit.
    """

    slug: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_LENGTH)
    #: Leave the old slug behind as an alias so existing links keep working.
    leave_redirect: bool = True
    author: str = Field(default=DEFAULT_AUTHOR, max_length=MAX_AUTHOR_LENGTH)
    comment: str = Field(default="", max_length=MAX_COMMENT_LENGTH)

    @model_validator(mode="after")
    def _needs_something_to_change(self) -> "RenameRequest":
        if self.slug is None and self.title is None:
            raise ValueError("supply at least one of 'slug' or 'title'")
        return self


class RedirectCreate(BaseModel):
    #: The alias to create; it will point at the page being addressed.
    slug: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    author: str = Field(default=DEFAULT_AUTHOR, max_length=MAX_AUTHOR_LENGTH)


class RedirectRef(BaseModel):
    from_slug: str
    to_slug: str
    created_at: str
    created_by: str


class RedirectList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[RedirectRef]


class PageRedirects(BaseModel):
    slug: str
    items: list[RedirectRef]


class RevisionMeta(BaseModel):
    id: int
    number: int
    title: str
    comment: str
    author: str
    byte_size: int
    parent_id: int | None
    created_at: str


class LinkRef(BaseModel):
    slug: str
    #: False for a red link — the target page has not been written yet.
    exists: bool
    #: True when the target is a page's old name rather than its current slug.
    via_redirect: bool


class CitationRef(BaseModel):
    """One source cited by a page, resolved."""

    #: 1-based, by first reference in the body. The number shown in the text.
    ordinal: int
    #: The ``[^name]`` as written — ``@key`` for a registered source.
    name: str
    #: Registry key, or empty when the body defines the source inline.
    source_key: str
    #: "inline" for a body definition, otherwise the registered source's kind,
    #: or "missing" when the key names no source.
    kind: str
    #: Display text, or empty when the URL stands in for it.
    title: str
    url: str
    host: str
    #: Bibliographic detail, from the registry. Empty for an inline definition.
    author: str = ""
    published: str = ""
    locator: str = ""


class PageCitations(BaseModel):
    slug: str
    items: list[CitationRef]


class CitationUse(BaseModel):
    slug: str
    title: str


class CitationEntry(BaseModel):
    """One source, with every page citing it."""

    source_key: str
    kind: str
    url: str
    host: str
    title: str
    page_count: int
    pages: list[CitationUse]


class CitationList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CitationEntry]


# --------------------------------------------------------------------------
# the source registry
# --------------------------------------------------------------------------


class SourceCreate(BaseModel):
    """Register a source. Which fields are required depends on the kind."""

    #: Derived from the title when omitted.
    key: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    kind: SourceKind = "link"
    title: str = Field(default="", max_length=MAX_TITLE_LENGTH)
    #: Required for kind="link"; optional elsewhere (a book's page, say).
    url: str = Field(default="", max_length=MAX_URL_LENGTH)
    #: The passage itself. Required for kind="text".
    text: str = ""
    author: str = Field(default="", max_length=MAX_AUTHOR_LENGTH)
    #: Free-form: "2004", "2024-03-11", "n.d." — sources date themselves however.
    published: str = Field(default="", max_length=MAX_META_LENGTH)
    #: Where in the source: page number, chapter, timestamp.
    locator: str = Field(default="", max_length=MAX_META_LENGTH)
    note: str = Field(default="", max_length=MAX_NOTE_LENGTH)

    @model_validator(mode="after")
    def _needs_a_name(self) -> "SourceCreate":
        if not (self.key or self.title):
            raise ValueError("supply at least one of 'key' or 'title'")
        return self


class SourceUpdate(BaseModel):
    """Correct a source. Fields left out keep their value.

    Neither `kind` nor `key` is here: the kind is what the source is, and the
    key is the name page bodies wrote down.
    """

    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    text: str | None = None
    author: str | None = Field(default=None, max_length=MAX_AUTHOR_LENGTH)
    published: str | None = Field(default=None, max_length=MAX_META_LENGTH)
    locator: str | None = Field(default=None, max_length=MAX_META_LENGTH)
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


class FileRef(BaseModel):
    filename: str
    media_type: str
    byte_size: int
    #: Content hash. The same bytes are stored once, however many sources use them.
    sha256: str


class SourceDetail(BaseModel):
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
    #: The passage, for kind="text".
    text: str | None = None
    #: Set for kind="file", once bytes have been attached.
    file: FileRef | None = None
    #: Pages citing this source right now.
    page_count: int = 0


class SourceSummary(BaseModel):
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
    page_count: int


class SourceList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SourceSummary]


class SourceUse(BaseModel):
    slug: str
    title: str
    updated_at: str


class SourceCitations(BaseModel):
    """Pages citing one source: the reverse index, one source at a time."""

    key: str
    items: list[SourceUse]


class PageDetail(BaseModel):
    slug: str
    title: str
    content: str
    html: str
    created_at: str
    updated_at: str
    revision: RevisionMeta
    links: list[LinkRef]
    #: Sources cited by this revision, in the order the numbers appear.
    citations: list[CitationRef]
    #: Set when the page was reached through one of its old names.
    redirected_from: str | None = None


class PageSaved(PageDetail):
    #: False when the edit matched the current revision and was skipped.
    changed: bool


class PageSummary(BaseModel):
    slug: str
    title: str
    revision_number: int | None
    byte_size: int | None
    author: str | None
    created_at: str
    updated_at: str


class PageList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[PageSummary]


class RevisionList(BaseModel):
    slug: str
    total: int
    limit: int
    offset: int
    items: list[RevisionMeta]


class DiffResult(BaseModel):
    slug: str
    from_revision: int
    to_revision: int
    diff: str
    added_lines: int
    removed_lines: int
    byte_delta: int


class BacklinkRef(BaseModel):
    slug: str
    title: str
    updated_at: str


class BacklinkList(BaseModel):
    slug: str
    items: list[BacklinkRef]


class SearchHit(BaseModel):
    slug: str
    title: str
    updated_at: str
    #: HTML-escaped excerpt with matches wrapped in <mark>.
    snippet: str
    #: BM25 score (lower is better), or null when the LIKE fallback answered.
    score: float | None


class SearchResult(BaseModel):
    query: str
    limit: int
    offset: int
    items: list[SearchHit]


class CompactResult(BaseModel):
    pages: int
    #: Bodies re-encoded as deltas by this run.
    converted: int
    #: Windows packed into a shared gzip stream by this run.
    bundled: int
    bytes_before: int
    bytes_after: int
    bytes_reclaimed: int
    #: Change in the database file itself, after VACUUM if one was asked for.
    file_before: int
    file_after: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = {}
