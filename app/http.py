"""Shared outbound HTTP client with a small, bounded retry policy."""

import asyncio
from typing import Any

import httpx

from app.config import Settings

RETRY_STATUS = {429, 502, 503, 504}


class SourceError(Exception):
    """An external source failed; `code` is short and safe to store and show to admins."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_s,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=False,
    )


async def request_json(
    client: httpx.AsyncClient, method: str, url: str, *, attempts: int = 3, **kwargs: Any
) -> Any:
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            if last:
                raise SourceError("network_error") from None
            await asyncio.sleep(0.5 * 2**attempt)
            continue
        if response.status_code in (401, 403):
            raise SourceError("authentication_rejected")
        if response.status_code in RETRY_STATUS and not last:
            await asyncio.sleep(min(5.0, 0.5 * 2**attempt))
            continue
        if response.status_code >= 400:
            raise SourceError(f"http_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise SourceError("malformed_json") from None
    raise SourceError("retry_exhausted")
