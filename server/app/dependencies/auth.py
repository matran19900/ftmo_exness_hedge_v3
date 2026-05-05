"""FastAPI auth dependencies — REST (Bearer header) and WebSocket (query token)."""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Query, WebSocketException, status

from app.auth import decode_access_token
from app.config import Settings, get_settings


async def get_current_user_rest(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Extract & validate the Bearer token from the Authorization header."""
    if authorization is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = parts[1]
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise HTTPException(status_code=401, detail="Invalid token")
    return sub


async def get_current_user_ws(
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Query()] = None,
) -> str:
    """Extract & validate the JWT from the WebSocket handshake query string."""
    if token is None:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except jwt.ExpiredSignatureError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token"
        ) from exc
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
    return sub
