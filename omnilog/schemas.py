"""Request and response bodies."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MAX_TITLE_LENGTH = 200
MAX_COMMENT_LENGTH = 500
MAX_AUTHOR_LENGTH = 100
DEFAULT_AUTHOR = "anonymous"


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


class PageDetail(BaseModel):
    slug: str
    title: str
    content: str
    html: str
    created_at: str
    updated_at: str
    revision: RevisionMeta
    links: list[LinkRef]
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
