"""9강 · 출처: 본문에서 파생된 색인과, 문서와 따로 사는 레지스트리.

  - citation 행이 인라인 정의와 레지스트리 참조에서 각각 무엇을 담는지
  - 출처를 한 번 고치면 왜 인용한 모든 문서가 같이 바뀌는지 (cite_state)
  - 등록 안 된 키를 인용하면 어떻게 되는지 (빨간 링크의 출처 판)
  - 세 종류(link/text/file)가 각각 어디에 저장되는지
를 확인한다. 6강의 link_state 시연과 나란히 놓고 보면 좋다.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

from _common import fresh_db, line
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings

DB = fresh_db("lecture09")

MW = "https://www.mediawiki.org/wiki/Manual:Text_table"
GIT = "https://git-scm.com/docs/git-gc"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"scan bytes"


def cited_titles(html: str) -> list[str]:
    """각주 목록에 실제로 찍힌 출처 이름들."""
    return re.findall(r'<a class="cite-source"[^>]*>([^<]*)</a>', html)


def cache_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT c.link_state, c.cite_state, c.cites_json
        FROM render_cache c
        JOIN revision r ON r.id = c.rev_id
        JOIN page p ON p.id = r.page_id
        WHERE p.slug = ?
        """,
        (slug,),
    ).fetchone()


