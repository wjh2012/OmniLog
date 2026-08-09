"""HTTP routes."""

from fastapi import APIRouter

from . import maintenance, pages, redirects, search

api_router = APIRouter()
api_router.include_router(pages.router)
api_router.include_router(redirects.router)
api_router.include_router(search.router)
api_router.include_router(maintenance.router)

__all__ = ["api_router"]
