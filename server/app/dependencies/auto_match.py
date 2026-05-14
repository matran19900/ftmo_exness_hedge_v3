"""FastAPI dependency: ``AutoMatchEngine`` from ``app.state``.

The engine is constructed once during lifespan startup
(``server/app/main.py``) and stashed on ``app.state``. Routes consume it
via ``Depends(get_auto_match_engine)``.

No route uses this getter yet — wired in step 4.A.4
(``POST /api/accounts/exness/{}/symbol-mapping/auto-match``).
"""

from __future__ import annotations

from fastapi import Request

from app.services.auto_match_engine import AutoMatchEngine


def get_auto_match_engine(request: Request) -> AutoMatchEngine:
    """Return the singleton engine attached to the FastAPI app state."""
    engine: AutoMatchEngine = request.app.state.auto_match_engine
    return engine
