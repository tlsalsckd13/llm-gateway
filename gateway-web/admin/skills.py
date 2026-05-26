from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/skills", tags=["admin-skills"])
api_router = APIRouter(prefix="/api/admin/skills", tags=["admin-skills-api"])

__all__ = ["page_router", "api_router"]
