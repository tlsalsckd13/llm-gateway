from fastapi import APIRouter


page_router = APIRouter(prefix="/portal/kb", tags=["portal-kb"])
api_router = APIRouter(prefix="/api/portal/me/kb", tags=["portal-kb-api"])

__all__ = ["page_router", "api_router"]
