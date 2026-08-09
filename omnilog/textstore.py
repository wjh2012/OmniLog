"""Store for revision bodies.

Rows are **logically** immutable: the string a given `text.id` resolves to never
changes, which is what lets the render cache key on `rev_id` alone. The physical
encoding is not immutable — compaction may re-encode a row as a delta, or move
its payload into a shared blob. That is the same trade `git repack` makes.
Writes always take the simple path and store the whole body; shrinking happens
later, offline.

A row's payload can be reached three ways, recorded in `flags`:

    utf-8                  data holds the body verbatim
    utf-8,gzip             data holds the gzipped body
    utf-8,blob             the payload is a slice of blob.data
    …,delta                whatever came out is a delta against base_id
"""

from __future__ import annotations

import gzip
import sqlite3

from .delta import DeltaError, apply_delta

_ENCODING = "utf-8"
_GZIP = "gzip"
_DELTA = "delta"
_BLOB = "blob"

#: Deltas are written against a keyframe, never against another delta, so one
#: hop is all a correct database ever needs. The cap is a corruption guard.
_MAX_DELTA_DEPTH = 4

#: Maps blob id -> decompressed stream, for the span of one read. A delta and
#: its base usually share a blob, so without this a single read would unzip the
#: same stream twice.
BlobCache = dict


def _encode(content: str, compress_min_bytes: int) -> tuple[str, bytes, int]:
    raw = content.encode(_ENCODING)
    if len(raw) >= compress_min_bytes:
        # mtime=0 keeps the blob deterministic for a given body.
        return f"{_ENCODING},{_GZIP}", gzip.compress(raw, compresslevel=6, mtime=0), len(raw)
    return _ENCODING, raw, len(raw)


def store_text(conn: sqlite3.Connection, content: str, compress_min_bytes: int) -> tuple[int, int]:
    """Write `content` whole and return ``(text_id, uncompressed_byte_size)``."""
    flags, data, size = _encode(content, compress_min_bytes)
    cursor = conn.execute(
        "INSERT INTO text (flags, data, base_id) VALUES (?, ?, NULL)", (flags, data)
    )
    return int(cursor.lastrowid), size


def _blob_stream(conn: sqlite3.Connection, blob_id: int, cache: BlobCache | None) -> bytes:
    if cache is not None and blob_id in cache:
        return cache[blob_id]
    row = conn.execute("SELECT data FROM blob WHERE id = ?", (blob_id,)).fetchone()
    if row is None:
        raise LookupError(f"blob {blob_id} is missing")
    stream = gzip.decompress(row["data"])
    if cache is not None:
        cache[blob_id] = stream
    return stream


