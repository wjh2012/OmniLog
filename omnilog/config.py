"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_FILENAME = "omnilog.db"


@dataclass(frozen=True)
class Settings:
    """Values that differ between a dev box, a test run and a deployment."""

    db_path: Path
    #: Revision bodies at least this large are gzipped before hitting the text store.
    compress_min_bytes: int = 512
    #: Prefix used when a [[wikilink]] is turned into an href.
    link_base: str = "/pages"
    #: Prefix used when a registered source is turned into an href.
    source_base: str = "/sources"
    #: Upper bound on one revision body, in bytes of UTF-8.
    max_content_bytes: int = 2 * 1024 * 1024
    #: Upper bound on one uploaded source file. Files live in the database, so
    #: this is the knob that keeps the database file from becoming an archive.
    max_file_bytes: int = 10 * 1024 * 1024


def load_settings() -> Settings:
    """Build settings from the environment."""
    db_path = Path(os.environ.get("OMNILOG_DB", DEFAULT_DB_FILENAME)).expanduser()
    return Settings(db_path=db_path.resolve())
