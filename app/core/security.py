from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

password_hash = PasswordHash.recommended()


def create_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
            "iss": "ecopulse",
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def decode_token(token: str) -> str:
    claims = jwt.decode(
        token,
        get_settings().jwt_secret_key,
        algorithms=["HS256"],
        issuer="ecopulse",
        options={"require": ["sub", "exp", "iat"]},
    )
    return claims["sub"]
