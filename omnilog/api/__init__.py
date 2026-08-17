"""HTTP routes."""

from fastapi import APIRouter

from . import citations, maintenance, pages, redirects, search, sources

api_router = APIRouter()
api_router.include_router(pages.router)
api_router.include_router(redirects.router)
api_router.include_router(sources.router)
api_router.include_router(citations.router)
api_router.include_router(search.router)
api_router.include_router(maintenance.router)

__all__ = ["api_router"]
