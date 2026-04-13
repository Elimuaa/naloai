from fastapi import APIRouter, HTTPException, Depends, Response, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from database import get_db, User, AsyncSession
from auth import (hash_password, verify_password, create_access_token,
                  create_refresh_token, decode_refresh_token, get_current_user)
import jwt
import os
import nacl.signing
import base64


def _is_admin(email: str) -> bool:
    admin_email = os.getenv("ADMIN_EMAIL", "").lower().strip()
    return bool(admin_email and email.lower().strip() == admin_email)


def generate_ed25519_keypair():
    signing_key = nacl.signing.SigningKey.generate()
    private_b64 = base64.b64encode(bytes(signing_key)).decode()
    public_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode()
    return private_b64, public_b64


async def ensure_keypair(user: User, db: AsyncSession):
    """Backfill key pair for users created before this feature."""
    if not user.ed25519_private_key:
        priv, pub = generate_ed25519_keypair()
        await db.execute(
            update(User).where(User.id == user.id).values(
                ed25519_private_key=priv, ed25519_public_key=pub
            )
        )
        await db.commit()
        user.ed25519_private_key = priv
        user.ed25519_public_key = pub

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def set_refresh_cookie(response: Response, token: str):
    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=False,  # Set True in production with HTTPS
        samesite="lax",
        max_age=30 * 24 * 60 * 60,
        path="/"
    )


@router.post("/signup")
async def signup(data: SignupRequest, response: Response, db: AsyncSession = Depends(get_db)):
    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    email = data.email.lower().strip()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    priv, pub = generate_ed25519_keypair()
    user = User(email=email, hashed_password=hash_password(data.password),
                ed25519_private_key=priv, ed25519_public_key=pub)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    set_refresh_cookie(response, refresh_token)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "is_admin": _is_admin(user.email)}
    }


@router.post("/login")
async def login(data: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    email = data.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password")

    await ensure_keypair(user, db)
    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    set_refresh_cookie(response, refresh_token)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "is_admin": _is_admin(user.email)}
    }


@router.post("/refresh")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = decode_refresh_token(token)
        user_id = payload.get("sub")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid refresh token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(401, "User not found")

    new_access = create_access_token(user.id)
    new_refresh = create_refresh_token(user.id)
    set_refresh_cookie(response, new_refresh)
    return {
        "access_token": new_access,
        "user": {"id": user.id, "email": user.email, "is_admin": _is_admin(user.email)}
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "has_api_keys": bool(current_user.rh_api_key),
        "bot_active": current_user.bot_active,
        "trading_symbol": current_user.trading_symbol,
        "is_admin": _is_admin(current_user.email),
    }
