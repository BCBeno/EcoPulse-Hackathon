"""Place search: a curated list of Iași places first, then Photon (OSM) for streets and addresses.

Reverse geocoding uses Nominatim, at most one request per second (its usage policy), with a cache.
Nominatim must not be used for search-as-you-type, which is why search goes to Photon.
"""

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Bounds
from app.geo import LatLon
from app.http import SourceError, request_json
from app.places.models import PlaceCache

PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
CACHE_TTL = timedelta(days=30)
GAZETTEER = json.loads((Path(__file__).parent / "gazetteer.json").read_text(encoding="utf-8"))

Kind = Literal["neighbourhood", "landmark", "street", "address", "poi"]


class Place(BaseModel):
    id: str
    name: str
    label: str
    kind: Kind
    location: LatLon


def normalize(text: str) -> str:
    text = text.lower().replace("ş", "ș").replace("ţ", "ț")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def _local(entry: dict) -> Place:
    return Place(
        id=f"local:{entry['id']}",
        name=entry["name"],
        label=f"{entry['name']}, Iași",
        kind=entry["kind"],
        location=LatLon(lat=entry["lat"], lon=entry["lon"]),
    )


def local_search(query: str, limit: int = 5) -> list[Place]:
    q = normalize(query)
    if not q:
        return []
    scored = []
    for entry in GAZETTEER:
        names = [normalize(entry["name"]), *(normalize(a) for a in entry["aliases"])]
        if q in names:
            score = 0
        elif any(name.startswith(q) for name in names):
            score = 1
        elif any(f" {q}" in f" {name}" for name in names):
            score = 2
        else:
            continue
        scored.append((score, entry["name"], entry))
    return [_local(entry) for _, _, entry in sorted(scored, key=lambda item: item[:2])[:limit]]


def parse_photon(data: dict, bounds: Bounds) -> list[Place]:
    places: list[Place] = []
    seen: set[str] = set()
    for feature in data.get("features") or []:
        props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
        try:
            lon, lat = (float(v) for v in geometry["coordinates"][:2])
        except (KeyError, TypeError, ValueError):
            continue
        if not bounds.contains(lat, lon):
            continue
        kind = _photon_kind(props)
        street = props.get("street")
        name = props.get("name") or (f"{street} {props.get('housenumber', '')}".strip() if street else None)
        if not name:
            continue
        area = props.get("district") or props.get("locality")
        parts = [name]
        if street and kind in ("poi", "address") and street not in name:
            parts.append(street)
        if area and area not in parts:
            parts.append(area)
        parts.append("Iași")
        label = ", ".join(parts)
        if label in seen:
            continue
        seen.add(label)
        place_id = f"osm:{props.get('osm_type', 'X')}{props.get('osm_id', len(places))}"
        places.append(
            Place(id=place_id, name=name, label=label, kind=kind, location=LatLon(lat=lat, lon=lon))
        )
    return places


def _photon_kind(props: dict) -> Kind:
    kind = props.get("type")
    if kind == "street":
        return "street"
    if kind == "house":
        return "address" if not props.get("name") else "poi"
    if kind in ("district", "locality", "city"):
        return "neighbourhood"
    return "poi"


def reverse_label(data: dict) -> str | None:
    address = data.get("address") if isinstance(data, dict) else None
    if not address:
        return None
    road = address.get("road") or address.get("pedestrian") or address.get("footway")
    area = address.get("suburb") or address.get("neighbourhood") or address.get("quarter")
    parts = [p for p in (road, area) if p]
    return ", ".join(parts) if parts else data.get("name") or None


@dataclass
class Throttle:
    """Serialises calls and keeps at least `interval` seconds between them."""

    interval: float
    last: float = 0.0
    lock: asyncio.Lock | None = None

    async def wait(self) -> None:
        self.lock = self.lock or asyncio.Lock()
        async with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


NOMINATIM_THROTTLE = Throttle(interval=1.1)


async def _cached(session: AsyncSession, key: str, provider: str):
    row = await session.get(PlaceCache, (key, provider))
    if row is not None and datetime.now(UTC) - row.created_at < CACHE_TTL:
        return row.results
    return None


async def _store(session: AsyncSession, key: str, provider: str, results) -> None:
    statement = insert(PlaceCache).values(
        query=key, provider=provider, results=results, created_at=datetime.now(UTC)
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[PlaceCache.query, PlaceCache.provider],
            set_={"results": statement.excluded.results, "created_at": statement.excluded.created_at},
        )
    )
    await session.commit()


async def search(
    session: AsyncSession,
    client: httpx.AsyncClient,
    bounds: Bounds,
    query: str,
    near: LatLon | None = None,
    limit: int = 6,
) -> list[Place]:
    local = local_search(query, limit)
    if len(local) >= limit or len(normalize(query)) < 3:
        return local
    key = normalize(query)
    remote = await _cached(session, key, "photon")
    if remote is None:
        params = {
            "q": query,
            "limit": 10,
            "bbox": f"{bounds.west},{bounds.south},{bounds.east},{bounds.north}",
            "lat": near.lat if near else (bounds.south + bounds.north) / 2,
            "lon": near.lon if near else (bounds.west + bounds.east) / 2,
        }
        try:
            data = await request_json(client, "GET", PHOTON_URL, params=params, attempts=2)
        except SourceError:
            return local  # search keeps working on the curated list when Photon is down
        remote = [p.model_dump(mode="json") for p in parse_photon(data, bounds)]
        await _store(session, key, "photon", remote)
    names = {p.name for p in local}
    extra = [Place.model_validate(p) for p in remote if p["name"] not in names]
    return (local + extra)[:limit]


async def reverse(session: AsyncSession, client: httpx.AsyncClient, lat: float, lon: float) -> str | None:
    key = f"{lat:.4f},{lon:.4f}"
    cached = await _cached(session, key, "nominatim_reverse")
    if cached is not None:
        return cached.get("label")
    await NOMINATIM_THROTTLE.wait()
    try:
        data = await request_json(
            client,
            "GET",
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17, "accept-language": "ro"},
            attempts=2,
        )
    except SourceError:
        return None
    label = reverse_label(data)
    await _store(session, key, "nominatim_reverse", {"label": label})
    return label


async def resolve(
    session: AsyncSession, client: httpx.AsyncClient, bounds: Bounds, name: str, near: LatLon | None = None
) -> list[Place]:
    """Candidates for a place name used in conversation; the agent asks when there are several."""
    local = local_search(name, 3)
    if local and normalize(local[0].name) == normalize(name):
        return local[:1]
    found = await search(session, client, bounds, name, near, limit=3)
    return found


async def cached_label(session: AsyncSession, lat: float, lon: float) -> str | None:
    row = await session.scalar(
        select(PlaceCache.results).where(
            PlaceCache.query == f"{lat:.4f},{lon:.4f}", PlaceCache.provider == "nominatim_reverse"
        )
    )
    return row.get("label") if row else None
