from __future__ import annotations

from omnilog.markup import extract_citations, render

MW = "https://www.mediawiki.org/wiki/Manual:Text_table"
GIT = "https://git-scm.com/docs/git-gc"

BODY = f"""위키피디아는 델타 기반 시스템을 사용한다.[^mw]
압축은 별도 작업으로 돌린다.[^git]

[^mw]: {MW} "MediaWiki Manual: Text table"
[^git]: {GIT}
"""


def _render(content: str):
    return render(content, lambda slugs: {}, "/pages")


# --------------------------------------------------------------------------
# markup
# --------------------------------------------------------------------------


def test_reference_becomes_a_numbered_mark() -> None:
    html = _render(BODY).html
    assert '<sup class="cite-ref" id="cite-ref-1-0"><a href="#cite-1">[1]</a></sup>' in html
    assert '<sup class="cite-ref" id="cite-ref-2-0"><a href="#cite-2">[2]</a></sup>' in html


def test_definition_line_is_not_rendered_as_text() -> None:
    html = _render(BODY).html
    assert "[^mw]:" not in html


def test_source_list_is_appended() -> None:
    html = _render(BODY).html
    assert '<section class="citations">' in html
    assert (
        '<li id="cite-1" data-kind="inline">'
        '<a class="cite-back" href="#cite-ref-1-0">↑</a>' in html
    )
    assert f'href="{MW}"' in html
    assert ">MediaWiki Manual: Text table</a>" in html


def test_untitled_source_shows_its_url() -> None:
    assert f">{GIT}</a>" in _render(BODY).html


def test_numbering_follows_first_reference_not_definition_order() -> None:
    body = f"""두 번째 근거.[^b] 첫 번째 근거.[^a]

[^a]: {MW}
[^b]: {GIT}
"""
    assert [(c.ordinal, c.name) for c in extract_citations(body)] == [(1, "b"), (2, "a")]


def test_citing_one_source_twice_makes_one_entry_with_two_backlinks() -> None:
    body = f"""앞 문장.[^mw] 뒤 문장.[^mw]

[^mw]: {MW} "제목"
"""
    result = _render(body)
    assert len(result.citations) == 1
    assert 'id="cite-ref-1-0"' in result.html
    assert 'id="cite-ref-1-1"' in result.html
    assert '<a class="cite-back" href="#cite-ref-1-0">a</a>' in result.html
    assert '<a class="cite-back" href="#cite-ref-1-1">b</a>' in result.html


def test_undefined_reference_stays_plain_text() -> None:
    result = _render("각주 표기처럼 보이는 [^2] 텍스트.")
    assert result.citations == ()
    assert "cite-ref" not in result.html
    assert "[^2]" in result.html


def test_unused_definition_is_not_a_citation() -> None:
    assert extract_citations(f"본문뿐.\n\n[^unused]: {MW}\n") == ()


def test_definition_may_sit_directly_under_its_paragraph() -> None:
    body = f"근거가 있는 문장.[^mw]\n[^mw]: {MW}\n"
    result = _render(body)
    assert len(result.citations) == 1
    assert "[^mw]:" not in result.html


def test_javascript_url_never_becomes_a_source() -> None:
    """An unusable definition falls through to text, marker and all."""
    result = _render('위험한 출처.[^x]\n\n[^x]: javascript:alert(1) "nope"\n')
    assert result.citations == ()
    assert "href=" not in result.html
    assert "cite-ref" not in result.html


def test_relative_url_is_not_a_source() -> None:
    assert extract_citations("출처.[^x]\n\n[^x]: /pages/local\n") == ()


def test_title_is_escaped() -> None:
    result = _render(f'출처.[^x]\n\n[^x]: {MW} "<img onerror=x>"\n')
    assert "<img" not in result.html
    assert "&lt;img" in result.html


def test_host_is_extracted() -> None:
    assert extract_citations(BODY)[0].host == "www.mediawiki.org"


def test_citation_inside_emphasis() -> None:
    result = _render(f"*강조된 문장.[^mw]*\n\n[^mw]: {MW}\n")
    assert "<em>" in result.html
    assert len(result.citations) == 1


def test_definition_in_a_code_block_is_left_alone() -> None:
    body = f"    [^mw]: {MW}\n"
    result = _render(body)
    assert result.citations == ()
    assert "<code>" in result.html


