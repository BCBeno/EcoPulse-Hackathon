"""Quality checks for station readings before they shape the grid. Pure functions, no I/O."""

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

STALE_AFTER = timedelta(hours=2)
HUMID_PCT = 90.0
NEIGHBOUR_RADIUS_M = 2000.0
OFFICIAL_WEIGHT = 3.0
QaFlag = Literal["high_humidity", "inconsistent", "outlier"]


@dataclass
class Station:
    id: UUID
    provider: str
    kind: str
    lat: float
    lon: float
    values: dict[str, float] = field(default_factory=dict)
    observed_at: dict[str, datetime] = field(default_factory=dict)

    def newest(self) -> datetime | None:
        return max(self.observed_at.values(), default=None)


@dataclass
class Assessment:
    status: str = "ok"  # ok | stale | outlier
    flags: list[str] = field(default_factory=list)
    weight: float = 1.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation; accurate to well under 1 % at city scale."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6_371_000 * math.hypot(x, y)


def assess(stations: list[Station], now: datetime, fallback_humidity: float | None) -> dict[UUID, Assessment]:
    result: dict[UUID, Assessment] = {}
    for station in stations:
        verdict = Assessment(weight=OFFICIAL_WEIGHT if station.kind == "official" else 1.0)
        newest = station.newest()
        if newest is None or now - newest > STALE_AFTER:
            verdict.status, verdict.weight = "stale", 0.0
        pm25, pm10 = station.values.get("pm25"), station.values.get("pm10")
        if pm25 is not None and pm10 is not None and pm25 > pm10 + 2:
            verdict.flags.append("inconsistent")
            verdict.weight *= 0.5
        humidity = station.values.get("humidity", fallback_humidity)
        if station.kind == "low_cost" and humidity is not None and humidity >= HUMID_PCT:
            # Optical low-cost sensors over-read particles in fog and very humid air.
            verdict.flags.append("high_humidity")
            verdict.weight *= 0.3
        result[station.id] = verdict
    _mark_outliers(stations, result)
    return result


def _mark_outliers(stations: list[Station], result: dict[UUID, Assessment]) -> None:
    usable = [s for s in stations if "pm25" in s.values and result[s.id].status == "ok"]
    for station in usable:
        neighbours = [
            other.values["pm25"]
            for other in usable
            if other.id != station.id
            and distance_m(station.lat, station.lon, other.lat, other.lon) <= NEIGHBOUR_RADIUS_M
        ]
        if len(neighbours) < 2:
            continue
        median = statistics.median(neighbours)
        spread = max(2.0, statistics.median(abs(v - median) for v in neighbours))
        if abs(station.values["pm25"] - median) > 4 * spread + 5:
            verdict = result[station.id]
            verdict.status, verdict.weight = "outlier", 0.0
            verdict.flags.append("outlier")
