from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/mcp", tags=["admin-mcp"])
api_router = APIRouter(prefix="/api/admin/mcp", tags=["admin-mcp-api"])

__all__ = ["page_router", "api_router"]
