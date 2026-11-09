"""Thin client for the self-hosted GraphHopper (flexible mode: custom models per request)."""

import itertools
import math
from dataclasses import dataclass

import httpx

from app.errors import AppError

PROFILES = {"walk": "foot", "bike": "bike"}


@dataclass
class GhPath:
    distance_m: float
    duration_s: float
    coordinates: list[tuple[float, float]]  # (lon, lat)
    segment_s: list[float]  # travel time of each segment between consecutive coordinates


def routing_unavailable() -> AppError:
    return AppError(503, "routing_unavailable", "Calculul traseelor nu e disponibil acum. Încearcă din nou.")


def no_route() -> AppError:
    return AppError(422, "no_route", "Nu am găsit un traseu între aceste puncte.")


async def route(
    client: httpx.AsyncClient,
    base_url: str,
    mode: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    custom_model: dict | None = None,
    alternatives: bool = False,
) -> list[GhPath]:
    """`origin`/`destination` are (lat, lon). Returns up to 3 paths, best first."""
    body: dict = {
        "points": [[origin[1], origin[0]], [destination[1], destination[0]]],
        "profile": PROFILES[mode],
        "ch.disable": True,
        "points_encoded": False,
        "instructions": False,
        "details": ["time"],
    }
    if custom_model:
        body["custom_model"] = custom_model
    if alternatives:
        body |= {"algorithm": "alternative_route", "alternative_route.max_paths": 3}
    try:
        response = await client.post(f"{base_url.rstrip('/')}/route", json=body, timeout=20)
    except httpx.HTTPError:
        raise routing_unavailable() from None
    if response.status_code == 400:
        raise no_route()
    if response.status_code >= 300:
        raise routing_unavailable()
    return [parse_path(path) for path in response.json().get("paths", [])]


def parse_path(path: dict) -> GhPath:
    coordinates = [(float(lon), float(lat)) for lon, lat, *_ in path["points"]["coordinates"]]
    lengths = [_length_m(a, b) for a, b in itertools.pairwise(coordinates)]
    segment_s = [0.0] * len(lengths)
    intervals = path.get("details", {}).get("time") or [[0, len(lengths), path["time"]]]
    for start, end, millis in intervals:
        span = lengths[start:end]
        total = sum(span)
        for offset, length in enumerate(span):
            share = length / total if total > 0 else 1 / len(span)
            segment_s[start + offset] += millis / 1000 * share
    return GhPath(
        distance_m=float(path["distance"]),
        duration_s=float(path["time"]) / 1000,
        coordinates=coordinates,
        segment_s=segment_s,
    )


def _length_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    y = math.radians(b[1] - a[1])
    return 6_371_000 * math.hypot(x, y)
