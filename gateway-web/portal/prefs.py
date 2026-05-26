from fastapi import APIRouter


api_router = APIRouter(prefix="/api/portal/me/prefs", tags=["portal-prefs-api"])

__all__ = ["api_router"]
