"""FastAPI dependency: ``MappingCacheService`` from ``app.state``.

Constructed once during lifespan startup and stashed on ``app.state``;
consumed via ``Depends(get_mapping_cache_service)``.
"""

from __future__ import annotations

from fastapi import Request

from app.services.mapping_cache_service import MappingCacheService


def get_mapping_cache_service(request: Request) -> MappingCacheService:
    """Return the singleton service attached to the FastAPI app state."""
    svc: MappingCacheService = request.app.state.mapping_cache_service
    return svc
