"""Main API router."""

from fastapi import APIRouter

from src.api.auth import router as auth_router
from src.api.media import router as media_router
from src.api.user import router as user_router

api_router = APIRouter(prefix="/api")

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(user_router, prefix="/user", tags=["user"])
