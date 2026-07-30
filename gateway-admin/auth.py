"""Admin authentication: bcrypt passwords + JWT sessions.

Admin accounts live in Redis as admin:{username} Hash. JWT guards all
/api routes except /api/login. Token API tokens (MCP client auth) are a
separate concern (tokens.py) - do not confuse the two.
"""
import os
import time

import bcrypt
import jwt
import structlog

logger = structlog.get_logger()

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_EXPIRES = int(os.environ.get("JWT_EXPIRES", "86400"))
JWT_ALGO = "HS256"


def hash_password(password: str) -> str:
    """bcrypt hash a password. Returns utf-8 str."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def create_jwt(subject: str) -> str:
    """Issue a JWT for an admin subject. Expires in JWT_EXPIRES seconds."""
    now = int(time.time())
    return jwt.encode(
        {"sub": subject, "iat": now, "exp": now + JWT_EXPIRES},
        JWT_SECRET,
        algorithm=JWT_ALGO,
    )


def decode_jwt(token: str) -> str | None:
    """Verify a JWT and return its subject, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def mask_token(token: str) -> str:
    """Mask a token for list/detail display: first 8 chars + ****."""
    if len(token) <= 8:
        return "****"
    return token[:8] + "****"


async def ensure_default_admin() -> None:
    """Create a default admin:admin account if none exists.

    NOTE: first-run bootstrap only. Change password immediately in prod.
    """
    from redis_client import get_redis
    r = get_redis()
    if not await r.exists("admin:admin"):
        await r.hset("admin:admin", mapping={
            "password_hash": hash_password("admin123"),
            "role": "admin",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        logger.warning("default_admin_created", service="gateway-admin",
                       note="change password immediately")
