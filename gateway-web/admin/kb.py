from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/kb", tags=["admin-kb"])
api_router = APIRouter(prefix="/api/admin/kb", tags=["admin-kb-api"])

__all__ = ["page_router", "api_router"]