def raw_payload(
    conn: sqlite3.Connection, text_id: int, cache: BlobCache | None = None
) -> tuple[list[str], bytes, int | None]:
    """Return ``(flags, payload bytes, base_id)`` without resolving a delta."""
    row = conn.execute(
        "SELECT flags, data, base_id, blob_id, blob_offset, blob_length "
        "FROM text WHERE id = ?",
        (text_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"text row {text_id} is missing")

    flags = row["flags"].split(",")
    if _BLOB in flags:
        stream = _blob_stream(conn, row["blob_id"], cache)
        start = row["blob_offset"]
        payload = stream[start : start + row["blob_length"]]
    elif _GZIP in flags:
        payload = gzip.decompress(row["data"])
    else:
        payload = bytes(row["data"])
    return flags, payload, row["base_id"]


def load_text(
    conn: sqlite3.Connection,
    text_id: int,
    _depth: int = 0,
    cache: BlobCache | None = None,
) -> str:
    """Resolve a body, unpacking a blob slice and applying a delta as needed."""
    if cache is None:
        cache = {}
    flags, payload, base_id = raw_payload(conn, text_id, cache)

    if _DELTA not in flags:
        return payload.decode(_ENCODING)

    if _depth >= _MAX_DELTA_DEPTH:
        raise DeltaError(f"delta chain from text row {text_id} is too deep")
    if base_id is None:
        raise DeltaError(f"text row {text_id} is a delta with no base")
    return apply_delta(load_text(conn, base_id, _depth + 1, cache), payload)


def replace_with_delta(
    conn: sqlite3.Connection, text_id: int, base_id: int, payload: bytes
) -> None:
    """Re-encode an existing row as a delta against `base_id`.

    Only compaction calls this, and only after checking that the row resolves
    to the same string afterwards.
    """
    conn.execute(
        "UPDATE text SET flags = ?, data = ?, base_id = ?, "
        "blob_id = NULL, blob_offset = NULL, blob_length = NULL WHERE id = ?",
        (f"{_ENCODING},{_GZIP},{_DELTA}", gzip.compress(payload, 6, mtime=0), base_id, text_id),
    )


def pack_into_blob(conn: sqlite3.Connection, text_ids: list[int], now: str) -> int:
    """Move several rows' payloads into one shared gzip stream.

    Returns the bytes saved, or 0 if bundling would not have helped and nothing
    was changed. The rows keep their own `delta` flag and `base_id`; only where
    the payload lives changes.
    """
    before = 0
    entries: list[tuple[int, list[str], bytes]] = []
    cache: BlobCache = {}
    for text_id in text_ids:
        flags, payload, _ = raw_payload(conn, text_id, cache)
        if _BLOB in flags:  # already packed; leave the whole window alone
            return 0
        before += stored_size(conn, text_id)
        entries.append((text_id, flags, payload))

    stream = b"".join(payload for _, _, payload in entries)
    data = gzip.compress(stream, 6, mtime=0)
    if len(data) >= before:
        return 0

    blob_id = int(
        conn.execute(
            "INSERT INTO blob (data, items, created_at) VALUES (?, ?, ?)",
            (data, len(entries), now),
        ).lastrowid
    )

    offset = 0
    for text_id, flags, payload in entries:
        kept = [flag for flag in flags if flag not in (_GZIP, _BLOB)]
        kept.append(_BLOB)
        conn.execute(
            "UPDATE text SET flags = ?, data = ?, "
            "blob_id = ?, blob_offset = ?, blob_length = ? WHERE id = ?",
            (",".join(kept), b"", blob_id, offset, len(payload), text_id),
        )
        offset += len(payload)
    return before - len(data)


def stored_size(conn: sqlite3.Connection, text_id: int) -> int:
    row = conn.execute("SELECT LENGTH(data) AS n FROM text WHERE id = ?", (text_id,)).fetchone()
    return 0 if row is None else int(row["n"])


def has_flag(conn: sqlite3.Connection, text_id: int, flag: str) -> bool:
    row = conn.execute("SELECT flags FROM text WHERE id = ?", (text_id,)).fetchone()
    return row is not None and flag in row["flags"].split(",")


def is_delta(conn: sqlite3.Connection, text_id: int) -> bool:
    return has_flag(conn, text_id, _DELTA)


def is_packed(conn: sqlite3.Connection, text_id: int) -> bool:
    return has_flag(conn, text_id, _BLOB)


def encoded_delta_size(payload: bytes) -> int:
    """Bytes a delta would occupy once stored."""
    return len(gzip.compress(payload, 6, mtime=0))


def storage_bytes(conn: sqlite3.Connection) -> int:
    """Everything the two body tables occupy, standalone rows plus blobs."""
    total = 0
    for table in ("text", "blob"):
        total += conn.execute(
            f"SELECT COALESCE(SUM(LENGTH(data)), 0) AS n FROM {table}"
        ).fetchone()["n"]
    return total


def delete_texts(conn: sqlite3.Connection, text_ids: list[int]) -> None:
    """Drop bodies whose revisions are gone. Only used when a page is deleted.

    Deltas among the doomed rows reference each other, so every base link is
    cleared before anything is removed; otherwise the delete order would decide
    whether a foreign key trips.
    """
    chunks = [text_ids[start : start + 400] for start in range(0, len(text_ids), 400)]
    for statement in ("UPDATE text SET base_id = NULL WHERE id IN", "DELETE FROM text WHERE id IN"):
        for chunk in chunks:
            conn.execute(f"{statement} ({','.join('?' * len(chunk))})", chunk)
    # Blobs are per-page, so removing a page usually empties whole blobs.
    conn.execute(
        "DELETE FROM blob WHERE id NOT IN "
        "(SELECT blob_id FROM text WHERE blob_id IS NOT NULL)"
    )
