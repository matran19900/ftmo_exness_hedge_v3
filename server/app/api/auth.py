"""POST /api/auth/login — exchange username/password for a JWT access token."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import create_access_token, verify_password
from app.config import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

INVALID_CREDS_DETAIL = "Invalid credentials"


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


@router.post("/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginResponse:
    if request.username != settings.admin_username:
        raise HTTPException(status_code=401, detail=INVALID_CREDS_DETAIL)
    if not verify_password(request.password, settings.admin_password_hash):
        raise HTTPException(status_code=401, detail=INVALID_CREDS_DETAIL)
    token = create_access_token(
        subject=request.username,
        secret=settings.jwt_secret,
        expires_minutes=settings.jwt_expires_minutes,
    )
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expires_minutes * 60,
    )
