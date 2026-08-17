"""5강 · FTS5, unicode61 vs trigram, BM25, LIKE 폴백, 인덱스 크기.

fts5vocab으로 실제 색인어를 꺼내 두 토크나이저를 비교하고,
사용자 입력에 섞인 FTS5 연산자가 무엇을 망가뜨리는지 확인한다.
"""
from __future__ import annotations

import sqlite3

from _common import fresh_db, line
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings

DB = fresh_db("lecture05")

# -------------------------------------------------------------- 토크나이저 비교
line("5강 · 같은 문장을 두 토크나이저가 어떻게 쪼개는가")

SENTENCE = "위키시스템 설계"
probe = sqlite3.connect(":memory:")
for tokenizer in ("unicode61", "trigram"):
    probe.execute(f"CREATE VIRTUAL TABLE t_{tokenizer} USING fts5(x, tokenize='{tokenizer}')")
    probe.execute(f"INSERT INTO t_{tokenizer}(x) VALUES (?)", (SENTENCE,))
    probe.execute(f"CREATE VIRTUAL TABLE v_{tokenizer} USING fts5vocab(t_{tokenizer}, 'row')")
    terms = [row[0] for row in probe.execute(f"SELECT term FROM v_{tokenizer} ORDER BY term")]
    print(f"{tokenizer:10} 색인어 {len(terms)}개  {' / '.join(repr(t) for t in terms)}")

print(f"\n원문: {SENTENCE!r}")
print(f"{'쿼리':<12} {'unicode61':>12} {'trigram':>10}")
for query in ("위키시스템", "위키시", "키시스", "설계", "위키", "시스"):
    verdicts = []
    for tokenizer in ("unicode61", "trigram"):
        hit = probe.execute(
            f"SELECT COUNT(*) FROM t_{tokenizer} WHERE x MATCH ?", (f'"{query}"',)
        ).fetchone()[0]
        verdicts.append("찾음" if hit else "못 찾음")
    print(f"{query:<12} {verdicts[0]:>12} {verdicts[1]:>10}")
probe.close()

# -------------------------------------------------------------- 실제 위키에서
PAGES = {
    "위키시스템": "위키시스템은 문서를 여러 사람이 함께 편집하는 시스템이다. "
                 "리비전이 쌓이고 되돌리기가 가능하다.",
    "검색 엔진": "전문 검색은 본문 안을 뒤진다. 역인덱스를 미리 만들어 두고 "
                "단어에서 문서를 찾는다. 위키시스템에도 검색이 필요하다.",
    "압축": "gzip은 반복을 참조로 바꾼다. 압축 창이 호출마다 초기화된다는 점이 중요하다.",
    "한국어 처리": "조사가 붙고 띄어쓰기가 불규칙하다. 그래서 부분 문자열 검색이 필요하다.",
    "리비전": "편집할 때마다 새 판본이 쌓인다. 기존 행은 수정하지 않는다.",
    "슬러그": "제목을 주소에 넣을 수 있는 형태로 바꾼 짧은 식별자다.",
    "델타": "직전 판본과의 차이만 저장한다. 본문보다 훨씬 작다.",
    "트랜잭션": "여러 쿼리를 전부 반영되거나 전부 취소되거나로 묶는 단위다.",
    "마크업": "본문은 CommonMark로 쓰고 대괄호 두 개로 다른 문서를 링크한다. "
             "한국어 처리가 여기서도 문제가 된다.",
}

with TestClient(create_app(Settings(db_path=DB))) as client:
    for title, content in PAGES.items():
        assert client.post(
            "/api/pages", json={"title": title, "content": content}
        ).status_code == 201
    print(f"\n문서 {len(PAGES)}개 작성")

    line("5강 · API로 검색해보면 (score가 null이면 LIKE 폴백을 탄 것)")
    for query in ("위키시스템", "검색", "위키", "압축 창", "gzip"):
        items = client.get("/api/search", params={"q": query}).json()["items"]
        path = "FTS5" if (items and items[0]["score"] is not None) else (
            "LIKE 폴백" if items else "-")
        print(f"\nq={query!r:<12} 결과 {len(items)}개   경로: {path}")
        for item in items[:3]:
            score = f"{item['score']:.3f}" if item["score"] is not None else "None"
            snippet = item["snippet"].replace("\x02", "[").replace("\x03", "]")
            print(f"    {item['title']:<10} score={score:>8}  {snippet[:56]}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    line("5강 · BM25는 왜 음수이고, 제목 가중치는 어떻게 먹히나")
    print("'한국어'로 검색 — 한 문서는 제목에, 다른 문서는 본문에만 들어있다.")
    print(f"{'문서':<12} {'어디에':<8} {'가중치 없음':>12} {'제목×5 (실제)':>15}")
    for row in conn.execute(
        """
        SELECT p.title,
               bm25(page_fts)                AS plain,
               bm25(page_fts, 0.0, 5.0, 1.0) AS weighted
        FROM page_fts JOIN page p ON p.id = page_fts.rowid
        WHERE page_fts MATCH '"한국어"' ORDER BY weighted
        """
    ):
        where = "제목" if "한국어" in row["title"] else "본문"
        print(f"{row['title']:<12} {where:<8} {row['plain']:>12.4f} {row['weighted']:>15.4f}")
    print("\n값이 작을수록(더 음수일수록) 관련도가 높다. 정렬은 ORDER BY score ASC.")
    print("검색어가 전체 문서의 절반에 들어있으면 IDF가 log(1)=0이 되어 점수가 0으로 붙는다.")

    line("5강 · 사용자 입력에 FTS5 연산자가 섞이면")
    print(f"{'입력':<18} {'날것으로 MATCH':<34} {'인용부호로 감싸면'}")
    for query in ("위키 OR 압축", "위키시스템*", "NEAR(위키 압축)", '따옴표" 주입'):
        outcomes = []
        for variant in (query, '"' + query.replace('"', '""') + '"'):
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?", (variant,)
                ).fetchone()[0]
                outcomes.append(f"결과 {n}개")
            except sqlite3.OperationalError as exc:
                outcomes.append(f"오류: {str(exc)[:26]}")
        print(f"{query:<18} {outcomes[0]:<34} {outcomes[1]}")

    line("5강 · 인덱스가 본문보다 큰가")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS v_probe USING fts5vocab(page_fts, 'row')")
    terms = conn.execute("SELECT COUNT(*) AS n FROM v_probe").fetchone()["n"]
    print(f"본문 총 글자 수      {sum(len(c) for c in PAGES.values()):>8,}")
    print(f"색인어(term) 종류    {terms:>8,}")
    for table, column in (("page_fts_data", "block"), ("page_fts_idx", "term"),
                          ("page_fts_docsize", "sz")):
        n = conn.execute(
            f"SELECT COALESCE(SUM(LENGTH({column})), 0) AS n FROM {table}"
        ).fetchone()["n"]
        print(f"{table:<20} {n:>8,}")
    print("\n샘플 색인어 12개:", ", ".join(
        repr(row["term"]) for row in conn.execute(
            "SELECT term FROM v_probe ORDER BY term LIMIT 12")))

    line("5강 · 3글자 미만이면 무슨 일이 벌어지나")
    for query in ("위키", "압축", "gz"):
        direct = conn.execute(
            "SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH ?", (f'"{query}"',)
        ).fetchone()[0]
        api = len(client.get("/api/search", params={"q": query}).json()["items"])
        print(f"q={query!r:<8} FTS5 직접: {direct}개    API 결과: {api}개 (LIKE 폴백이 받아냄)")

    conn.close()

print(f"\n결과 DB: {DB}")
