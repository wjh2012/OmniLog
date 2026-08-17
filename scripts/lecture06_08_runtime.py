"""6~8강 · 렌더 캐시 무효화, SQLite 동시성, FastAPI 구조.

  - 본문이 그대로인데 링크 해석이 바뀌면 캐시가 어떻게 무효화되는지 (6강)
  - BEGIN DEFERRED는 왜 기다려도 안 풀리고 IMMEDIATE는 왜 풀리는지 (7강)
  - 요청 하나에 DB 연결이 어디서 열리고 닫히는지 (8강)
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from _common import fresh_db, line
from fastapi.testclient import TestClient

from omnilog import db as dbmod
from omnilog.app import create_app
from omnilog.config import Settings

DB = fresh_db("lecture06_08")

# ---- 8강: 연결이 열리고 닫히는 것을 센다. id()는 재사용되므로 쓰지 않는다.
events: list[tuple[str, str]] = []


class Tracked(sqlite3.Connection):
    def close(self) -> None:
        events.append(("close", threading.current_thread().name))
        super().close()


def spy_connect(settings: Settings) -> sqlite3.Connection:
    """omnilog/db.py 의 connect()와 동일하되 열림/닫힘만 기록한다."""
    events.append(("open", threading.current_thread().name))
    conn = sqlite3.connect(
        settings.db_path, timeout=10.0, isolation_level=None, factory=Tracked
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


real_db_connect = dbmod.connect
dbmod.connect = spy_connect
import omnilog.deps  # noqa: E402

omnilog.deps.connect = spy_connect


def anchor(html: str) -> str:
    match = re.search(r'<a class="[^"]*"[^>]*>', html)
    return match.group(0) if match else "(링크 없음)"


def cache_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rev_id, LENGTH(html_gz) AS n, links_json, link_state, cite_state "
        "FROM render_cache ORDER BY rev_id"
    ).fetchall()


with TestClient(create_app(Settings(db_path=DB))) as client:
    # ================================================================ 6강
    line("6강 · 아직 없는 문서를 가리키는 링크")
    client.post(
        "/api/pages", json={"title": "출발 문서", "content": "여기서 [[도착 문서]]로 간다."}
    )
    first = client.get("/api/pages/출발-문서").json()
    print("렌더된 링크:", anchor(first["html"]))
    print("links 배열   :", first["links"])

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for row in cache_rows(conn):
        print(f"\nrender_cache  rev_id={row['rev_id']}  html_gz={row['n']}bytes")
        print(f"  links_json = {row['links_json']}")
        print(f"  link_state = {row['link_state'][:32]}…")
        print(f"  cite_state = {row['cite_state'][:32]}…  <- 인용한 출처 쪽 지문 (9강)")
    state = cache_rows(conn)[0]["link_state"]

    line("6강 · 대상 문서를 만들면 본문은 그대로인데 결과가 바뀐다")
    client.post("/api/pages", json={"title": "도착 문서", "content": "도착했다."})
    second = client.get("/api/pages/출발-문서").json()
    print("렌더된 링크:", anchor(second["html"]))
    print("links 배열   :", second["links"])
    print(f"\n리비전 번호는 그대로: {first['revision']['number']} -> {second['revision']['number']}")
    print(f"본문도 그대로       : {first['content'] == second['content']}")
    print(f"그런데 HTML은 다름   : {first['html'] != second['html']}")
    print(f"\nlink_state: {state[:16]}… -> {cache_rows(conn)[0]['link_state'][:16]}…")
    print("  -> 캐시 히트여도 링크 해석을 다시 해보고, 해시가 다르면 버린다")
    state = cache_rows(conn)[0]["link_state"]

    line("6강 · 대상 문서의 이름을 바꾸면 (존재 여부는 그대로인데도)")
    client.post(
        "/api/pages/도착-문서/rename", json={"slug": "종착 문서", "title": "종착 문서"}
    )
    third = client.get("/api/pages/출발-문서").json()
    print("렌더된 링크:", anchor(third["html"]))
    print("links 배열   :", third["links"])
    print(f"link_state: {state[:16]}… -> {cache_rows(conn)[0]['link_state'][:16]}…")
    print("  -> 해시가 존재 여부만이 아니라 '리다이렉트인지'까지 포함하기 때문")
    print("\n캐시 행에는 이런 지문이 둘 있다. link_state는 [[위키링크]]가 가리키는")
    print("문서 쪽을, cite_state는 [^@키]가 가리키는 출처 쪽을 지켜본다. 본문 바깥에서")
    print("바뀔 수 있는 것이 둘이기 때문이고, 두 번째는 9강에서 본다.")

    line("6강 · 캐시 행은 문서당 몇 개인가")
    for n in range(2, 6):
        client.put("/api/pages/출발-문서",
                   json={"content": f"여기서 [[종착 문서]]로 간다. 판본 {n}."})
    for n in range(1, 5):
        client.get(f"/api/pages/출발-문서/revisions/{n}")

    for row in conn.execute(
        """
        SELECT c.rev_id, p.slug, r.number, p.latest_rev_id, LENGTH(c.html_gz) AS n
        FROM render_cache c
        JOIN revision r ON r.id = c.rev_id
        JOIN page p ON p.id = r.page_id
        ORDER BY r.page_id
        """
    ):
        mark = "= latest" if row["rev_id"] == row["latest_rev_id"] else "!! 과거"
        print(f"  page={row['slug']:<10} rev_id={row['rev_id']}  number={row['number']}  "
              f"html_gz={row['n']:>4}B   {mark}")
    pages = conn.execute("SELECT COUNT(*) AS n FROM page").fetchone()["n"]
    revisions = conn.execute("SELECT COUNT(*) AS n FROM revision").fetchone()["n"]
    cached = conn.execute("SELECT COUNT(*) AS n FROM render_cache").fetchone()["n"]
    print(f"\n문서 {pages}개, 리비전 {revisions}개(그중 과거 4개를 열람), 캐시 행 {cached}개")
    print("  -> 문서당 정확히 1행. 과거 리비전을 열어봐도 늘지 않는다")

    # ================================================================ 7강
    line("7강 · 낙관적 동시성 제어: base_revision")
    current = client.get("/api/pages/출발-문서").json()["revision"]["number"]
    print(f"현재 리비전 번호: {current}")
    response = client.put(
        "/api/pages/출발-문서", json={"content": "A가 씀", "base_revision": current}
    )
    print(f"A가 {current}번 기준으로 저장 -> {response.status_code}, "
          f"새 번호 {response.json()['revision']['number']}")
    response = client.put(
        "/api/pages/출발-문서", json={"content": "B가 씀", "base_revision": current}
    )
    print(f"B도 {current}번 기준으로 저장 -> {response.status_code}")
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))

    response = client.put("/api/pages/출발-문서", json={"content": "C가 씀"})
    print(f"\nbase_revision 없이 저장 -> {response.status_code} "
          f"(막지 않는다. 보호를 쓸지는 호출하는 쪽이 정한다)")

    line("7강 · 널 편집")
    body = client.get("/api/pages/출발-문서").json()["content"]
    before = client.get("/api/pages/출발-문서/revisions").json()["total"]
    response = client.put("/api/pages/출발-문서", json={"content": body})
    after = client.get("/api/pages/출발-문서/revisions").json()["total"]
    print(f"같은 내용으로 PUT -> changed={response.json()['changed']}, "
          f"리비전 {before} -> {after}")

    # ================================================================ 8강
    line("8강 · 요청 하나에 연결이 열리고 닫히는가")
    events.clear()
    for _ in range(3):
        client.get("/api/pages/출발-문서")
    opens = sum(1 for kind, _ in events if kind == "open")
    closes = sum(1 for kind, _ in events if kind == "close")
    print(f"요청 3번 -> connect() {opens}회, close() {closes}회")
    print(f"이벤트 순서: {[kind for kind, _ in events]}")
    print(f"실행 스레드: {sorted({name for _, name in events})}")
    print("\n  핸들러가 async def가 아니라 def이므로 FastAPI가 스레드풀에서 돌린다.")
    print("  sqlite3는 동기 라이브러리이고 busy_timeout이 최대 10초를 블로킹하므로,")
    print("  이벤트 루프에서 직접 돌리면 그동안 서버 전체가 멈춘다.")

    line("8강 · OpenAPI가 자동 생성한 것")
    spec = client.get("/openapi.json").json()
    print(f"제목: {spec['info']['title']} v{spec['info']['version']}")
    print(f"경로 {len(spec['paths'])}개, 스키마 {len(spec['components']['schemas'])}개")
    for path in sorted(spec["paths"])[:6]:
        print(f"   {','.join(m.upper() for m in spec['paths'][path]):<14} {path}")
    print("   …")

    conn.close()

# ================================================================ 7강 동시성
# TestClient가 닫힌 뒤에 해야 앱 쪽 연결과 락이 겹치지 않는다.


def two_connections() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    pair = tuple(
        sqlite3.connect(DB, timeout=5.0, isolation_level=None, check_same_thread=False)
        for _ in range(2)
    )
    for conn in pair:
        conn.execute("PRAGMA busy_timeout = 5000")
    return pair


line("7강 · BEGIN IMMEDIATE에서 늦은 쪽은 실패가 아니라 '기다린다'")
a, b = two_connections()
a.execute("BEGIN IMMEDIATE")
a.execute("UPDATE page SET title = title WHERE id = 1")
print("A: BEGIN IMMEDIATE + 쓰기 완료 (아직 커밋 안 함)")

threading.Thread(target=lambda: (time.sleep(0.4), a.execute("COMMIT"))).start()

start = time.perf_counter()
b.execute("BEGIN IMMEDIATE")
waited = time.perf_counter() - start
b.execute("UPDATE page SET title = title WHERE id = 1")
b.execute("COMMIT")
print(f"B: BEGIN IMMEDIATE에서 {waited:.2f}초 기다린 뒤 통과, 쓰기·커밋 성공")
print("   -> A가 커밋하자마자 B가 이어받았다. 실패가 아니라 직렬화다")
a.close()
b.close()

line("7강 · DEFERRED로 같은 순서를 밟으면")
a, b = two_connections()
a.execute("BEGIN DEFERRED")
b.execute("BEGIN DEFERRED")
a.execute("SELECT COUNT(*) FROM page").fetchone()
b.execute("SELECT COUNT(*) FROM page").fetchone()
print("A, B 둘 다 트랜잭션 시작 후 읽기 (편집 화면을 연 상태)")
a.execute("UPDATE page SET title = title WHERE id = 1")
print("A: 쓰기 성공 (여기서 읽기 -> 쓰기로 승격)")

threading.Thread(target=lambda: (time.sleep(0.4), a.execute("COMMIT"))).start()
start = time.perf_counter()
try:
    b.execute("UPDATE page SET title = title WHERE id = 1")
    print(f"B: 쓰기 성공 ({time.perf_counter() - start:.2f}초)")
except sqlite3.OperationalError as exc:
    print(f"B: {time.perf_counter() - start:.2f}초 만에 실패 -> {exc}")
    print("   busy_timeout이 5초인데 즉시 실패했다. 기다려서 풀릴 문제가 아니라는 뜻이다.")
    print("   B는 이미 낡은 스냅샷을 들고 있어서, 기다린들 그 위에 쓸 수 없다.")
time.sleep(0.6)
for conn in (a, b):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass
    conn.close()

line("7강 · WAL 파일과, 연결마다 다시 걸어야 하는 PRAGMA")
live = sqlite3.connect(DB, isolation_level=None)
live.execute("PRAGMA busy_timeout = 3000")
live.execute("BEGIN IMMEDIATE")
live.execute("UPDATE page SET title = title WHERE id = 1")
live.execute("COMMIT")
print("연결이 열려 있고 방금 커밋한 상태:")
for suffix in ("", "-wal", "-shm"):
    path = Path(str(DB) + suffix)
    size = path.stat().st_size if path.exists() else 0
    print(f"  {path.name:<22} {'있음' if path.exists() else '없음':<5} {size:>9,} bytes")
print(f"  journal_mode = {live.execute('PRAGMA journal_mode').fetchone()[0]}")
print(f"  이 연결의 foreign_keys = {live.execute('PRAGMA foreign_keys').fetchone()[0]}"
      f"  <- PRAGMA를 안 걸었으므로 꺼져 있다")
live.close()

print("\n연결을 모두 닫은 뒤 (SQLite가 체크포인트하고 정리):")
for suffix in ("", "-wal", "-shm"):
    path = Path(str(DB) + suffix)
    size = path.stat().st_size if path.exists() else 0
    print(f"  {path.name:<22} {'있음' if path.exists() else '없음':<5} {size:>9,} bytes")

check = real_db_connect(Settings(db_path=DB))
print(f"\nomnilog.db.connect()로 연 연결의 foreign_keys = "
      f"{check.execute('PRAGMA foreign_keys').fetchone()[0]}  <- connect()가 켜준다")
check.close()

print(f"\n결과 DB: {DB}")
