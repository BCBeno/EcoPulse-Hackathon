import asyncio
import copy
import time
from collections import OrderedDict

import httpx

from app.core.config import Settings


class ProviderError(Exception):
    def __init__(self, code="unavailable"):
        self.code = code
        super().__init__(code)


class ProviderHTTP:
    def __init__(self, settings: Settings, transport=None):
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            transport=transport,
            follow_redirects=False,
        )
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.geocode_lock = asyncio.Lock()
        self.last_geocode = 0.0

    async def close(self):
        await self.client.aclose()

    async def get_json(self, url, params=None, headers=None, ttl=600):
        # Cache is per adapter session and never stores authentication material in keys.
        key = (url, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items())))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        for attempt in range(3):
            try:
                response = await self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError:
                if attempt < 2:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                raise ProviderError("network_error") from None
            if response.status_code in (401, 403):
                raise ProviderError("authentication_rejected")
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                try:
                    delay = float(response.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    delay = 2**attempt
                if delay > 10:
                    raise ProviderError("rate_limited")
                await asyncio.sleep(max(0.1, delay))
                continue
            if response.status_code >= 400:
                raise ProviderError(f"upstream_http_{response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise ProviderError("malformed_json") from None
            self.cache[key] = (time.monotonic() + ttl, copy.deepcopy(data))
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return data
        raise ProviderError("retry_exhausted")
