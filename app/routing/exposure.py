"""Exposure along a route and the choice of the three routes shown. Pure functions, no I/O.

exposure index = Σ PM2.5 × Δt (µg·h/m³), sampling every ~20 m with the travel time GraphHopper gives
for each segment. It compares routes of the same request only (same mode, same departure).

Active community reports that affect the air (smoke, dust, ...) are not in the grid; inside their
radius they add a PM2.5-equivalent by severity (REPORT_BOOST), so a route through a reported fire
scores worse than a slightly longer one around it. This is a heuristic, not a measurement.
"""

import math
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np
from shapely.geometry import LineString, Point
from shapely.prepared import prep

from app.air import eaqi
from app.air.grid import GridSpec, sample
from app.routing.graphhopper import GhPath

STEP_M = 20.0
MAX_SLOWDOWN = 1.5  # the cleanest route may take at most 1.5 x the fastest
MIN_GAIN = 0.05  # below 5 % less exposure, the cleaner route is not worth showing separately
SAME_ROUTE_OVERLAP = 0.9
OVERLAP_BUFFER_M = 15.0
LOW_CONFIDENCE = 0.35  # at or below model-only confidence (grid.MODEL_CONFIDENCE = 0.3)
REPORT_BOOST = {1: 10.0, 2: 25.0, 3: 50.0}  # µg/m³ PM2.5-equivalent inside an air report's radius


@dataclass(frozen=True)
class Boost:
    lat: float
    lon: float
    radius_m: float
    extra: float


@dataclass(frozen=True)
class AreaBoost:
    """Extra PM2.5-equivalent inside a polygon (lon/lat), e.g. along a congested road."""

    polygon: object
    extra: float


@dataclass
class Exposure:
    index: float
    avg_pm25: float | None
    max_level: int | None
    minutes_poor_or_worse: float
    low_confidence_share: float
    vertex_levels: list[int | None]


@dataclass
class Candidate:
    path: GhPath
    exposure: Exposure
    kinds: list[str] = field(default_factory=list)


def _length_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    x = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return 6_371_000 * math.hypot(x, math.radians(b[1] - a[1]))


def measure(
    path: GhPath,
    spec: GridSpec,
    layers: dict[str, np.ndarray],
    boosts: tuple[Boost, ...] = (),
    area_boosts: tuple[AreaBoost, ...] = (),
) -> Exposure:
    pm25 = layers.get("pm25")
    levels_layer = layers.get("eaqi")
    confidence = layers.get("confidence")
    dose = known_time = low_conf_time = poor_time = 0.0
    total_time = sum(path.segment_s) or 1.0
    max_level = None
    prepared = [(prep(area.polygon), area.extra) for area in area_boosts]
    for (a, b), seconds in zip(pairwise(path.coordinates), path.segment_s, strict=True):
        pieces = max(1, math.ceil(_length_m(a, b) / STEP_M))
        for k in range(pieces):
            t = (k + 0.5) / pieces
            lon, lat = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            dt = seconds / pieces
            value = sample(spec, pm25, lat, lon) if pm25 is not None else None
            level = sample(spec, levels_layer, lat, lon) if levels_layer is not None else None
            conf = sample(spec, confidence, lat, lon) if confidence is not None else 0.0
            extra = sum(b.extra for b in boosts if _length_m((lon, lat), (b.lon, b.lat)) <= b.radius_m)
            if prepared:
                here = Point(lon, lat)
                extra += sum(value for area, value in prepared if area.contains(here))
            if value is not None or extra:
                dose += ((value or 0.0) + extra) * dt
                known_time += dt
            if level:
                max_level = int(level) if max_level is None else max(max_level, int(level))
                if level >= 4:
                    poor_time += dt
            # Without any grid the route notes already say so; no per-route warning.
            if pm25 is not None and (conf or 0.0) < LOW_CONFIDENCE:
                low_conf_time += dt
    vertex_levels = []
    for lon, lat in path.coordinates:
        level = sample(spec, levels_layer, lat, lon) if levels_layer is not None else None
        vertex_levels.append(int(level) if level else None)
    return Exposure(
        index=dose / 3600,
        avg_pm25=dose / known_time if known_time else None,
        max_level=max_level,
        minutes_poor_or_worse=poor_time / 60,
        low_confidence_share=low_conf_time / total_time,
        vertex_levels=vertex_levels,
    )


def overlap(a: GhPath, b: GhPath) -> float:
    """Share of route `b` that runs within ~15 m of route `a`."""
    line_a, line_b = LineString(a.coordinates), LineString(b.coordinates)
    lat = line_a.centroid.y
    k = math.cos(math.radians(lat))

    def metric(line: LineString) -> LineString:
        return LineString([(x * k * 111_320, y * 111_320) for x, y in line.coords])

    ma, mb = metric(line_a), metric(line_b)
    if mb.length == 0:
        return 1.0
    return ma.buffer(OVERLAP_BUFFER_M).intersection(mb).length / mb.length


def choose(candidates: list[Candidate]) -> list[Candidate]:
    """Fastest, cleanest and balanced, merging near-identical routes; ordered cleanest -> fastest."""
    unique: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: c.path.duration_s):
        if not any(overlap(kept.path, candidate.path) >= SAME_ROUTE_OVERLAP for kept in unique):
            unique.append(candidate)
    if not unique:
        return []
    fastest = unique[0]
    fastest.kinds = ["fastest"]
    acceptable = [c for c in unique if c.path.duration_s <= fastest.path.duration_s * MAX_SLOWDOWN]
    cleanest = min(acceptable, key=lambda c: c.exposure.index)
    if (
        fastest.exposure.index <= 0
        or (fastest.exposure.index - cleanest.exposure.index) / fastest.exposure.index < MIN_GAIN
    ):
        cleanest = fastest
    cleanest.kinds = [*cleanest.kinds, "cleanest"] if cleanest is fastest else ["cleanest"]
    others = [c for c in acceptable if c is not fastest and c is not cleanest]
    chosen = [cleanest] if cleanest is not fastest else []
    if others:
        durations = [c.path.duration_s for c in acceptable]
        indexes = [c.exposure.index for c in acceptable]

        def score(c: Candidate) -> float:
            d_span = (max(durations) - min(durations)) or 1.0
            e_span = (max(indexes) - min(indexes)) or 1.0
            return (
                0.5 * (c.path.duration_s - min(durations)) / d_span
                + 0.5 * (c.exposure.index - min(indexes)) / e_span
            )

        balanced = min(others, key=score)
        between = fastest.exposure.index > balanced.exposure.index > cleanest.exposure.index
        if between:
            balanced.kinds = ["balanced"]
            chosen.append(balanced)
    if fastest not in chosen:
        chosen.append(fastest)
    order = {"cleanest": 0, "balanced": 1, "fastest": 2}
    return sorted(chosen, key=lambda c: min(order[k] for k in c.kinds))


def segments(levels: list[int | None]) -> list[tuple[int, int, int | None]]:
    """Runs of equal EAQI level over coordinate indexes: (from, to, level)."""
    runs: list[tuple[int, int, int | None]] = []
    for index, level in enumerate(levels):
        if runs and runs[-1][2] == level:
            runs[-1] = (runs[-1][0], index, level)
        else:
            if runs:
                runs[-1] = (runs[-1][0], index, runs[-1][2])
            runs.append((index, index, level))
    return runs


def label_level(level: int | None) -> str:
    return next((lvl.label for lvl in eaqi.LEVELS if lvl.level == level), "necunoscut")
