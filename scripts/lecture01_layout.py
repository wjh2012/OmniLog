"""1강 · 저장 구조 3단 분리.

문서를 만들고 이름을 바꿨을 때 각 테이블이 실제로 어떻게 변하는지 본다.
핵심은 rename이 만든 리비전이 직전 리비전의 text_id를 그대로 재사용한다는 것.
"""
from __future__ import annotations

import sqlite3

from _common import fresh_db, line
from fastapi.testclient import TestClient

from omnilog.app import create_app
from omnilog.config import Settings

DB = fresh_db("lecture01")

TABLES = [
    ("text", "id, flags, data"),
    ("page", "id, slug, title, latest_rev_id"),
    ("revision", "id, page_id, number, text_id, title, parent_id"),
    ("redirect", "from_slug, to_page_id"),
]


def dump(label: str) -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    line(label)
    for table, columns in TABLES:
        rows = conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1").fetchall()
        if not rows:
            print(f"{table:9} (비어 있음)")
            continue
        for row in rows:
            parts = []
            for key in row.keys():
                value = row[key]
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                parts.append(f"{key}={value}")
            print(f"{table:9} " + "  ".join(parts))
    conn.close()


with TestClient(create_app(Settings(db_path=DB))) as client:
    response = client.post(
        "/api/pages", json={"title": "위키 시스템", "content": "설계 문서."}
    )
    print("생성:", response.status_code, response.json()["slug"])
    dump("① 문서를 막 만든 직후")

    response = client.post(
        "/api/pages/위키-시스템/rename",
        json={"slug": "OmniLog 위키", "title": "OmniLog 위키"},
    )
    print("\nrename:", response.status_code, "->", response.json()["slug"])
    dump("② 이름을 바꾼 뒤 — text 행이 늘지 않고 text_id가 재사용된다")

    response = client.get("/api/pages/위키-시스템")
    body = response.json()
    print(
        f"\n옛 이름으로 GET: {response.status_code}  slug={body['slug']}  "
        f"redirected_from={body['redirected_from']}"
    )

    response = client.put(
        "/api/pages/omnilog-위키", json={"content": "설계 문서.\n\n한 줄 추가."}
    )
    print("본문 수정:", response.status_code, "changed =", response.json()["changed"])
    dump("③ 본문을 진짜 고친 뒤 — 이번엔 text 행이 새로 생긴다")

print(f"\n결과 DB: {DB}")
