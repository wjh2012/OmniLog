from __future__ import annotations

from omnilog.markup import extract_headings, extract_links, render


def _render(
    content: str, existing: set[str] | None = None, redirects: set[str] | None = None
):
    known = {slug: "page" for slug in existing or ()}
    known.update({slug: "redirect" for slug in redirects or ()})
    return render(content, lambda slugs: {s: known[s] for s in slugs if s in known}, "/pages")


def test_wikilink_to_existing_page_is_a_normal_link() -> None:
    result = _render("see [[Target Page]]", {"target-page"})
    assert 'class="wikilink"' in result.html
    assert 'data-exists="true"' in result.html
    assert result.slugs == ("target-page",)


def test_wikilink_to_missing_page_is_a_red_link() -> None:
    result = _render("see [[Nowhere]]")
    assert 'class="wikilink wikilink-new"' in result.html
    assert 'data-exists="false"' in result.html


def test_wikilink_to_an_old_name_still_counts_as_existing() -> None:
    result = _render("see [[Former Name]]", redirects={"former-name"})
    assert 'data-exists="true"' in result.html
    assert 'data-redirect="true"' in result.html
    assert "wikilink-redirect" in result.html


def test_live_page_link_carries_no_redirect_marker() -> None:
    assert "data-redirect" not in _render("[[Target]]", {"target"}).html


def test_wikilink_label_overrides_display_text() -> None:
    result = _render("[[Target Page|click here]]", {"target-page"})
    assert ">click here</a>" in result.html
    assert result.slugs == ("target-page",)


def test_korean_wikilink_href_is_percent_encoded() -> None:
    result = _render("[[홈 페이지]]")
    assert result.slugs == ("홈-페이지",)
    # 홈-페이지 in UTF-8 percent encoding.
    assert "/pages/%ED%99%88-%ED%8E%98%EC%9D%B4%EC%A7%80" in result.html


def test_raw_html_is_escaped() -> None:
    result = _render("<script>alert(1)</script>")
    assert "<script>" not in result.html
    assert "&lt;script&gt;" in result.html


def test_label_is_escaped() -> None:
    result = _render('[[Target|<img onerror="x">]]', {"target"})
    assert "<img" not in result.html


def test_links_are_deduplicated_case_insensitively() -> None:
    assert extract_links("[[Alpha]] [[alpha]] [[Beta]]") == ("alpha", "beta")


def test_malformed_wikilinks_are_left_alone() -> None:
    html = _render("[[]] and [[ ]] and [[a[b]]").html
    assert "wikilink" not in html


def test_tables_render() -> None:
    html = _render("| a | b |\n|---|---|\n| 1 | 2 |").html
    assert "<table>" in html


def test_wikilink_inside_emphasis() -> None:
    result = _render("*[[Target]]*", {"target"})
    assert "<em>" in result.html
    assert result.slugs == ("target",)


def test_extract_headings_in_document_order() -> None:
    headings = extract_headings("# Title\n\nintro\n\n## Section A\n\ntext\n\n### Sub A1\n")
    assert [(h.level, h.text, h.anchor) for h in headings] == [
        (1, "Title", "title"),
        (2, "Section A", "section-a"),
        (3, "Sub A1", "sub-a1"),
    ]


def test_duplicate_heading_text_gets_a_suffixed_anchor() -> None:
    headings = extract_headings("## Overview\n\ntext\n\n## Overview\n")
    assert [h.anchor for h in headings] == ["overview", "overview-2"]


def test_heading_with_nothing_sluggable_falls_back_to_position() -> None:
    headings = extract_headings("## !!!\n")
    assert headings[0].anchor == "section-1"


def test_heading_formatting_marks_are_stripped_from_the_anchor_text() -> None:
    headings = extract_headings("## Section **Bold** and `code` here\n")
    assert headings[0].text == "Section Bold and code here"


def test_render_stamps_heading_ids_matching_the_outline() -> None:
    result = _render("## Section A\n\ntext\n")
    assert result.headings == extract_headings("## Section A\n\ntext\n")
    assert '<h2 id="section-a">Section A</h2>' in result.html
