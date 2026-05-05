"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "service": "ftmo-hedge-server", "version": "0.1.0"}
