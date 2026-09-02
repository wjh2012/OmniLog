"""Runtime configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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
    #: API key -> roles it holds (e.g. {"sk-admin": ["viewer", "editor", "admin"]}).
    #: Empty means auth is off: every caller gets every role. See omnilog.auth.
    api_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Which omnilog.embeddings provider to use. Swapping this (plus the model
    #: fields below) is the whole story for moving from a hosted model to a
    #: local one -- see omnilog/embeddings/__init__.py.
    embedding_provider: str = "openai"
    embedding_model_id: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    #: Empty means the provider's own default (OpenAI's public endpoint for
    #: the "openai" provider). Point this at a local server to go local.
    embedding_base_url: str = ""
    #: Empty means the provider falls back to its usual env var
    #: (OPENAI_API_KEY, for "openai").
    embedding_api_key: str = ""


def load_settings() -> Settings:
    """Build settings from the environment."""
    db_path = Path(os.environ.get("OMNILOG_DB", DEFAULT_DB_FILENAME)).expanduser()
    return Settings(
        db_path=db_path.resolve(),
        api_keys=_load_api_keys(),
        embedding_provider=os.environ.get("OMNILOG_EMBEDDING_PROVIDER", "openai"),
        embedding_model_id=os.environ.get(
            "OMNILOG_EMBEDDING_MODEL", "text-embedding-3-small"
        ),
        embedding_dimensions=int(os.environ.get("OMNILOG_EMBEDDING_DIMENSIONS", "1536")),
        embedding_base_url=os.environ.get("OMNILOG_EMBEDDING_BASE_URL", ""),
        embedding_api_key=os.environ.get("OMNILOG_EMBEDDING_API_KEY", ""),
    )


def _load_api_keys() -> dict[str, tuple[str, ...]]:
    """Parse OMNILOG_API_KEYS, a JSON object mapping API key -> list of roles."""
    raw = os.environ.get("OMNILOG_API_KEYS")
    if not raw:
        return {}
    parsed = json.loads(raw)
    return {key: tuple(roles) for key, roles in parsed.items()}
