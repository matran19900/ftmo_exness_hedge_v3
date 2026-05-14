"""FastAPI dependency: ``MappingCacheRepository`` from ``app.state``.

The repository is constructed once during lifespan startup
(``server/app/main.py``) and stashed on ``app.state``. Routes consume
it via ``Depends(get_mapping_cache_repository)``.

No route handler uses this getter yet — wired in step 4.A.4 (mapping
cache API endpoints).
"""

from __future__ import annotations

from fastapi import Request

from app.services.mapping_cache_repository import MappingCacheRepository


def get_mapping_cache_repository(request: Request) -> MappingCacheRepository:
    """Return the singleton repository attached to the FastAPI app state."""
    repo: MappingCacheRepository = request.app.state.mapping_cache_repository
    return repo
