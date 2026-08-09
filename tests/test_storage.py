from __future__ import annotations

import gzip
import sqlite3

import pytest

from omnilog.delta import DeltaError, apply_delta, make_delta


# --------------------------------------------------------------------------
# delta codec
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "target"),
    [
        ("", ""),
        ("", "새 본문\n"),
        ("지워질 본문\n", ""),
        ("a\nb\nc\n", "a\nB\nc\n"),
        ("a\nb\nc\n", "a\nb\nc\nd\n"),
        ("a\nb\nc\nd\n", "a\nd\n"),
        ("끝에 개행 없음", "끝에 개행 없음!"),
        ("한글\n본문\n", "한글\n바뀐 본문\n줄 추가\n"),
        ("x\n" * 200, "x\n" * 100 + "y\n" + "x\n" * 99),
    ],
)
def test_delta_round_trips(base: str, target: str) -> None:
    assert apply_delta(base, make_delta(base, target)) == target


def test_delta_of_a_small_edit_is_small() -> None:
    lines = [f"{i}번째 줄입니다. 본문이 길어지도록 채웁니다.\n" for i in range(300)]
    base = "".join(lines)
    lines[150] = "150번째 줄 하나만 고쳤습니다.\n"
    target = "".join(lines)
    assert len(make_delta(base, target)) < len(base.encode()) // 20


@pytest.mark.parametrize(
    "payload",
    [b"", b"X1\n", b"D1\nC 0 999\n", b"D1\nI 99\nshort", b"D1\nZ 1\n", b"D1\nC x y\n"],
)
def test_malformed_deltas_are_rejected(payload: bytes) -> None:
    with pytest.raises(DeltaError):
        apply_delta("a\nb\n", payload)


# --------------------------------------------------------------------------
# compaction through the API
# --------------------------------------------------------------------------


