"""2~4강 · 불변성, 델타/키프레임, gzip 압축 창과 블롭 묶기.

리비전 61개짜리 문서를 만들고 압축 작업을 돌려서,
  - text 행이 물리적으로 재인코딩돼도 같은 문자열을 돌려주는지 (2강)
  - 델타가 키프레임만 가리켜 적용 횟수가 1을 넘지 않는지 (3강)
  - 조각을 나눠 압축하면 왜 커지는지 (4강)
를 확인한다.
"""
from __future__ import annotations

import gzip
import sqlite3

from _common import fresh_db, line
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings
from omnilog.textstore import load_text, raw_payload

DB = fresh_db("lecture02_04")
REVISIONS = 61

PARAS = [
    "위키는 문서를 여러 사람이 같이 고치는 시스템이다. 누가 무엇을 언제 바꿨는지 남는다.",
    "저장소는 본문을 메타데이터와 분리해서 담는다. 이력 목록이 본문을 읽지 않게 하려는 것이다.",
    "리비전은 편집할 때마다 새로 쌓이고 기존 행은 절대 수정되지 않는다.",
    "슬러그는 주소일 뿐이고 문서의 정체성은 절대 바뀌지 않는 정수 키가 맡는다.",
    "압축은 쓰기 경로에서 하지 않는다. 나중에 별도 작업으로 몰아서 한다.",
    "델타는 직전 판본이 아니라 구간의 키프레임을 가리킨다. 그래야 읽기가 한 번에 끝난다.",
    "gzip은 자기가 보고 있는 범위 안에서만 반복을 찾고, 호출이 끝나면 그 범위를 잊는다.",
    "전문 검색 인덱스는 본문보다 크다. 한국어 부분 문자열 검색의 대가다.",
]


def body(version: int) -> str:
    """약 35KB짜리 본문. 판본마다 딱 한 줄만 달라진다."""
    out = [f"# 저장소 설계 문서 (판본 {version})", ""]
    for section in range(40):
        out.append(f"## {section + 1}절")
        out.append("")
        for index, para in enumerate(PARAS):
            if section == version % 40 and index == version % len(PARAS):
                out.append(f"{para} 판본 {version}에서 이 줄만 고쳤다.")
            else:
                out.append(para)
        out.append("")
    return "\n".join(out)


