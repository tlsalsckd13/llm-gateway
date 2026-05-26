from fastapi import APIRouter


page_router = APIRouter(prefix="/portal/skills", tags=["portal-skills"])
api_router = APIRouter(prefix="/api/portal/me/skills", tags=["portal-skills-api"])

__all__ = ["page_router", "api_router"]
