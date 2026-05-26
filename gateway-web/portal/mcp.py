from fastapi import APIRouter


page_router = APIRouter(prefix="/portal/mcp", tags=["portal-mcp"])
api_router = APIRouter(prefix="/api/portal/me/mcp", tags=["portal-mcp-api"])

__all__ = ["page_router", "api_router"]
