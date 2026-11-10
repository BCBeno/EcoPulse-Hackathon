"""Traffic levels, storage of the HERE snapshot and the queries the map, routes and agent use."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from geoalchemy2 import Geography
from sqlalchemy import cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.traffic.models import TrafficSegment
from app.traffic.provider import FlowSegment

# Traffic is only used while it is current; older snapshots are ignored.
FRESH_FOR = timedelta(minutes=45)


@dataclass(frozen=True)
class TrafficLevel:
    key: str
    label: str
    color: str
    min_jam: float


LEVELS = (
    TrafficLevel("free", "Fluent", "#3BA55C", 0.0),
    TrafficLevel("moderate", "Moderat", "#E8B300", 3.0),
    TrafficLevel("heavy", "Aglomerat", "#E8590C", 6.0),
    TrafficLevel("jammed", "Blocat", "#C92A2A", 8.0),
    TrafficLevel("closed", "Închis", "#5F0F0F", 10.0),
)
# Congestion that raises pollution near the road (heuristic PM2.5-equivalent, µg/m³) for route scoring.
POLLUTION_BOOST = {"heavy": 3.0, "jammed": 6.0}
NEAR_ROAD_M = 30


def level(jam: float, closed: bool = False) -> TrafficLevel:
    if closed:
        return LEVELS[-1]
    return next(lvl for lvl in reversed(LEVELS[:-1]) if jam >= lvl.min_jam)


async def store(
    session: AsyncSession, observed_at: datetime, segments: list[FlowSegment], now: datetime
) -> int:
    await session.execute(delete(TrafficSegment))
    for start in range(0, len(segments), 500):
        session.add_all(
            TrafficSegment(
                geometry=func.ST_SetSRID(
                    func.ST_GeomFromGeoJSON(json.dumps({"type": "LineString", "coordinates": s.coordinates})),
                    4326,
                ),
                description=s.description[:200] if s.description else None,
                functional_class=s.functional_class,
                length_m=s.length_m,
                jam_factor=s.jam_factor,
                speed_kmh=s.speed_kmh,
                free_flow_kmh=s.free_flow_kmh,
                confidence=s.confidence,
                closed=s.closed,
                observed_at=observed_at,
                retrieved_at=now,
            )
            for s in segments[start : start + 500]
        )
        await session.flush()
    return len(segments)


@dataclass
class SegmentRow:
    id: str
    geometry: dict
    description: str | None
    jam_factor: float
    closed: bool
    speed_kmh: float | None
    free_flow_kmh: float | None
    observed_at: datetime


async def current(
    session: AsyncSession,
    now: datetime,
    *,
    min_jam: float = 0.0,
    near: tuple[float, float, float] | None = None,
) -> list[SegmentRow]:
    """Fresh segments, optionally only around (lat, lon, radius_m) and above a congestion level."""
    query = select(
        TrafficSegment.id,
        func.ST_AsGeoJSON(func.ST_Simplify(TrafficSegment.geometry, 0.00003), 5),
        TrafficSegment.description,
        TrafficSegment.jam_factor,
        TrafficSegment.closed,
        TrafficSegment.speed_kmh,
        TrafficSegment.free_flow_kmh,
        TrafficSegment.observed_at,
    ).where(TrafficSegment.retrieved_at >= now - FRESH_FOR)
    if min_jam > 0:
        query = query.where((TrafficSegment.jam_factor >= min_jam) | TrafficSegment.closed)
    if near is not None:
        lat, lon, radius = near
        point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
        query = query.where(func.ST_DWithin(cast(TrafficSegment.geometry, Geography), point, radius))
    rows = await session.execute(query.order_by(TrafficSegment.jam_factor.desc()))
    return [SegmentRow(str(i), json.loads(g), d, j, c, s, f, o) for i, g, d, j, c, s, f, o in rows]
