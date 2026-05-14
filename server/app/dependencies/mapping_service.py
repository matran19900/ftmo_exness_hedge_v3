"""FastAPI dependency: ``MappingService`` from ``app.state``.

Constructed once during lifespan startup and stashed on ``app.state``;
consumed via ``Depends(get_mapping_service)``.
"""

from __future__ import annotations

from fastapi import Request

from app.services.mapping_service import MappingService


def get_mapping_service(request: Request) -> MappingService:
    """Return the singleton service attached to the FastAPI app state."""
    svc: MappingService = request.app.state.mapping_service
    return svc
