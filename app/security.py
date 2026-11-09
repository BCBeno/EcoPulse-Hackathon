"""Device tokens, the admin key, client IP resolution and in-process rate limits."""

import hashlib
import hmac
import secrets
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from time import monotonic
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.devices.models import Device
from app.errors import AppError

LAST_SEEN_RESOLUTION = timedelta(hours=1)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _device_from_header(session: AsyncSession, authorization: str | None) -> Device | None:
    token = _bearer(authorization)
    if token is None:
        return None
    device = await session.scalar(select(Device).where(Device.token_hash == hash_token(token)))
    if device is None:
        raise AppError(
            401, "unauthorized", "Sesiunea nu mai este validă.", headers={"WWW-Authenticate": "Bearer"}
        )
    now = datetime.now(UTC)
    if now - device.last_seen_at > LAST_SEEN_RESOLUTION:
        device.last_seen_at = now
        await session.commit()
    return device


async def optional_device(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> Device | None:
    return await _device_from_header(session, authorization)


async def current_device(device: Annotated[Device | None, Depends(optional_device)]) -> Device:
    if device is None:
        raise AppError(401, "unauthorized", "Autentificare necesară.", headers={"WWW-Authenticate": "Bearer"})
    return device


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.admin_api_key.strip()
    if not expected:
        # Closed by default: no key configured means no admin access at all.
        raise AppError(503, "admin_disabled", "Accesul de administrare nu este configurat.")
    if not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), expected.encode()):
        raise AppError(403, "forbidden", "Cheie de administrare invalidă.")


def client_ip(request: Request, trusted_proxies: list[str]) -> str:
    """The caller's IP. Forwarding headers count only when the direct peer is a trusted proxy."""
    networks = [ip_network(item, strict=False) for item in trusted_proxies]

    def trusted(value: str) -> bool:
        try:
            address = ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in networks)

    peer = request.client.host if request.client else "unknown"
    if not trusted(peer):
        return peer
    cloudflare = request.headers.get("cf-connecting-ip", "").strip()
    if cloudflare:
        return cloudflare
    hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
    for hop in reversed(hops):
        if not trusted(hop):
            return hop
    return hops[0] if hops else peer


class RateLimiter:
    """Sliding-window limiter kept in memory; valid because the API runs as a single process."""

    def __init__(self, max_keys: int = 50_000):
        self.buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self.max_keys = max_keys

    def hit(self, key: str, limit: int, window_s: float) -> float | None:
        """Record a hit. Returns None when allowed, else the seconds to wait."""
        now = monotonic()
        bucket = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        while bucket and bucket[0] <= now - window_s:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(1.0, bucket[0] + window_s - now)
        bucket.append(now)
        while len(self.buckets) > self.max_keys:
            self.buckets.popitem(last=False)
        return None

    def check(self, key: str, limit: int, window_s: float) -> None:
        wait = self.hit(key, limit, window_s)
        if wait is not None:
            raise AppError(
                429,
                "rate_limited",
                "Prea multe cereri. Încearcă din nou puțin mai târziu.",
                headers={"Retry-After": str(int(wait) + 1)},
            )


def limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


def limit_ip(name: str, setting: str, window_s: float):
    """Dependency: limit per client IP, with the limit read from a Settings attribute."""

    async def dependency(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
        ip = client_ip(request, settings.trusted_proxies)
        limiter(request).check(f"{name}:ip:{ip}", getattr(settings, setting), window_s)

    return dependency


def limit_device(name: str, setting: str, window_s: float):
    """Dependency: limit per authenticated device."""

    async def dependency(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        device: Annotated[Device, Depends(current_device)],
    ) -> None:
        limiter(request).check(f"{name}:device:{device.id}", getattr(settings, setting), window_s)

    return dependency


read_limit = limit_ip("read", "reads_per_minute_per_ip", 60)
write_limit = limit_device("write", "writes_per_minute_per_device", 60)
