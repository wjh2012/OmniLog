from __future__ import annotations

import hashlib

import pytest

MW = "https://www.mediawiki.org/wiki/Manual:Text_table"
GIT = "https://git-scm.com/docs/git-gc"


@pytest.fixture
def make_source(client):
    def _make(key: str, kind: str = "link", **kwargs):
        payload = {"key": key, "kind": kind, **kwargs}
        if kind == "link":
            payload.setdefault("url", MW)
        response = client.post("/api/sources", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


# --------------------------------------------------------------------------
# registry CRUD
# --------------------------------------------------------------------------


def test_register_a_link_source(client, make_source) -> None:
    source = make_source(
        "mw-text-table",
        title="MediaWiki Manual: Text table",
        author="MediaWiki",
        published="2024",
        locator="Storage 절",
    )
    assert source["key"] == "mw-text-table"
    assert source["host"] == "www.mediawiki.org"
    assert source["page_count"] == 0
    assert client.get("/api/sources/mw-text-table").json()["author"] == "MediaWiki"


def test_key_is_derived_from_the_title(client) -> None:
    response = client.post(
        "/api/sources", json={"title": "델타 압축 문서", "kind": "link", "url": MW}
    )
    assert response.status_code == 201
    assert response.json()["key"] == "델타-압축-문서"


def test_duplicate_key_is_409(client, make_source) -> None:
    make_source("dup")
    response = client.post("/api/sources", json={"key": "dup", "kind": "link", "url": MW})
    assert response.status_code == 409
    assert response.json()["code"] == "source_conflict"


def test_link_source_needs_a_url(client) -> None:
    response = client.post("/api/sources", json={"key": "empty", "kind": "link"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_source"


def test_source_url_must_be_http(client) -> None:
    response = client.post(
        "/api/sources", json={"key": "bad", "kind": "link", "url": "javascript:alert(1)"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_source"


def test_text_source_carries_its_passage(client, make_source) -> None:
    source = make_source(
        "kim-2004",
        kind="text",
        title="한국어 위키의 검색",
        text="트라이그램 토크나이저는 부분 문자열을 찾는다." * 20,
        author="김",
        locator="112쪽",
    )
    assert source["kind"] == "text"
    assert source["text"].startswith("트라이그램")
    assert client.get("/api/sources/kim-2004").json()["text"] == source["text"]


def test_text_source_needs_text(client) -> None:
    response = client.post("/api/sources", json={"key": "hollow", "kind": "text"})
    assert response.status_code == 400


def test_text_cannot_be_registered_on_a_link_source(client) -> None:
    response = client.post(
        "/api/sources", json={"key": "mixed", "kind": "link", "url": MW, "text": "안 됨"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_source"


def test_unknown_kind_is_rejected(client) -> None:
    response = client.post("/api/sources", json={"key": "x", "kind": "podcast"})
    assert response.status_code == 422  # pydantic rejects it before the repository


def test_update_corrects_fields_in_place(client, make_source) -> None:
    make_source("mw", title="오타 있는 제목")
    response = client.put("/api/sources/mw", json={"title": "고친 제목", "url": GIT})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "고친 제목"
    assert body["host"] == "git-scm.com"
    # Fields left out keep their value.
    assert body["kind"] == "link"


def test_update_replaces_a_text_body(client, make_source) -> None:
    make_source("quote", kind="text", text="원래 인용문")
    client.put("/api/sources/quote", json={"text": "고친 인용문"})
    assert client.get("/api/sources/quote").json()["text"] == "고친 인용문"


def test_text_cannot_be_put_on_a_link_source(client, make_source) -> None:
    make_source("mw")
    response = client.put("/api/sources/mw", json={"text": "안 됨"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_source"


def test_missing_source_is_404(client) -> None:
    assert client.get("/api/sources/nope").status_code == 404
    assert client.get("/api/sources/nope").json()["code"] == "source_not_found"


def test_listing_filters_by_kind_and_host(client, make_source) -> None:
    make_source("a", url=MW)
    make_source("b", url=GIT)
    make_source("c", kind="text", text="인용문")

    assert client.get("/api/sources").json()["total"] == 3
    assert client.get("/api/sources", params={"kind": "text"}).json()["total"] == 1
    hosted = client.get("/api/sources", params={"host": "git-scm.com"}).json()
    assert [item["key"] for item in hosted["items"]] == ["b"]


def test_listing_searches_key_title_author_and_url(client, make_source) -> None:
    make_source("mw-text-table", title="Text table", author="MediaWiki")
    assert client.get("/api/sources", params={"q": "text-table"}).json()["total"] == 1
    assert client.get("/api/sources", params={"q": "MediaWiki"}).json()["total"] == 1
    assert client.get("/api/sources", params={"q": "없음"}).json()["total"] == 0


# --------------------------------------------------------------------------
# citing a registered source
# --------------------------------------------------------------------------


def test_page_cites_a_registered_source(client, make_source, make_page) -> None:
    make_source(
        "mw-text-table",
        title="MediaWiki Manual: Text table",
        author="MediaWiki",
        published="2024",
        locator="Storage 절",
    )
    page = make_page("델타 압축", "위키피디아는 델타를 쓴다.[^@mw-text-table]")

    cite = page["citations"][0]
    assert cite == {
        "ordinal": 1,
        "name": "@mw-text-table",
        "source_key": "mw-text-table",
        "kind": "link",
        "title": "MediaWiki Manual: Text table",
        "url": MW,
        "host": "www.mediawiki.org",
        "author": "MediaWiki",
        "published": "2024",
        "locator": "Storage 절",
    }
    assert 'href="https://www.mediawiki.org/wiki/Manual:Text_table"' in page["html"]
    assert 'data-source="mw-text-table"' in page["html"]


def test_no_definition_line_is_needed(client, make_source, make_page) -> None:
    make_source("mw-text-table")
    page = make_page("문서", "근거가 있다.[^@mw-text-table]")
    assert "[^@" not in page["html"]
    assert 'class="cite-ref"' in page["html"]


def test_registry_reference_and_inline_definition_share_the_numbering(
    client, make_source, make_page
) -> None:
    make_source("mw-text-table")
    page = make_page(
        "섞어쓰기",
        f"등록된 출처.[^@mw-text-table] 일회성 출처.[^blog]\n\n[^blog]: {GIT} \"어떤 글\"\n",
    )
    assert [(c["ordinal"], c["kind"]) for c in page["citations"]] == [
        (1, "link"),
        (2, "inline"),
    ]


def test_key_in_the_body_is_slugified(client, make_source, make_page) -> None:
    """[^@Manual Text] finds the source keyed manual-text, like a wikilink does."""
    make_source("manual-text")
    page = make_page("문서", "근거.[^@Manual Text]")
    assert page["citations"][0]["source_key"] == "manual-text"


def test_citing_an_unregistered_key_renders_as_missing(client, make_page) -> None:
    page = make_page("이른 인용", "아직 없는 출처.[^@not-yet]")
    cite = page["citations"][0]
    assert cite["kind"] == "missing"
    assert cite["url"] == ""
    assert 'class="cite-source cite-missing"' in page["html"]
    assert 'data-exists="false"' in page["html"]


def test_footnote_shows_author_year_and_locator(client, make_source, make_page) -> None:
    make_source(
        "kim-2004",
        kind="text",
        title="한국어 정보검색",
        text="트라이그램 색인은 부분 문자열 검색을 가능하게 한다.",
        author="김",
        published="2004",
        locator="112쪽",
    )
    html = make_page("문서", "책에 따르면.[^@kim-2004]")["html"]
    assert (
        '<span class="cite-meta">'
        '<span class="cite-author">김</span>, '
        '<span class="cite-published">2004</span>, '
        '<span class="cite-locator">112쪽</span></span>' in html
    )


def test_missing_bibliographic_fields_are_skipped(client, make_source, make_page) -> None:
    make_source("mw", title="제목만 있는 출처", published="2024")
    html = make_page("문서", "근거.[^@mw]")["html"]
    assert '<span class="cite-meta"><span class="cite-published">2024</span></span>' in html
    assert "cite-author" not in html
    assert "cite-locator" not in html


def test_a_source_with_no_metadata_gets_no_meta_span(client, make_source, make_page) -> None:
    make_source("mw", title="제목만")
    assert "cite-meta" not in make_page("문서", "근거.[^@mw]")["html"]


def test_inline_definitions_carry_no_metadata(make_page) -> None:
    page = make_page("문서", f'근거.[^blog]\n\n[^blog]: {GIT} "어떤 글"\n')
    assert page["citations"][0]["author"] == ""
    assert "cite-meta" not in page["html"]


def test_correcting_an_author_reaches_every_citing_page(
    client, make_source, make_page
) -> None:
    """The metadata is in the render, so it needs the same cache check the title gets."""
    make_source("kim-2004", title="한국어 정보검색", author="김", published="2004")
    make_page("문서", "근거.[^@kim-2004]")
    assert "김" in client.get("/api/pages/문서").json()["html"]

    client.put("/api/sources/kim-2004", json={"author": "김철수", "locator": "112쪽"})
    page = client.get("/api/pages/문서").json()
    assert '<span class="cite-author">김철수</span>' in page["html"]
    assert '<span class="cite-locator">112쪽</span>' in page["html"]
    assert page["citations"][0]["author"] == "김철수"


def test_metadata_is_escaped(client, make_source, make_page) -> None:
    make_source("x", title="제목", author='<img onerror="x">')
    assert "<img" not in make_page("문서", "근거.[^@x]")["html"]


def test_text_source_citation_points_at_the_registry(client, make_source, make_page) -> None:
    make_source("kim-2004", kind="text", title="한국어 위키의 검색", text="인용문")
    page = make_page("문서", "책에 따르면.[^@kim-2004]")
    assert page["citations"][0]["kind"] == "text"
    assert 'href="/sources/kim-2004"' in page["html"]


def test_index_stores_the_key_not_a_copy(client, make_source, make_page) -> None:
    make_source("mw", title="처음 제목")
    make_page("문서", "근거.[^@mw]")
    client.put("/api/sources/mw", json={"title": "고친 제목"})

    items = client.get("/api/pages/문서/citations").json()["items"]
    assert items[0]["title"] == "고친 제목"


def test_correcting_a_source_updates_every_citing_page(
    client, make_source, make_page
) -> None:
    make_source("mw", title="처음 제목")
    make_page("첫 문서", "근거.[^@mw]")
    make_page("둘째 문서", "같은 근거.[^@mw]")
    client.put("/api/sources/mw", json={"title": "고친 제목", "url": GIT})

    for slug in ("첫-문서", "둘째-문서"):
        page = client.get(f"/api/pages/{slug}").json()
        assert page["citations"][0]["title"] == "고친 제목"
        assert "고친 제목" in page["html"]
        assert f'href="{GIT}"' in page["html"]


def test_registering_a_source_later_fixes_a_dangling_citation(
    client, make_source, make_page
) -> None:
    make_page("먼저 쓴 문서", "근거.[^@late]")
    assert client.get("/api/pages/먼저-쓴-문서").json()["citations"][0]["kind"] == "missing"

    make_source("late", title="뒤늦게 등록")
    page = client.get("/api/pages/먼저-쓴-문서").json()
    assert page["citations"][0]["kind"] == "link"
    assert "뒤늦게 등록" in page["html"]


def test_source_citations_endpoint_is_the_reverse_index(
    client, make_source, make_page
) -> None:
    make_source("mw")
    make_page("첫 문서", "근거.[^@mw]")
    make_page("둘째 문서", "근거.[^@mw]")
    make_page("무관한 문서", "출처 없음")

    body = client.get("/api/sources/mw/citations").json()
    assert body["key"] == "mw"
    assert sorted(item["slug"] for item in body["items"]) == ["둘째-문서", "첫-문서"]
    assert client.get("/api/sources/mw").json()["page_count"] == 2


def test_deleting_a_source_reports_and_dangles_citations(
    client, make_source, make_page
) -> None:
    make_source("mw", title="사라질 출처")
    make_page("문서", "근거.[^@mw]")

    response = client.delete("/api/sources/mw")
    assert response.status_code == 204
    assert response.headers["X-Dangling-Citations"] == "1"

    page = client.get("/api/pages/문서").json()
    assert page["citations"][0]["kind"] == "missing"
    assert "사라질 출처" not in page["html"]


def test_deleting_a_page_leaves_the_source_alone(client, make_source, make_page) -> None:
    make_source("mw")
    make_page("문서", "근거.[^@mw]")
    client.delete("/api/pages/문서")
    assert client.get("/api/sources/mw").json()["page_count"] == 0


def test_wiki_wide_listing_mixes_registered_and_inline(
    client, make_source, make_page
) -> None:
    make_source("mw", title="등록된 출처")
    make_page("A", f"등록.[^@mw] 일회성.[^blog]\n\n[^blog]: {GIT}\n")
    make_page("B", "등록.[^@mw]")

    items = client.get("/api/citations").json()["items"]
    assert [(item["source_key"], item["kind"], item["page_count"]) for item in items] == [
        ("mw", "link", 2),
        ("", "inline", 1),
    ]


def test_wiki_wide_listing_filters_by_key(client, make_source, make_page) -> None:
    make_source("mw")
    make_page("A", "근거.[^@mw]")
    body = client.get("/api/citations", params={"key": "mw"}).json()
    assert body["total"] == 1
    assert [page["slug"] for page in body["items"][0]["pages"]] == ["a"]


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake png bytes"


def test_upload_and_download_a_file_source(client, make_source) -> None:
    make_source("scan", kind="file", title="스캔본")
    response = client.put(
        "/api/sources/scan/file",
        content=PNG,
        headers={"Content-Type": "image/png"},
        params={"filename": "표지.png"},
    )
    assert response.status_code == 200
    attached = response.json()["file"]
    assert attached["byte_size"] == len(PNG)
    assert attached["media_type"] == "image/png"
    assert attached["sha256"] == hashlib.sha256(PNG).hexdigest()

    download = client.get("/api/sources/scan/file")
    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["content-type"] == "image/png"
    assert "inline" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"


def test_downloading_before_upload_is_404(client, make_source) -> None:
    make_source("scan", kind="file")
    response = client.get("/api/sources/scan/file")
    assert response.status_code == 404
    assert response.json()["code"] == "file_not_attached"


def test_html_upload_is_served_as_a_download(client, make_source) -> None:
    """An uploaded page served inline would be script running on this origin."""
    make_source("evil", kind="file")
    client.put(
        "/api/sources/evil/file",
        content=b"<script>alert(1)</script>",
        headers={"Content-Type": "text/html"},
    )
    download = client.get("/api/sources/evil/file")
    assert "attachment" in download.headers["content-disposition"]


def test_upload_over_the_limit_is_413(client, make_source, settings) -> None:
    make_source("big", kind="file")
    response = client.put(
        "/api/sources/big/file", content=b"x" * (settings.max_file_bytes + 1)
    )
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_upload_only_lands_on_a_file_source(client, make_source) -> None:
    make_source("mw")
    response = client.put("/api/sources/mw/file", content=PNG)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_source"


def test_identical_bytes_are_stored_once(client, make_source) -> None:
    make_source("one", kind="file")
    make_source("two", kind="file")
    for key in ("one", "two"):
        client.put(f"/api/sources/{key}/file", content=PNG, headers={"Content-Type": "image/png"})

    first = client.get("/api/sources/one").json()["file"]
    second = client.get("/api/sources/two").json()["file"]
    assert first["sha256"] == second["sha256"]
    # Deleting one source must not take the bytes the other still points at.
    client.delete("/api/sources/one")
    assert client.get("/api/sources/two/file").content == PNG


def test_replacing_a_file_drops_the_old_bytes(client, make_source) -> None:
    make_source("scan", kind="file")
    client.put("/api/sources/scan/file", content=PNG, headers={"Content-Type": "image/png"})
    client.put("/api/sources/scan/file", content=b"new bytes", headers={"Content-Type": "text/plain"})
    assert client.get("/api/sources/scan/file").content == b"new bytes"


def test_a_filename_cannot_smuggle_a_path(client, make_source) -> None:
    make_source("scan", kind="file")
    response = client.put(
        "/api/sources/scan/file", content=PNG, params={"filename": "../../etc/passwd"}
    )
    assert response.json()["file"]["filename"] == "passwd"


def test_file_citation_links_to_the_registry(client, make_source, make_page) -> None:
    make_source("scan", kind="file", title="스캔본")
    client.put("/api/sources/scan/file", content=PNG, headers={"Content-Type": "image/png"})
    page = make_page("문서", "사진이 근거다.[^@scan]")
    assert page["citations"][0]["kind"] == "file"
    assert 'href="/sources/scan"' in page["html"]
