"""Normalized records produced by every provider, independent of the source format."""

import math
from dataclasses import dataclass, field
from datetime import datetime

# Readings above this are physically implausible for ambient air in a city.
MAX_CONCENTRATION = 1000.0


@dataclass(frozen=True)
class StationReading:
    provider: str
    external_id: str
    kind: str  # low_cost | official
    lat: float
    lon: float
    parameter: str
    observed_at: datetime
    value: float
    meta: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ForecastValue:
    lat: float
    lon: float
    parameter: str
    valid_at: datetime
    value: float

    @property
    def point_id(self) -> str:
        return f"{self.lat:.2f},{self.lon:.2f}"


@dataclass(frozen=True)
class WeatherRecord:
    valid_at: datetime
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_ms: float | None
    wind_dir_deg: float | None
    precipitation_mm: float | None


@dataclass(frozen=True)
class OsmRecord:
    osm_id: str
    kind: str
    name: str | None
    geometry: dict
    tags: dict


def number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def concentration(value) -> float | None:
    result = number(value)
    return result if result is not None and 0 <= result <= MAX_CONCENTRATION else None
