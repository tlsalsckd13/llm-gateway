from fastapi import APIRouter


page_router = APIRouter(prefix="/admin/guardrails", tags=["admin-guardrails"])
api_router = APIRouter(prefix="/api/admin/guardrails", tags=["admin-guardrails-api"])

__all__ = ["page_router", "api_router"]