def total_bytes(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(
        f"SELECT COALESCE(SUM(LENGTH(data)), 0) AS n FROM {table}"
    ).fetchone()["n"]


with TestClient(create_app(Settings(db_path=DB))) as client:
    assert client.post(
        "/api/pages", json={"title": "저장소 설계", "content": body(1)}
    ).status_code == 201
    for version in range(2, REVISIONS + 1):
        assert client.put(
            "/api/pages/저장소-설계", json={"content": body(version)}
        ).status_code == 200
    print(f"리비전 {REVISIONS}개 작성. 본문 하나 = {len(body(1).encode()):,} bytes")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------- 2강
    line("2강 · 압축 전 상태와 append-only 동작")
    before_strings = {
        row["id"]: load_text(conn, row["id"])
        for row in conn.execute("SELECT id FROM text ORDER BY id")
    }
    before_flags = {
        row["id"]: row["flags"]
        for row in conn.execute("SELECT id, flags FROM text ORDER BY id")
    }
    counts = {flag: list(before_flags.values()).count(flag) for flag in set(before_flags.values())}
    print(f"text 행 {len(before_strings)}개, flags 분포: {counts}")

    response = client.put("/api/pages/저장소-설계", json={"content": body(REVISIONS)})
    total = client.get("/api/pages/저장소-설계/revisions").json()["total"]
    print(f"\n같은 내용으로 다시 PUT -> changed={response.json()['changed']}, 리비전 {total}개")

    response = client.post("/api/pages/저장소-설계/revisions/3/revert", json={})
    total = client.get("/api/pages/저장소-설계/revisions").json()["total"]
    print(
        f"3번으로 revert -> 새 리비전 {response.json()['revision']['number']}번 생성, "
        f"총 {total}개 (이력을 고쳐 쓰지 않는다)"
    )

    # ---------------------------------------------------------------- 3강
    line("3강 · 압축 작업")
    print(f"압축 전  text={total_bytes(conn, 'text'):,}  blob={total_bytes(conn, 'blob'):,}")

    totals = client.post("/api/maintenance/compact?vacuum=true").json()
    for key in ("pages", "converted", "bundled", "bytes_before", "bytes_after",
                "bytes_reclaimed", "file_before", "file_after"):
        print(f"   {key:15} {totals[key]:,}")
    print(
        f"   본문 저장량 {100 * totals['bytes_reclaimed'] / totals['bytes_before']:.1f}% 감소"
        f" / DB 파일 "
        f"{100 * (totals['file_before'] - totals['file_after']) / totals['file_before']:.1f}% 감소"
    )

    after = sqlite3.connect(DB)
    after.row_factory = sqlite3.Row
    rows = after.execute(
        "SELECT id, flags, LENGTH(data) AS n, base_id, blob_id, blob_offset, blob_length "
        "FROM text ORDER BY id"
    ).fetchall()

    line("3강 · 압축 뒤 각 text 행이 무엇이 되었나 (앞 20개)")
    print(f"{'pos':>4} {'id':>4}  {'flags':26} {'data':>6} {'base':>5} "
          f"{'blob':>5} {'off':>7} {'len':>6}")
    for pos, row in enumerate(rows[:20]):
        keyframe = "◆키프레임" if pos % 16 == 0 else ""
        print(
            f"{pos:>4} {row['id']:>4}  {row['flags']:26} {row['n']:>6} "
            f"{str(row['base_id'] or '-'):>5} {str(row['blob_id'] or '-'):>5} "
            f"{str(row['blob_offset'] if row['blob_offset'] is not None else '-'):>7} "
            f"{str(row['blob_length'] or '-'):>6}  {keyframe}"
        )
    print(f"... (총 {len(rows)}행)")
    print(f"\n마지막 행 id={rows[-1]['id']}: flags={rows[-1]['flags']} -> 현재 본문은 통째로 둔다")

    line("3강 · 델타는 몇 홉을 거치는가")
    depths = []
    for row in rows:
        hops, current = 0, row["id"]
        while True:
            flags, _, base = raw_payload(after, current)
            if "delta" not in flags:
                break
            hops += 1
            current = base
        depths.append(hops)
    print(f"델타 적용 횟수 분포: {({d: depths.count(d) for d in sorted(set(depths))})}")
    print("  -> 어떤 리비전을 읽어도 최대 1회. 0회는 키프레임과 현재 본문")

    line("2강 · 물리 인코딩은 바뀌었는데 문자열은 그대로인가")
    now_flags = {row["id"]: row["flags"] for row in rows}
    changed = sum(1 for i, flag in before_flags.items() if flag != now_flags[i])
    same = all(load_text(after, i) == s for i, s in before_strings.items())
    print(f"flags가 바뀐 행: {changed} / {len(before_flags)}")
    print(f"모든 text.id가 압축 전과 똑같은 문자열을 돌려주는가: {same}")

    # ---------------------------------------------------------------- 4강
    line("4강 · 블롭 묶기 결과")
    blobs = after.execute("SELECT id, items, LENGTH(data) AS n FROM blob ORDER BY id").fetchall()
    for blob in blobs:
        inside = after.execute(
            "SELECT COALESCE(SUM(blob_length), 0) AS n FROM text WHERE blob_id = ?", (blob["id"],)
        ).fetchone()["n"]
        print(
            f"blob id={blob['id']}  담은 행={blob['items']:>3}  압축 후={blob['n']:>7,}  "
            f"압축 전 내용물={inside:>8,}  압축률={100 * blob['n'] / inside:.1f}%"
        )
    standalone = after.execute(
        "SELECT COUNT(*) AS n FROM text WHERE blob_id IS NULL"
    ).fetchone()["n"]
    print(
        f"\ngzip 스트림: 블롭 {len(blobs)}개 + 독립 행 {standalone}개 = "
        f"{len(blobs) + standalone}개  (압축 전에는 {len(rows)}개)"
    )
    print(f"text={total_bytes(after, 'text'):,}  blob={total_bytes(after, 'blob'):,}")

    line("4강 · 압축이 다 끝난 DB에서 무엇이 제일 큰가")
    file_size = DB.stat().st_size
    fts = after.execute(
        "SELECT COALESCE(SUM(LENGTH(block)), 0) AS n FROM page_fts_data"
    ).fetchone()["n"]
    bodies = total_bytes(after, "text") + total_bytes(after, "blob")
    cache = after.execute(
        "SELECT COALESCE(SUM(LENGTH(html_gz)), 0) AS n FROM render_cache"
    ).fetchone()["n"]
    print(f"DB 파일          {file_size:>9,}")
    print(f"page_fts_data    {fts:>9,}   ({100 * fts / file_size:.0f}%)")
    print(f"본문 text+blob   {bodies:>9,}   ({100 * bodies / file_size:.0f}%)")
    print(f"render_cache     {cache:>9,}")

    conn.close()
    after.close()

# -------------------------------------------------------------------- 4강 순수 실험
line("4강 · gzip 자체의 성질 (DB와 무관)")
print(f"빈 데이터를 gzip -> {len(gzip.compress(b'', 6, mtime=0))} bytes  (헤더 10 + 푸터 8 + 최소 블록)")

tiny = "C 0 12\nI 48\n이 줄만 고쳤다. 나머지는 그대로 둔다.\n".encode()
print(
    f"{len(tiny)}바이트 델타를 gzip -> {len(gzip.compress(tiny, 6, mtime=0))} bytes"
    f"  <- 압축했는데 더 커진다"
)

sample = (
    "델타는 직전 판본이 아니라 구간의 키프레임을 가리킨다. 그래야 읽기가 한 번에 끝난다.\n" * 6
).encode()
print(f"\n같은 데이터 {len(sample)}바이트를 몇 조각으로 나눠 압축하느냐:")
for pieces in (1, 2, 4, 8, 16):
    size = len(sample) // pieces
    chunks = [sample[i:i + size] for i in range(0, len(sample), size)]
    packed = sum(len(gzip.compress(chunk, 6, mtime=0)) for chunk in chunks)
    print(f"  {pieces:>2}조각 -> {packed:>5} bytes  {'█' * max(1, round(packed / 40))}")

print("\n창(window)이 호출마다 초기화되기 때문이다. 조각마다 백지에서 시작한다.")
print(f"\n결과 DB: {DB}")