def _rows(db, sql: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.fixture
def history(client, make_page):
    """A page with enough revisions to cross a keyframe boundary."""
    body = [f"{i}번 문단입니다. 위키 본문을 흉내 냅니다.\n" for i in range(40)]
    make_page("History", "".join(body))
    for edit in range(1, 40):
        body[edit % 40] = f"{edit % 40}번 문단 — 개정 {edit}에서 수정.\n"
        response = client.put("/api/pages/history", json={"content": "".join(body)})
        assert response.status_code == 200
    return "".join(body)


def test_every_revision_reads_back_intact_after_compaction(client, history, settings) -> None:
    before = [
        client.get(f"/api/pages/history/revisions/{n}").json()["content"]
        for n in range(1, 41)
    ]
    assert client.post("/api/maintenance/compact").status_code == 200
    after = [
        client.get(f"/api/pages/history/revisions/{n}").json()["content"]
        for n in range(1, 41)
    ]
    assert after == before


def test_compaction_reclaims_space(client, history) -> None:
    body = client.post("/api/maintenance/compact").json()
    assert body["converted"] > 0
    assert body["bytes_reclaimed"] > 0
    assert body["bytes_after"] < body["bytes_before"]


def test_current_revision_stays_whole(client, history, settings) -> None:
    """Reading the current page must never walk a delta."""
    rows = _rows(
        settings.db_path,
        """
        SELECT t.flags FROM page p
        JOIN revision r ON r.id = p.latest_rev_id
        JOIN text t ON t.id = r.text_id
        """,
    )
    assert rows and all("delta" not in row["flags"] for row in rows)


def test_deltas_never_point_at_another_delta(client, history, settings) -> None:
    """One hop is the whole promise; a chain would break the read budget."""
    client.post("/api/maintenance/compact")
    rows = _rows(
        settings.db_path,
        """
        SELECT base.flags AS base_flags
        FROM text d JOIN text base ON base.id = d.base_id
        WHERE d.flags LIKE '%delta%'
        """,
    )
    assert rows
    assert all("delta" not in row["base_flags"] for row in rows)


def test_reported_file_size_matches_the_file(client, history, settings) -> None:
    """Under WAL a VACUUM lands in the -wal file, so the size needs a checkpoint."""
    body = client.post("/api/maintenance/compact").json()
    assert body["file_after"] == settings.db_path.stat().st_size


def test_compaction_is_idempotent(client, history) -> None:
    first = client.post("/api/maintenance/compact").json()
    second = client.post("/api/maintenance/compact").json()
    assert (second["converted"], second["bundled"]) == (0, 0)
    assert second["bytes_before"] == first["bytes_after"]


# --------------------------------------------------------------------------
# blob bundling
# --------------------------------------------------------------------------


def test_bundling_packs_windows_into_shared_blobs(client, history, settings) -> None:
    body = client.post("/api/maintenance/compact").json()
    assert body["bundled"] > 0

    blobs = _rows(settings.db_path, "SELECT id, items FROM blob")
    assert blobs
    assert all(row["items"] > 1 for row in blobs)

    packed = _rows(
        settings.db_path, "SELECT COUNT(*) AS n FROM text WHERE flags LIKE '%blob%'"
    )
    assert packed[0]["n"] > 1


def test_packed_rows_carry_no_payload_of_their_own(client, history, settings) -> None:
    """A packed row is a pointer; its bytes live in the blob."""
    client.post("/api/maintenance/compact")
    rows = _rows(
        settings.db_path,
        "SELECT LENGTH(data) AS n, blob_id, blob_offset, blob_length "
        "FROM text WHERE flags LIKE '%blob%'",
    )
    assert rows
    for row in rows:
        assert row["n"] == 0
        assert row["blob_id"] is not None
        assert row["blob_offset"] is not None and row["blob_length"] is not None


def test_current_revision_is_never_bundled(client, history, settings) -> None:
    """Reading the live page must not unpack a shared stream."""
    client.post("/api/maintenance/compact")
    rows = _rows(
        settings.db_path,
        """
        SELECT t.flags FROM page p
        JOIN revision r ON r.id = p.latest_rev_id
        JOIN text t ON t.id = r.text_id
        """,
    )
    assert rows and all("blob" not in row["flags"] for row in rows)


def test_bundling_shrinks_storage_further(client, history, settings) -> None:
    """Delta-only compaction, then the same run with bundling, on equal input."""
    from omnilog import repository
    from omnilog.db import connect, write_tx

    conn = connect(settings)
    try:
        page_id = conn.execute("SELECT id FROM page").fetchone()["id"]
        text_ids = repository._page_text_ids(conn, page_id)
        with write_tx(conn):
            repository._deltify(conn, text_ids)
        after_deltas = repository.storage_bytes(conn)

        with write_tx(conn):
            repository._bundle(conn, text_ids, "2026-01-01T00:00:00Z")
        after_bundling = repository.storage_bytes(conn)
    finally:
        conn.close()

    assert after_bundling < after_deltas


def test_blobs_go_when_the_page_goes(client, history, settings) -> None:
    client.post("/api/maintenance/compact")
    assert _rows(settings.db_path, "SELECT id FROM blob")
    client.delete("/api/pages/history")
    assert _rows(settings.db_path, "SELECT id FROM blob") == []
    assert _rows(settings.db_path, "SELECT id FROM text") == []


def test_editing_after_bundling_still_works(client, history) -> None:
    client.post("/api/maintenance/compact")
    assert client.put("/api/pages/history", json={"content": "새 본문\n"}).status_code == 200
    assert client.get("/api/pages/history").json()["content"] == "새 본문\n"


def test_editing_still_works_after_compaction(client, history) -> None:
    client.post("/api/maintenance/compact")
    response = client.put("/api/pages/history", json={"content": "완전히 새 본문\n"})
    assert response.status_code == 200
    assert client.get("/api/pages/history").json()["content"] == "완전히 새 본문\n"


def test_deleting_a_page_with_deltas_works(client, history, settings) -> None:
    client.post("/api/maintenance/compact")
    assert client.delete("/api/pages/history").status_code == 204
    assert _rows(settings.db_path, "SELECT id FROM text") == []


def test_revert_reads_through_a_delta(client, history) -> None:
    client.post("/api/maintenance/compact")
    original = client.get("/api/pages/history/revisions/2").json()["content"]
    body = client.post("/api/pages/history/revisions/2/revert").json()
    assert body["content"] == original


def test_compaction_on_an_empty_wiki(client) -> None:
    body = client.post("/api/maintenance/compact").json()
    assert body == {
        "pages": 0,
        "converted": 0,
        "bundled": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "bytes_reclaimed": 0,
        "file_before": body["file_before"],
        "file_after": body["file_after"],
    }


# --------------------------------------------------------------------------
# render cache
# --------------------------------------------------------------------------


def test_cache_holds_only_the_current_revision(client, history, settings) -> None:
    for n in (1, 5, 20):
        client.get(f"/api/pages/history/revisions/{n}")
    client.get("/api/pages/history")

    rows = _rows(
        settings.db_path,
        """
        SELECT c.rev_id, p.latest_rev_id FROM render_cache c
        JOIN revision r ON r.id = c.rev_id
        JOIN page p ON p.id = r.page_id
        """,
    )
    assert len(rows) == 1
    assert rows[0]["rev_id"] == rows[0]["latest_rev_id"]


def test_cached_html_is_compressed_and_valid(client, make_page, settings) -> None:
    make_page("Cached", "# 제목\n\n본문입니다.\n")
    expected = client.get("/api/pages/cached").json()["html"]

    rows = _rows(settings.db_path, "SELECT html_gz FROM render_cache")
    assert len(rows) == 1
    assert gzip.decompress(rows[0]["html_gz"]).decode("utf-8") == expected


def test_old_cache_entries_go_when_a_new_revision_lands(client, make_page, settings) -> None:
    make_page("Churn", "v1\n")
    client.get("/api/pages/churn")
    for version in range(2, 6):
        client.put("/api/pages/churn", json={"content": f"v{version}\n"})
        client.get("/api/pages/churn")

    assert len(_rows(settings.db_path, "SELECT rev_id FROM render_cache")) == 1
