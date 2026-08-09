from __future__ import annotations

import unicodedata

import pytest

from omnilog.errors import InvalidSlug
from omnilog.slug import slugify


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  spaced   out  ", "spaced-out"),
        ("Slash/Separated", "slash-separated"),
        ("한국어 제목", "한국어-제목"),
        ("Mixed 한글 and ASCII", "mixed-한글-and-ascii"),
        ("dots.are.kept", "dots.are.kept"),
        ("!!!weird---punctuation???", "weird-punctuation"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert slugify(title) == expected


def test_slugify_normalises_decomposed_hangul() -> None:
    """macOS hands over NFD; it has to land on the same slug as NFC."""
    nfc = "한국어"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    assert slugify(nfd) == slugify(nfc)


def test_slugify_rejects_unusable_titles() -> None:
    with pytest.raises(InvalidSlug):
        slugify("!!!")


def test_slugify_lenient_mode_returns_empty() -> None:
    assert slugify("!!!", strict=False) == ""
