"""Password hashing, JWT issuing/verification, and a sliding-window rate limiter."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

import bcrypt
import jwt

from .config import settings

ALGORITHM = "HS256"

# bcrypt silently truncates at 72 bytes. Truncating is worse than rejecting:
# a 100-character password and its first 72 characters would collide.
BCRYPT_MAX_BYTES = 72


class PasswordTooLongError(ValueError):
    """Raised when a password exceeds bcrypt's 72-byte input limit."""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(
            f"password is {len(encoded)} bytes; bcrypt accepts at most {BCRYPT_MAX_BYTES}"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        encoded = password.encode("utf-8")
        if len(encoded) > BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # A malformed stored hash must read as "wrong password", not crash.
        return False


def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.signing_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Return the claims, or None for any invalid/expired token."""
    try:
        return jwt.decode(token, settings.signing_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


class RateLimiter:
    """Fixed-window limiter, in-process.

    Deliberately simple: this is per-worker, so with N workers the effective
    limit is N times the configured value. That is stated here rather than
    hidden, and the fix (a shared store) is in ARCHITECTURE.md.
    """

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Return (allowed, remaining)."""
        current = now if now is not None else time.monotonic()
        cutoff = current - self.window
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False, 0
            hits.append(current)
            self._hits[key] = hits
            return True, self.limit - len(hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


auth_limiter = RateLimiter(settings.auth_rate_limit)
write_limiter = RateLimiter(settings.write_rate_limit)
