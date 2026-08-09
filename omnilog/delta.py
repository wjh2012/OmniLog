"""Line-level delta codec for revision bodies.

Wiki edits touch a handful of lines in a body that is otherwise unchanged, so a
delta against a nearby revision is far smaller than the body itself — even
after both are gzipped.

The format is deliberately plain text before compression, since gzip is applied
on top anyway and a readable encoding is worth more than a few saved bytes:

    D1\\n                 header
    C <start> <count>\\n  copy `count` lines from the base, starting at `start`
    I <nbytes>\\n<bytes>  insert `nbytes` of literal UTF-8

Lines keep their terminators, so reassembly is a plain join and a body with no
trailing newline round-trips unchanged.
"""

from __future__ import annotations

import difflib

_HEADER = b"D1\n"


class DeltaError(ValueError):
    """A delta could not be decoded, or its base does not match."""


def make_delta(base: str, target: str) -> bytes:
    """Encode `target` as a set of edits against `base`."""
    base_lines = base.splitlines(keepends=True)
    target_lines = target.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, base_lines, target_lines, autojunk=False)

    parts = [_HEADER]
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(b"C %d %d\n" % (i1, i2 - i1))
        elif tag in ("replace", "insert"):
            chunk = "".join(target_lines[j1:j2]).encode("utf-8")
            parts.append(b"I %d\n" % len(chunk))
            parts.append(chunk)
        # 'delete' needs no instruction: the copy simply skips those lines.
    return b"".join(parts)


def apply_delta(base: str, delta: bytes) -> str:
    """Rebuild the body that `delta` encodes against `base`."""
    if not delta.startswith(_HEADER):
        raise DeltaError("not a delta payload")

    base_lines = base.splitlines(keepends=True)
    out: list[str] = []
    pos = len(_HEADER)

    while pos < len(delta):
        end = delta.find(b"\n", pos)
        if end == -1:
            raise DeltaError("truncated instruction")
        try:
            kind, _, argument = delta[pos:end].decode("ascii").partition(" ")
        except UnicodeDecodeError as exc:  # pragma: no cover - corrupt payload
            raise DeltaError("undecodable instruction") from exc
        pos = end + 1

        if kind == "C":
            try:
                start, count = (int(value) for value in argument.split())
            except ValueError as exc:
                raise DeltaError(f"bad copy instruction {argument!r}") from exc
            if start < 0 or start + count > len(base_lines):
                raise DeltaError("copy runs past the end of the base")
            out.extend(base_lines[start : start + count])
        elif kind == "I":
            try:
                length = int(argument)
            except ValueError as exc:
                raise DeltaError(f"bad insert instruction {argument!r}") from exc
            if pos + length > len(delta):
                raise DeltaError("insert runs past the end of the delta")
            out.append(delta[pos : pos + length].decode("utf-8"))
            pos += length
        else:
            raise DeltaError(f"unknown instruction {kind!r}")

    return "".join(out)
