"""Main API router."""

from fastapi import APIRouter

from src.api.auth import router as auth_router
from src.api.imports import router as imports_router
from src.api.kobo import router as kobo_router
from src.api.media import router as media_router
from src.api.search import router as search_router
from src.api.stats import router as stats_router
from src.api.user import router as user_router

api_router = APIRouter(prefix="/api")

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(imports_router, prefix="/import", tags=["import"])
api_router.include_router(kobo_router, prefix="/kobo", tags=["kobo"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(search_router, prefix="/search", tags=["search"])
api_router.include_router(stats_router, prefix="/stats", tags=["stats"])
api_router.include_router(user_router, prefix="/user", tags=["user"])
