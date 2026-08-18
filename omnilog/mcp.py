"""Mounts the REST API as MCP tools at /mcp.

Every route becomes a tool (`FastMCP.from_fastapi`); routes tagged
"role-editor"/"role-admin" (see omnilog/api/*.py) additionally require a
token whose scopes include that role, via `restrict_tag`. Token -> scopes
resolution is the same `omnilog.auth.resolve_scopes` the REST dependencies
use, so a caller's access is identical on both surfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier, restrict_tag
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.utilities.lifespan import combine_lifespans

from .auth import ADMIN, EDITOR, INTERNAL_BRIDGE_TOKEN, resolve_scopes

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .config import Settings

EDITOR_TAG = "role-editor"
ADMIN_TAG = "role-admin"


class StaticKeyVerifier(TokenVerifier):
    """Resolves a bearer token to roles via Settings.api_keys."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        scopes = resolve_scopes(self._settings, token)
        if scopes is None:
            return None
        return AccessToken(token=token, client_id=token[:8], scopes=scopes)


def mount_mcp(app: FastAPI, settings: Settings) -> FastMCP:
    """Attach an MCP server exposing `app`'s routes as tools, at /mcp."""
    auth = None
    middleware = []
    if settings.api_keys:
        auth = StaticKeyVerifier(settings)
        middleware.append(
            AuthMiddleware(
                auth=[
                    restrict_tag(EDITOR_TAG, scopes=[EDITOR]),
                    restrict_tag(ADMIN_TAG, scopes=[ADMIN]),
                ]
            )
        )

    mcp = FastMCP.from_fastapi(
        app,
        auth=auth,
        middleware=middleware or None,
        # The REST routes gate on role too (see omnilog/api/*.py); this is
        # the credential for the in-process bridge call FastMCP makes back
        # into them, once restrict_tag has already authorized the real
        # caller for that tool. FastMCP strips Authorization when forwarding
        # the caller's own headers, so without this every write tool would
        # 401 against its own REST route.
        httpx_client_kwargs={
            "headers": {"Authorization": f"Bearer {INTERNAL_BRIDGE_TOKEN}"}
        },
    )
    # `path` is relative to the mount point below, so "/" here + mount("/mcp")
    # lands the endpoint at /mcp, not /mcp/mcp.
    mcp_app = mcp.http_app(path="/")

    app.router.lifespan_context = combine_lifespans(
        app.router.lifespan_context, mcp_app.lifespan
    )
    app.mount("/mcp", mcp_app)
    return mcp
