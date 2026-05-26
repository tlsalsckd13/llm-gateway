from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/eval", tags=["admin-eval"])
api_router = APIRouter(prefix="/api/admin/eval", tags=["admin-eval-api"])

__all__ = ["page_router", "api_router"]
