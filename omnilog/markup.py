"""Markdown rendering with ``[[wikilink]]`` support.

Raw HTML in source is disabled, so bodies cannot smuggle script tags through
the renderer. Wikilink targets are slugified the same way page titles are, so
``[[홈 페이지]]`` and a page titled "홈 페이지" meet at the slug ``홈-페이지``.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .slug import slugify

#: Longest ``[[...]]`` body we bother parsing.
_MAX_TARGET_LENGTH = 300

_WIKILINK_TOKEN = "wikilink"
_local = threading.local()

#: What a wikilink target turned out to be. A slug missing from the mapping
#: does not exist at all and renders as a red link.
STATUS_PAGE = "page"
STATUS_REDIRECT = "redirect"

#: Given link targets, says which ones resolve and how.
Resolver = Callable[[Sequence[str]], Mapping[str, str]]


@dataclass(frozen=True)
class Rendered:
    """Result of rendering one body."""

    html: str
    #: Wikilink targets in first-appearance order, deduplicated.
    slugs: tuple[str, ...]
    #: slug -> STATUS_PAGE | STATUS_REDIRECT, for the targets that resolved.
    status: Mapping[str, str]


def _wikilink_rule(state, silent: bool) -> bool:
    """Inline rule for ``[[Target]]`` and ``[[Target|label]]``."""
    src, start = state.src, state.pos
    if not src.startswith("[[", start):
        return False
    end = src.find("]]", start + 2)
    if end == -1 or end + 2 > state.posMax:
        return False

    body = src[start + 2 : end]
    if len(body) > _MAX_TARGET_LENGTH or any(ch in body for ch in "[]\n"):
        return False

    target, separator, label = body.partition("|")
    target = target.strip()
    if not target:
        return False
    slug = slugify(target, strict=False)
    if not slug:
        return False

    if not silent:
        token = state.push(_WIKILINK_TOKEN, "", 0)
        token.meta = {"slug": slug, "label": (label.strip() if separator else "") or target}
    state.pos = end + 2
    return True


def _render_wikilink(tokens: Sequence[Token], idx: int, options, env) -> str:
    meta = tokens[idx].meta
    wiki = env.get("wiki", {}) if isinstance(env, dict) else {}
    base = wiki.get("base", "/pages")
    status: Mapping[str, str] = wiki.get("status", {})

    slug = meta["slug"]
    resolved = status.get(slug)
    css = "wikilink" if resolved else "wikilink wikilink-new"
    if resolved == STATUS_REDIRECT:
        css += " wikilink-redirect"
    href = f"{base}/{quote(slug, safe='')}"
    redirect_attr = ' data-redirect="true"' if resolved == STATUS_REDIRECT else ""
    return (
        f'<a class="{css}" href="{html.escape(href)}"'
        f' data-slug="{html.escape(slug)}"'
        f' data-exists="{"true" if resolved else "false"}"{redirect_attr}>'
        f"{html.escape(meta['label'])}</a>"
    )


def _markdown() -> MarkdownIt:
    """One parser per thread; handlers run in FastAPI's threadpool."""
    md = getattr(_local, "md", None)
    if md is None:
        md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
        md.enable(["table", "strikethrough"])
        md.inline.ruler.before("link", _WIKILINK_TOKEN, _wikilink_rule)
        md.renderer.rules[_WIKILINK_TOKEN] = _render_wikilink
        _local.md = md
    return md


def _collect_slugs(tokens: Iterable[Token], found: list[str], seen: set[str]) -> None:
    for token in tokens:
        if token.type == _WIKILINK_TOKEN:
            slug = token.meta["slug"]
            if slug not in seen:
                seen.add(slug)
                found.append(slug)
        if token.children:
            _collect_slugs(token.children, found, seen)


def extract_links(content: str) -> tuple[str, ...]:
    """Wikilink targets in `content`, without rendering it."""
    found: list[str] = []
    _collect_slugs(_markdown().parse(content, {}), found, set())
    return tuple(found)


def render(content: str, resolve: Resolver, base: str) -> Rendered:
    """Render `content` to HTML.

    Parsing happens first so `resolve` gets the full target list in one call,
    rather than one lookup per link.
    """
    md = _markdown()
    env: dict[str, object] = {}
    tokens = md.parse(content, env)

    found: list[str] = []
    _collect_slugs(tokens, found, set())
    status = dict(resolve(found)) if found else {}

    env["wiki"] = {"status": status, "base": base}
    return Rendered(
        html=md.renderer.render(tokens, md.options, env),
        slugs=tuple(found),
        status=status,
    )
