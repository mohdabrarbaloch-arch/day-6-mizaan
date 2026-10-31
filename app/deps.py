"""Shared FastAPI dependencies: current user, role guards, rate limiting."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .security import auth_limiter, decode_access_token, write_limiter

bearer = HTTPBearer(auto_error=False)

ROLE_RANK = {"viewer": 0, "engineer": 1, "admin": 2}


def client_key(request: Request) -> str:
    """Rate-limit key. Prefers the proxy header when present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_access_token(credentials.credentials)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = int(claims.get("sub", ""))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token subject"
        ) from None

    user = db.get(User, user_id)
    if user is None:
        # Token valid but the account is gone — treat as unauthenticated.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(minimum: str) -> Callable[..., User]:
    """Dependency factory: require at least `minimum` role."""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {minimum} role or higher",
            )
        return user

    return _guard


def limit_auth(request: Request) -> None:
    allowed, remaining = auth_limiter.check(client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts; try again in a minute",
            headers={"Retry-After": "60"},
        )
    request.state.rate_limit_remaining = remaining


def limit_write(request: Request) -> None:
    allowed, remaining = write_limiter.check(client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Write rate limit exceeded; try again in a minute",
            headers={"Retry-After": "60"},
        )
    request.state.rate_limit_remaining = remaining