def test_body_without_citations_gets_no_section() -> None:
    assert "citations" not in _render("그냥 본문.").html


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_page_response_carries_citations(make_page) -> None:
    page = make_page("델타 압축", BODY)
    assert [c["ordinal"] for c in page["citations"]] == [1, 2]
    assert page["citations"][0]["url"] == MW
    assert page["citations"][0]["title"] == "MediaWiki Manual: Text table"
    assert page["citations"][1]["host"] == "git-scm.com"


def test_page_citations_endpoint_reads_the_index(client, make_page) -> None:
    make_page("델타 압축", BODY)
    response = client.get("/api/pages/델타-압축/citations")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "델타-압축"
    assert [item["name"] for item in body["items"]] == ["mw", "git"]


def test_citations_endpoint_follows_an_old_name(client, make_page) -> None:
    make_page("Old Name", BODY)
    client.post("/api/pages/old-name/rename", json={"slug": "New Name"})
    response = client.get("/api/pages/old-name/citations")
    assert response.status_code == 200
    assert response.json()["slug"] == "new-name"


def test_rename_keeps_citations(client, make_page) -> None:
    make_page("Before", BODY)
    client.post("/api/pages/before/rename", json={"slug": "After"})
    assert len(client.get("/api/pages/after/citations").json()["items"]) == 2


def test_editing_replaces_the_index(client, make_page) -> None:
    make_page("Notes", BODY)
    client.put("/api/pages/notes", json={"content": f"하나만 남긴다.[^git]\n\n[^git]: {GIT}\n"})
    items = client.get("/api/pages/notes/citations").json()["items"]
    assert [item["url"] for item in items] == [GIT]


def test_reverse_lookup_finds_every_citing_page(client, make_page) -> None:
    make_page("첫 문서", BODY)
    make_page("둘째 문서", f"같은 출처를 쓴다.[^mw]\n\n[^mw]: {MW}\n")
    make_page("무관한 문서", "출처 없음")

    response = client.get("/api/citations", params={"url": MW})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    entry = body["items"][0]
    assert entry["page_count"] == 2
    assert sorted(page["slug"] for page in entry["pages"]) == ["둘째-문서", "첫-문서"]


def test_listing_groups_by_url_and_orders_by_use(client, make_page) -> None:
    make_page("A", BODY)
    make_page("B", f"또 인용.[^mw]\n\n[^mw]: {MW}\n")
    items = client.get("/api/citations").json()["items"]
    assert [item["url"] for item in items] == [MW, GIT]
    assert items[0]["page_count"] == 2
    # A title supplied by one page represents the source even if another omits it.
    assert items[0]["title"] == "MediaWiki Manual: Text table"


def test_host_filter_gathers_a_domain(client, make_page) -> None:
    make_page("A", BODY)
    body = client.get("/api/citations", params={"host": "git-scm.com"}).json()
    assert [item["url"] for item in body["items"]] == [GIT]


def test_substring_filter_matches_url_and_title(client, make_page) -> None:
    make_page("A", BODY)
    assert client.get("/api/citations", params={"q": "Text_table"}).json()["total"] == 1
    assert client.get("/api/citations", params={"q": "MediaWiki Manual"}).json()["total"] == 1
    assert client.get("/api/citations", params={"q": "없는-문자열"}).json()["total"] == 0


def test_deleting_a_page_drops_its_citations(client, make_page) -> None:
    make_page("Doomed", BODY)
    client.delete("/api/pages/doomed")
    assert client.get("/api/citations").json()["total"] == 0


def test_old_revision_renders_its_own_citations(client, make_page) -> None:
    make_page("History", BODY)
    client.put("/api/pages/history", json={"content": "출처를 다 뺐다."})
    first = client.get("/api/pages/history/revisions/1").json()
    assert [c["url"] for c in first["citations"]] == [MW, GIT]
    assert client.get("/api/pages/history").json()["citations"] == []


def test_cached_render_returns_the_same_citations(client, make_page) -> None:
    make_page("Cached", BODY)
    first = client.get("/api/pages/cached").json()
    second = client.get("/api/pages/cached").json()
    assert first["citations"] == second["citations"]
    assert first["html"] == second["html"]
