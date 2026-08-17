"""Markdown rendering with ``[[wikilink]]`` and ``[^source]`` support.

Raw HTML in source is disabled, so bodies cannot smuggle script tags through
the renderer. Wikilink targets are slugified the same way page titles are, so
``[[홈 페이지]]`` and a page titled "홈 페이지" meet at the slug ``홈-페이지``.

Citations are the outward-facing counterpart of wikilinks: ``[^name]`` marks a
sentence and ``[^name]: <url> "title"`` says where it came from. A wikilink
resolves against the wiki, a citation against the open web — which is why the
two are indexed separately.

``[^@key]`` cites a source from the registry instead, and needs no definition:
the title, URL and everything else live in one place outside the wiki. Like a
wikilink, the body holds only the name and resolution happens at render time,
so a key naming nothing yet is a citation that renders as missing rather than
an error.
"""

from __future__ import annotations

import html
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import quote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.token import Token

from .slug import slugify

#: Longest ``[[...]]`` body we bother parsing.
_MAX_TARGET_LENGTH = 300
_MAX_CITE_NAME_LENGTH = 100
_MAX_CITE_TITLE_LENGTH = 300
_MAX_CITE_URL_LENGTH = 2000

_WIKILINK_TOKEN = "wikilink"
_CITATION_TOKEN = "citation"
_CITATION_DEF_RULE = "citation_definition"
_local = threading.local()

#: What a wikilink target turned out to be. A slug missing from the mapping
#: does not exist at all and renders as a red link.
STATUS_PAGE = "page"
STATUS_REDIRECT = "redirect"

#: Heading of the source list appended to a rendered body.
CITATIONS_HEADING = "출처"

#: The only schemes allowed to become an href. A ``javascript:`` "source" is
#: not something a reader can go check, and is the XSS vector besides.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: ``[^name]: https://example.com "Optional title"``
_CITE_DEFINITION = re.compile(r'^\[\^([^\]\s]+)\]:[ \t]*(\S+)(?:[ \t]+"([^"]*)")?[ \t]*$')

#: What a citation turned out to be.
KIND_INLINE = "inline"
#: A registry reference nobody has resolved yet — what extract_citations sees.
KIND_SOURCE = "source"
#: A registry reference whose key names no source. The red link of citations.
KIND_MISSING = "missing"

#: Given link targets, says which ones resolve and how.
Resolver = Callable[[Sequence[str]], Mapping[str, str]]


class SourceRef(NamedTuple):
    """A registered source, as the renderer needs to see it."""

    key: str
    #: 'link' | 'text' | 'file'.
    kind: str
    title: str
    #: Empty for a source that is not on the web at all.
    url: str
    host: str
    #: Changes whenever the source does. The render cache watches this.
    fingerprint: str
    author: str = ""
    #: Free-form date: "2004", "2024-03-11", "n.d.".
    published: str = ""
    #: Where in the source: page number, chapter, timestamp.
    locator: str = ""


#: Given source keys, says which ones are registered and what they hold.
SourceResolver = Callable[[Sequence[str]], Mapping[str, SourceRef]]


class Citation(NamedTuple):
    """One source, as the page's reference list shows it."""

    #: 1-based, ordered by where the body first refers to the source.
    ordinal: int
    #: The ``[^name]`` as written — ``@key`` for a registry reference. Citing
    #: the same name twice reuses one entry.
    name: str
    #: Registry key, or '' when the body defines the source inline.
    source_key: str
    #: KIND_INLINE, the registered source's kind, KIND_MISSING, or KIND_SOURCE
    #: when the reference has not been resolved.
    kind: str
    #: Display text; empty means the URL or the key stands in for it.
    title: str
    url: str
    #: Hostname, kept alongside so sources can be listed per domain.
    host: str
    #: Bibliographic detail. Only a registered source carries it — the inline
    #: ``[^name]: <url> "title"`` form has nowhere to put it, which is one of
    #: the reasons the registry exists. Defaulted so a citation cached before
    #: these existed still loads.
    author: str = ""
    published: str = ""
    locator: str = ""


