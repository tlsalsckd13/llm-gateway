from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/policies", tags=["admin-policies"])
api_router = APIRouter(prefix="/api/admin/policies", tags=["admin-policies-api"])

__all__ = ["page_router", "api_router"]
