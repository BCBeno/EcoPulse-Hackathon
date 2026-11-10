"""HERE Traffic API v7 flow: real-time speed and congestion on the main roads of the city.

One request covers the whole city bbox (`in=bbox:west,south,east,north`, max 1° x 1°).
Speeds come in m/s; jamFactor goes from 0 (free flow) to 10 (closed road).
confidence > 0.7 means real-time data, 0.5-0.7 historical, below that only the speed limit.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.config import Bounds
from app.http import SourceError, request_json

URL = "https://data.traffic.hereapi.com/v7/flow"
MS_TO_KMH = 3.6


@dataclass(frozen=True)
class FlowSegment:
    coordinates: list[list[float]]  # [lon, lat] pairs
    description: str | None
    functional_class: int | None
    length_m: float
    jam_factor: float
    speed_kmh: float | None
    free_flow_kmh: float | None
    confidence: float | None
    closed: bool


async def fetch(
    client: httpx.AsyncClient, api_key: str, bounds: Bounds
) -> tuple[datetime, list[FlowSegment]]:
    params = {
        "in": f"bbox:{bounds.west},{bounds.south},{bounds.east},{bounds.north}",
        "locationReferencing": "shape",
        "apiKey": api_key,
    }
    data = await request_json(client, "GET", URL, params=params)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise SourceError("unexpected_schema")
    return parse(data)


def _kmh(value) -> float | None:
    try:
        return round(float(value) * MS_TO_KMH, 1)
    except (TypeError, ValueError):
        return None


def parse(data: dict) -> tuple[datetime, list[FlowSegment]]:
    try:
        updated = datetime.fromisoformat(str(data.get("sourceUpdated")).replace("Z", "+00:00"))
    except ValueError:
        updated = datetime.now(UTC)
    segments: list[FlowSegment] = []
    for result in data.get("results", []):
        location, flow = result.get("location") or {}, result.get("currentFlow") or {}
        try:
            jam = float(flow["jamFactor"])
        except (KeyError, TypeError, ValueError):
            continue
        closed = flow.get("traversability") == "closed" or jam >= 10
        for link in (location.get("shape") or {}).get("links") or []:
            points = [[p["lng"], p["lat"]] for p in link.get("points") or [] if "lat" in p and "lng" in p]
            if len(points) < 2:
                continue
            segments.append(
                FlowSegment(
                    coordinates=points,
                    description=location.get("description"),
                    functional_class=link.get("functionalClass"),
                    length_m=float(link.get("length") or 0),
                    jam_factor=min(max(jam, 0.0), 10.0),
                    speed_kmh=_kmh(flow.get("speed")),
                    free_flow_kmh=_kmh(flow.get("freeFlow")),
                    confidence=flow.get("confidence"),
                    closed=closed,
                )
            )
    return updated, segments
