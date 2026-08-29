"""Domain errors, translated to HTTP status codes in omnilog.app."""

from __future__ import annotations


class WikiError(Exception):
    """Base class for everything the wiki raises on purpose."""

    status_code = 400
    code = "wiki_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class PageNotFound(WikiError):
    status_code = 404
    code = "page_not_found"

    def __init__(self, slug: str) -> None:
        super().__init__(f"No page with slug {slug!r}.", slug=slug)


class RevisionNotFound(WikiError):
    status_code = 404
    code = "revision_not_found"

    def __init__(self, slug: str, number: int) -> None:
        super().__init__(
            f"Page {slug!r} has no revision {number}.", slug=slug, number=number
        )


class SectionNotFound(WikiError):
    status_code = 404
    code = "section_not_found"

    def __init__(self, slug: str, anchor: str) -> None:
        super().__init__(
            f"Page {slug!r} has no section {anchor!r}.", slug=slug, anchor=anchor
        )


class SlugConflict(WikiError):
    status_code = 409
    code = "slug_conflict"

    def __init__(self, slug: str) -> None:
        super().__init__(f"A page with slug {slug!r} already exists.", slug=slug)


class RedirectNotFound(WikiError):
    status_code = 404
    code = "redirect_not_found"

    def __init__(self, slug: str) -> None:
        super().__init__(f"No redirect at {slug!r}.", slug=slug)


class RedirectConflict(WikiError):
    status_code = 409
    code = "redirect_conflict"

    def __init__(self, slug: str, reason: str) -> None:
        super().__init__(f"Cannot point {slug!r} anywhere: {reason}.", slug=slug, reason=reason)


class EditConflict(WikiError):
    """The client edited against a revision that is no longer the latest one."""

    status_code = 409
    code = "edit_conflict"

    def __init__(self, slug: str, base_revision: int, latest_revision: int) -> None:
        super().__init__(
            f"Page {slug!r} moved on to revision {latest_revision} "
            f"while you were editing revision {base_revision}.",
            slug=slug,
            base_revision=base_revision,
            latest_revision=latest_revision,
        )


class InvalidSlug(WikiError):
    status_code = 400
    code = "invalid_slug"

    def __init__(self, value: str) -> None:
        super().__init__(
            f"{value!r} does not contain any character usable in a slug.", value=value
        )


class ContentTooLarge(WikiError):
    status_code = 413
    code = "content_too_large"

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"Revision body is {size} bytes, over the {limit} byte limit.",
            size=size,
            limit=limit,
        )


class SourceNotFound(WikiError):
    status_code = 404
    code = "source_not_found"

    def __init__(self, key: str) -> None:
        super().__init__(f"No source registered as {key!r}.", key=key)


class SourceConflict(WikiError):
    status_code = 409
    code = "source_conflict"

    def __init__(self, key: str) -> None:
        super().__init__(f"A source is already registered as {key!r}.", key=key)


class InvalidSource(WikiError):
    """The fields do not add up to a usable source of the requested kind."""

    status_code = 400
    code = "invalid_source"

    def __init__(self, reason: str, **details: object) -> None:
        super().__init__(reason, **details)


class FileNotAttached(WikiError):
    status_code = 404
    code = "file_not_attached"

    def __init__(self, key: str) -> None:
        super().__init__(f"Source {key!r} has no file attached yet.", key=key)


class FileTooLarge(WikiError):
    status_code = 413
    code = "file_too_large"

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"Upload is {size} bytes, over the {limit} byte limit.", size=size, limit=limit
        )
