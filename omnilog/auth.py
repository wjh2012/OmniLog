"""Role resolution shared by the REST API and the MCP tool surface.

Both sides authenticate the same way: a bearer token is looked up in
``Settings.api_keys`` to get the list of roles it holds. ``Settings.api_keys``
empty means auth is switched off (the PoC default) and every caller gets
every role.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

VIEWER = "viewer"
EDITOR = "editor"
ADMIN = "admin"

#: Credential the MCP layer uses on the in-process bridge call it makes back
#: into the REST routes, once its own restrict_tag check has already decided
#: whether the real caller may run that tool. Random per process, never
#: handed to a client, so it can't be used to skip the MCP-side gate.
INTERNAL_BRIDGE_TOKEN = secrets.token_urlsafe(32)


def resolve_scopes(settings: Settings, token: str | None) -> list[str] | None:
    """Roles held by `token`, or None if it isn't a known key."""
    if token == INTERNAL_BRIDGE_TOKEN:
        return [VIEWER, EDITOR, ADMIN]
    if not settings.api_keys:
        return [VIEWER, EDITOR, ADMIN]
    if token is None:
        return None
    roles = settings.api_keys.get(token)
    return list(roles) if roles else None


def has_role(settings: Settings, token: str | None, role: str) -> bool:
    scopes = resolve_scopes(settings, token)
    return scopes is not None and role in scopes
