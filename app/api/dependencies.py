from collections import OrderedDict, deque
from time import monotonic

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.entities import User

bearer = HTTPBearer(auto_error=False)


async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer), db: AsyncSession = Depends(get_db)
):
    if not credentials:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = decode_token(credentials.credentials)
    except (jwt.InvalidTokenError, KeyError, TypeError):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from None
    user = await db.get(User, user_id)
    if not user or not user.active:
        raise HTTPException(401, "Account unavailable")
    return user


async def admin_user(user=Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Administrator access required")
    return user


class RateLimit:
    """Per-process development limiter. Run one API worker or enforce limits at the gateway."""

    def __init__(self, count=15, seconds=60):
        self.count, self.seconds, self.buckets = count, seconds, OrderedDict()

    async def __call__(self, request: Request):
        key = request.client.host if request.client else "unknown"
        bucket = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        now = monotonic()
        while bucket and bucket[0] <= now - self.seconds:
            bucket.popleft()
        if len(bucket) >= self.count:
            raise HTTPException(
                429, "Too many requests; try again later", headers={"Retry-After": str(self.seconds)}
            )
        bucket.append(now)
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)


auth_limit, write_limit, routing_limit, geocoding_limit = (
    RateLimit(10),
    RateLimit(30),
    RateLimit(20),
    RateLimit(15),
)