with TestClient(create_app(Settings(db_path=DB))) as client:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ================================================================ ①
    line("9강 · citation 행에는 무엇이 들어가나")
    client.post(
        "/api/sources",
        json={
            "key": "mw-text-table",
            "kind": "link",
            "title": "MediaWiki Manual: Text table",
            "url": MW,
            "author": "MediaWiki",
            "published": "2024",
        },
    )
    body = (
        "위키피디아는 델타를 쓴다.[^@mw-text-table]\n"
        "압축은 별도 작업으로 돌린다.[^@mw-text-table] 일회성 출처도 있다.[^blog]\n"
        "아직 등록 안 한 것도 쓸 수 있다.[^@나중에]\n\n"
        f'[^blog]: {GIT} "어떤 글"\n'
    )
    client.post("/api/pages", json={"title": "저장 구조", "content": body})

    print(f"{'name':<16} {'ord':>3} {'source_key':<14} {'url':<32} {'title':<10}")
    for row in conn.execute("SELECT * FROM citation ORDER BY ordinal"):
        print(
            f"{row['name']:<16} {row['ordinal']:>3} {row['source_key']:<14} "
            f"{row['url'][:32]:<32} {row['title']:<10}"
        )
    print("\n  -> 레지스트리 참조는 키만, 인라인 정의는 url/title/host를 담는다.")
    print("     제목 사본을 여기 두면 고칠 자리가 두 곳이 된다.")
    print(f"     본문에서 [^@mw-text-table]을 두 번 썼는데 행은 하나 "
          f"(PK가 문서+이름이라 등장 횟수는 저장하지 않는다)")

    line("9강 · 각주 목록에 찍힌 것")
    page = client.get("/api/pages/저장-구조").json()
    for cite in page["citations"]:
        print(f"  [{cite['ordinal']}] kind={cite['kind']:<8} title={cite['title'] or '(없음)':<28} "
              f"key={cite['source_key'] or '-'}")
    print("\n  -> 등록 안 된 '나중에'는 kind=missing. 오류가 아니라 상태다.")

    # ================================================================ ②
    line("9강 · 출처를 한 번 고치면 인용한 문서 전부가 바뀐다")
    client.post("/api/pages", json={"title": "다른 문서", "content": "같은 근거.[^@mw-text-table]"})
    before = {
        slug: client.get(f"/api/pages/{slug}").json()
        for slug in ("저장-구조", "다른-문서")
    }
    state_before = {slug: cache_row(conn, slug)["cite_state"] for slug in before}
    print("고치기 전 각주에 찍힌 이름:")
    for slug, view in before.items():
        print(f"  {slug:<10} {cited_titles(view['html'])}")

    client.put(
        "/api/sources/mw-text-table",
        json={"title": "MediaWiki: text 테이블", "locator": "Storage 절"},
    )
    print("\nPUT /api/sources/mw-text-table 한 번 -> 두 문서를 다시 읽으면:")
    for slug, view in before.items():
        after = client.get(f"/api/pages/{slug}").json()
        print(f"  {slug:<10} {cited_titles(after['html'])}")
        print(f"    리비전 번호 {view['revision']['number']} -> {after['revision']['number']},"
              f"  본문 동일: {view['content'] == after['content']},"
              f"  HTML 동일: {view['html'] == after['html']}")
        print(f"    cite_state {state_before[slug][:16]}… -> "
              f"{cache_row(conn, slug)['cite_state'][:16]}…")
    print("\n  -> 6강의 link_state와 같은 장치다. 본문은 그대로인데 바깥이 바뀌었으니")
    print("     캐시 히트여도 지문을 다시 재보고 다르면 버린다.")
    print("     지문에는 title·url뿐 아니라 author·published·locator도 들어간다.")
    print("     저자만 고쳐도 각주에 찍히는 값이라 무효화돼야 하기 때문이다.")

    # ================================================================ ③
    line("9강 · 끊어진 인용은 등록되는 순간 채워진다")

    def kind_of(slug: str, ordinal: int) -> str:
        page = client.get(f"/api/pages/{slug}").json()
        return page["citations"][ordinal - 1]["kind"]

    print(f"'나중에'를 인용한 상태          -> kind={kind_of('저장-구조', 3)}")
    client.post("/api/sources", json={"key": "나중에", "kind": "link", "url": GIT,
                                      "title": "뒤늦게 등록한 출처"})
    print(f"그 키로 출처를 등록한 뒤        -> kind={kind_of('저장-구조', 3)}")
    response = client.delete("/api/sources/나중에")
    print(f"다시 등록을 해제하면            -> kind={kind_of('저장-구조', 3)}"
          f"   (X-Dangling-Citations: {response.headers['X-Dangling-Citations']})")
    print("\n  -> 본문은 세 번 내내 '[^@나중에]' 그대로다. 문서를 지웠을 때")
    print("     [[위키링크]]가 빨간 링크로 돌아가는 것과 같은 처리다.")

    # ================================================================ ④
    line("9강 · 종류 셋이 각각 어디에 저장되나")
    client.post("/api/sources", json={
        "key": "kim-2004", "kind": "text", "title": "한국어 정보검색",
        "text": "트라이그램 색인은 부분 문자열 검색을 가능하게 한다. " * 12,
        "author": "김철수", "published": "2004", "locator": "112쪽"})
    client.post("/api/sources", json={"key": "scan-01", "kind": "file", "title": "원본 스캔"})
    client.post("/api/sources", json={"key": "scan-02", "kind": "file", "title": "같은 스캔 재사용"})
    for key in ("scan-01", "scan-02"):
        client.put(f"/api/sources/{key}/file", content=PNG,
                   headers={"Content-Type": "image/png"}, params={"filename": "표지.png"})

    print(f"{'key':<14} {'kind':<6} {'url':<34} {'text_id':>7} {'file_id':>7}")
    for row in conn.execute("SELECT key, kind, url, text_id, file_id FROM source ORDER BY id"):
        print(f"{row['key']:<14} {row['kind']:<6} {row['url'][:34]:<34} "
              f"{str(row['text_id'] or '-'):>7} {str(row['file_id'] or '-'):>7}")

    line("9강 · 인용문은 문서 본문과 같은 테이블에 들어간다")
    print(f"{'text.id':>7}  {'flags':<12} {'bytes':>6}  {'쓰는 쪽'}")
    revision_texts = {
        row["text_id"] for row in conn.execute("SELECT DISTINCT text_id FROM revision")
    }
    source_texts = {
        row["text_id"] for row in conn.execute(
            "SELECT text_id FROM source WHERE text_id IS NOT NULL")
    }
    for row in conn.execute("SELECT id, flags, LENGTH(data) AS n FROM text ORDER BY id"):
        owner = "문서 리비전" if row["id"] in revision_texts else (
            "출처 인용문" if row["id"] in source_texts else "-")
        print(f"{row['id']:>7}  {row['flags']:<12} {row['n']:>6}  {owner}")
    print("\n  -> 인용문도 512바이트가 넘으면 gzip된다. 출처용 저장 코드를 따로 쓰지 않는다.")

    line("9강 · 같은 파일을 두 출처에 붙이면")
    files = conn.execute(
        "SELECT id, sha256, filename, byte_size, LENGTH(data) AS n FROM file"
    ).fetchall()
    for row in files:
        users = [
            r["key"] for r in conn.execute(
                "SELECT key FROM source WHERE file_id = ? ORDER BY key", (row["id"],))
        ]
        print(f"file id={row['id']}  sha256={row['sha256'][:16]}…  "
              f"{row['filename']}  {row['byte_size']}bytes  <- {users}")
    print(f"\nfile 행 {len(files)}개, 붙인 출처는 2개. sha256이 UNIQUE라 바이트는 한 벌만 남는다.")
    print(f"업로드한 바이트의 해시: {hashlib.sha256(PNG).hexdigest()[:16]}…")

    client.delete("/api/sources/scan-01")
    remaining = conn.execute("SELECT COUNT(*) AS n FROM file").fetchone()["n"]
    download = client.get("/api/sources/scan-02/file")
    print(f"scan-01을 지운 뒤 file 행 {remaining}개, scan-02 내려받기 {download.status_code} "
          f"({len(download.content)}bytes) -> 아직 가리키는 출처가 있으면 지우지 않는다")

    # ================================================================ ⑤
    line("9강 · 역인덱스: 이 출처를 누가 쓰고 있나")
    used = client.get("/api/sources/mw-text-table/citations").json()
    print(f"GET /api/sources/mw-text-table/citations -> "
          f"{[item['slug'] for item in used['items']]}")

    hosted = client.get("/api/citations", params={"host": "git-scm.com"}).json()
    print(f"GET /api/citations?host=git-scm.com      -> "
          f"{[(i['title'], i['page_count']) for i in hosted['items']]}")
    print("  -> 링크 썩음은 URL 하나씩이 아니라 도메인 단위로 온다.")
    print("     host를 URL에서 따로 뽑아 인덱스를 걸어둔 이유다.")

    line("9강 · 위키가 인용하는 모든 것")
    print(f"{'source_key':<16} {'kind':<8} {'page_count':>10}  {'title'}")
    for item in client.get("/api/citations").json()["items"]:
        print(f"{item['source_key'] or '(인라인)':<16} {item['kind']:<8} "
              f"{item['page_count']:>10}  {item['title'] or '(제목 없음)'}")
    print("\n  -> 등록된 출처와 일회성 정의가 한 줄에 섞여 나온다.")
    print("     '이 위키가 무엇에 기대고 있나'라는 질문은 둘을 구분하지 않는다.")

    conn.close()

print(f"\n결과 DB: {DB}")
