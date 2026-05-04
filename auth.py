import os
import logging
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
import jwt
from database import get_db, User, AsyncSession
from sqlalchemy import select

_auth_logger = logging.getLogger(__name__)


def _get_or_create_secret(env_var: str) -> str:
    """Load secret from env; if missing, generate once and persist to .env so it survives restarts."""
    val = os.getenv(env_var)
    if val:
        return val
    new_val = secrets.token_hex(32)
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, "a") as f:
            f.write(f"\n{env_var}={new_val}\n")
        os.environ[env_var] = new_val
        _auth_logger.info(f"Generated and persisted new {env_var} to {env_path}")
    except Exception as e:
        # CRITICAL: if we can't persist, every restart invalidates ALL existing JWTs.
        # This is a security/UX regression — log loud so ops sees it.
        _auth_logger.error(
            f"Could not persist {env_var} to .env ({e}). "
            f"Generated in-memory only — all sessions will be invalidated on next restart. "
            f"Set {env_var} in your environment to fix permanently."
        )
        os.environ[env_var] = new_val
    return new_val

SECRET_KEY = _get_or_create_secret("JWT_SECRET_KEY")
REFRESH_SECRET = _get_or_create_secret("JWT_REFRESH_SECRET")
ALGORITHM = "HS256"
ACCESS_EXPIRE_MINUTES = 480  # 8 hours — avoids frequent WS disconnects
REFRESH_EXPIRE_DAYS = 30

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return password_hash.verify(plain, hashed)


def create_access_token(user_id: str) -> str:
    return jwt.encode({
        "sub": user_id, "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    return jwt.encode({
        "sub": user_id, "type": "refresh",
        "jti": secrets.token_hex(16),
        "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE_DAYS),
    }, REFRESH_SECRET, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def decode_refresh_token(token: str) -> dict:
    payload = jwt.decode(token, REFRESH_SECRET, algorithms=[ALGORITHM])
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("Not a refresh token")
    return payload


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_user_ws(token: str, db: AsyncSession) -> User:
    """For WebSocket auth via query param"""
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
    except jwt.InvalidTokenError:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