@dataclass(frozen=True)
class Rendered:
    """Result of rendering one body."""

    html: str
    #: Wikilink targets in first-appearance order, deduplicated.
    slugs: tuple[str, ...]
    #: slug -> STATUS_PAGE | STATUS_REDIRECT, for the targets that resolved.
    status: Mapping[str, str]
    #: Sources actually referred to, numbered.
    citations: tuple[Citation, ...] = ()
    #: Registry key -> fingerprint for every key the body cited, '' when the key
    #: resolved to nothing. What the render cache has to watch.
    sources: Mapping[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# wikilinks
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# citations
# --------------------------------------------------------------------------


def url_host(url: str) -> str:
    """Hostname of `url`, lowercased. Empty when it has none."""
    return (urlsplit(url).hostname or "").lower()


def external_url(url: str) -> str:
    """Return `url` if it may become an href, else the empty string."""
    if len(url) > _MAX_CITE_URL_LENGTH:
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.hostname:
        return ""
    return url


def _citation_definition_rule(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    """Block rule for ``[^name]: <url> "title"`` lines.

    Registered ahead of CommonMark's own ``reference`` rule, which would
    otherwise claim the line as a link reference definition. A definition that
    cannot be used — unknown scheme, no host — is declined here and falls
    through to the ordinary rules, so it shows up as text instead of vanishing.
    """
    if state.is_code_block(start_line):
        return False
    begin = state.bMarks[start_line] + state.tShift[start_line]
    match = _CITE_DEFINITION.match(state.src[begin : state.eMarks[start_line]])
    if match is None:
        return False

    name, url, title = match.group(1), match.group(2), match.group(3) or ""
    if len(name) > _MAX_CITE_NAME_LENGTH or not external_url(url):
        return False
    if silent:
        return True

    definitions: dict[str, tuple[str, str]] = state.env.setdefault("citations", {})
    # First definition wins, as with CommonMark link references.
    definitions.setdefault(name, (url, title[:_MAX_CITE_TITLE_LENGTH]))
    state.line = start_line + 1
    return True


def _citation_rule(state, silent: bool) -> bool:
    """Inline rule for ``[^name]`` and ``[^@key]``.

    ``[^name]`` needs a definition in the body; an undefined name is left alone,
    so ``[^2]`` in ordinary prose stays ordinary prose rather than becoming a
    footnote marker pointing nowhere.

    ``[^@key]`` needs nothing, the way ``[[page]]`` needs nothing: the ``@``
    says the author meant a citation, and whether the registry has that key is
    a question for render time.
    """
    src, start = state.src, state.pos
    if not src.startswith("[^", start):
        return False
    end = src.find("]", start + 2)
    if end == -1 or end + 1 > state.posMax:
        return False

    name = src[start + 2 : end]
    if not name or len(name) > _MAX_CITE_NAME_LENGTH:
        return False
    if "[" in name or "\n" in name:
        return False

    if name.startswith("@"):
        # Slugified like a page title, so [^@Manual Text] and a source keyed
        # 'manual-text' meet the same way [[Home Page]] and 'home-page' do.
        # Spaces are fine here: the @ already said this is a citation.
        key = slugify(name[1:], strict=False)
        if not key:
            return False
        meta = {"name": f"@{key}", "source_key": key}
    else:
        if any(ch.isspace() for ch in name):
            return False
        definitions = state.env.get("citations", {}) if isinstance(state.env, dict) else {}
        if name not in definitions:
            return False
        meta = {"name": name, "source_key": ""}

    if not silent:
        token = state.push(_CITATION_TOKEN, "", 0)
        token.meta = meta
    state.pos = end + 1
    return True


def _render_citation(tokens: Sequence[Token], idx: int, options, env) -> str:
    meta = tokens[idx].meta
    wiki = env.get("wiki", {}) if isinstance(env, dict) else {}
    ordinal = wiki.get("cites", {}).get(meta["name"])
    if ordinal is None:  # pragma: no cover - numbering covers every token
        return ""
    # The occurrence index keeps ids unique when one source is cited twice,
    # and gives the reference list something to point back at.
    return (
        f'<sup class="cite-ref" id="cite-ref-{ordinal}-{meta["index"]}">'
        f'<a href="#cite-{ordinal}">[{ordinal}]</a></sup>'
    )


def _collect_citations(
    tokens: Iterable[Token], definitions: Mapping[str, tuple[str, str]]
) -> tuple[tuple[Citation, ...], dict[str, int]]:
    """Number the sources a body refers to. Returns ``(citations, uses)``.

    Numbering follows first reference, not definition order, so the marks read
    1, 2, 3 down the page. Each marker token is stamped with which occurrence
    of its source it is. Definitions nothing refers to are dropped: an unused
    source is not a citation, the same way an unused link is not a pagelink.

    Registry references come back unresolved, as KIND_SOURCE with empty fields.
    Only `render` knows how to look a key up.
    """
    order: list[tuple[str, str]] = []
    uses: dict[str, int] = {}

    def walk(items: Iterable[Token]) -> None:
        for token in items:
            if token.type == _CITATION_TOKEN:
                name = token.meta["name"]
                if name not in uses:
                    order.append((name, token.meta["source_key"]))
                    uses[name] = 0
                token.meta["index"] = uses[name]
                uses[name] += 1
            if token.children:
                walk(token.children)

    walk(tokens)
    citations = []
    for position, (name, source_key) in enumerate(order, start=1):
        if source_key:
            citations.append(
                Citation(
                    ordinal=position,
                    name=name,
                    source_key=source_key,
                    kind=KIND_SOURCE,
                    title="",
                    url="",
                    host="",
                )
            )
        else:
            url, title = definitions[name]
            citations.append(
                Citation(
                    ordinal=position,
                    name=name,
                    source_key="",
                    kind=KIND_INLINE,
                    title=title,
                    url=url,
                    host=url_host(url),
                )
            )
    return tuple(citations), uses


def _resolve_citations(
    citations: Sequence[Citation], sources: Mapping[str, SourceRef]
) -> tuple[Citation, ...]:
    """Fill registry references in from the registry."""
    resolved = []
    for cite in citations:
        if not cite.source_key:
            resolved.append(cite)
            continue
        source = sources.get(cite.source_key)
        if source is None:
            resolved.append(cite._replace(kind=KIND_MISSING))
        else:
            resolved.append(
                cite._replace(
                    kind=source.kind,
                    title=source.title,
                    url=source.url,
                    host=source.host,
                    author=source.author,
                    published=source.published,
                    locator=source.locator,
                )
            )
    return tuple(resolved)


def _back_label(index: int) -> str:
    """Label for one of several back-references: a, b, c, … then numbers."""
    return chr(ord("a") + index) if index < 26 else str(index + 1)


def _back_links(cite: Citation, count: int) -> str:
    if count == 1:
        return f'<a class="cite-back" href="#cite-ref-{cite.ordinal}-0">↑</a>'
    links = " ".join(
        f'<a class="cite-back" href="#cite-ref-{cite.ordinal}-{index}">{_back_label(index)}</a>'
        for index in range(count)
    )
    return f'<span class="cite-back">↑</span> {links}'


def _citation_meta_html(cite: Citation) -> str:
    """Author, date and locator — what makes a footnote a citation.

    Each field gets its own span so a client can style or reorder them; the
    commas are only there for readers of the raw HTML.
    """
    present = [
        (field, value)
        for field, value in (
            ("author", cite.author),
            ("published", cite.published),
            ("locator", cite.locator),
        )
        if value
    ]
    if not present:
        return ""
    inner = ", ".join(
        f'<span class="cite-{field}">{html.escape(value)}</span>' for field, value in present
    )
    return f' <span class="cite-meta">{inner}</span>'


def _citation_body_html(cite: Citation, source_base: str) -> str:
    """The part of a reference-list entry that says what the source is."""
    key = html.escape(cite.source_key)
    if cite.kind == KIND_MISSING:
        # The red link of citations: the body cites a key nobody registered.
        return (
            f'<span class="cite-source cite-missing" data-source="{key}"'
            f' data-exists="false">@{key}</span>'
        )

    registry = ""
    if cite.source_key:
        href = f"{source_base}/{quote(cite.source_key, safe='')}"
        registry = (
            f' <a class="cite-key" href="{html.escape(href)}" data-source="{key}"'
            f' data-exists="true">@{key}</a>'
        )

    label = cite.title or cite.url or f"@{cite.source_key}"
    if cite.url:
        # Off-site, so it gets the treatment untrusted outbound links get.
        anchor = (
            f'<a class="cite-source" href="{html.escape(cite.url)}"'
            f' rel="nofollow noopener" target="_blank">{html.escape(label)}</a>'
        )
    else:
        # A quoted passage or a file: the registry entry is the destination.
        href = f"{source_base}/{quote(cite.source_key, safe='')}"
        anchor = (
            f'<a class="cite-source" href="{html.escape(href)}">{html.escape(label)}</a>'
        )
    # The host is only worth showing when the label is not already the URL.
    host = (
        f' <span class="cite-host">{html.escape(cite.host)}</span>'
        if cite.title and cite.host
        else ""
    )
    return anchor + _citation_meta_html(cite) + host + registry


def _citation_list_html(
    citations: Sequence[Citation], uses: Mapping[str, int], source_base: str
) -> str:
    """The reference list appended below the body."""
    if not citations:
        return ""
    rows = [
        f'<li id="cite-{cite.ordinal}" data-kind="{html.escape(cite.kind)}">'
        f"{_back_links(cite, uses[cite.name])} "
        f"{_citation_body_html(cite, source_base)}</li>"
        for cite in citations
    ]
    return (
        f'<section class="citations">\n'
        f'<h2 class="citations-heading">{html.escape(CITATIONS_HEADING)}</h2>\n'
        f"<ol>\n" + "\n".join(rows) + "\n</ol>\n</section>\n"
    )


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def _markdown() -> MarkdownIt:
    """One parser per thread; handlers run in FastAPI's threadpool."""
    md = getattr(_local, "md", None)
    if md is None:
        md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
        md.enable(["table", "strikethrough"])
        # 'alt' lets a definition line end the paragraph above it, so sources
        # may sit directly under the sentence they back.
        md.block.ruler.before(
            "reference",
            _CITATION_DEF_RULE,
            _citation_definition_rule,
            {"alt": ["paragraph", "reference", "blockquote"]},
        )
        md.inline.ruler.before("link", _WIKILINK_TOKEN, _wikilink_rule)
        md.inline.ruler.before("link", _CITATION_TOKEN, _citation_rule)
        md.renderer.rules[_WIKILINK_TOKEN] = _render_wikilink
        md.renderer.rules[_CITATION_TOKEN] = _render_citation
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


def extract_citations(content: str) -> tuple[Citation, ...]:
    """Sources cited by `content`, numbered, without rendering it.

    Registry references come back unresolved — this is what the index stores,
    and the index stores the key, not a copy of the source.
    """
    env: dict[str, object] = {}
    tokens = _markdown().parse(content, env)
    citations, _ = _collect_citations(tokens, env.get("citations", {}))
    return citations


def render(
    content: str,
    resolve: Resolver,
    base: str,
    *,
    resolve_sources: SourceResolver | None = None,
    source_base: str = "/sources",
) -> Rendered:
    """Render `content` to HTML.

    Parsing happens first so `resolve` gets the full target list in one call,
    rather than one lookup per link. `resolve_sources` gets the same treatment
    for ``[^@key]`` references.
    """
    md = _markdown()
    env: dict[str, object] = {}
    tokens = md.parse(content, env)

    found: list[str] = []
    _collect_slugs(tokens, found, set())
    status = dict(resolve(found)) if found else {}
    citations, uses = _collect_citations(tokens, env.get("citations", {}))

    keys = [cite.source_key for cite in citations if cite.source_key]
    sources = dict(resolve_sources(keys)) if keys and resolve_sources else {}
    citations = _resolve_citations(citations, sources)

    env["wiki"] = {
        "status": status,
        "base": base,
        "cites": {cite.name: cite.ordinal for cite in citations},
    }
    return Rendered(
        html=md.renderer.render(tokens, md.options, env)
        + _citation_list_html(citations, uses, source_base),
        slugs=tuple(found),
        status=status,
        citations=citations,
        # Missing keys are recorded as '' so registering one later is a change
        # the render cache notices.
        sources={key: sources[key].fingerprint if key in sources else "" for key in keys},
    )
