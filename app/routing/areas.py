"""Penalised areas for GraphHopper custom models, built from the pollution grid and active reports.

Two pollution tiers are relative to the city at that moment, so the cleanest route avoids local
hotspots even when the whole city sits at the same EAQI level:
  hot1: PM2.5 >= max(75th percentile, median + 4 µg/m³), or EAQI >= 4
  hot2: PM2.5 >= max(90th percentile, median + 8 µg/m³), or EAQI >= 5
"""

import math

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box, mapping

from app.air.grid import GridSpec

SIMPLIFY_DEG = 0.0004
MAX_POLYGONS_PER_TIER = 15
MIN_AREA_DEG2 = 0.00225 * 0.0033 * 2  # ignore isolated single cells

# multiply_by per tier and route kind (1 = no penalty).
FACTORS = {
    "balanced": {
        "hot1": 0.8,
        "hot2": 0.55,
        "report_air": 0.6,
        "construction": 0.85,
        "traffic": 0.85,
        "obstacle": 0.2,
    },
    "cleanest": {
        "hot1": 0.5,
        "hot2": 0.25,
        "report_air": 0.2,
        "construction": 0.6,
        "traffic": 0.6,
        "obstacle": 0.05,
    },
}


def hotspot_masks(pm25: np.ndarray | None, eaqi: np.ndarray) -> dict[str, np.ndarray]:
    hot1 = eaqi >= 4
    hot2 = eaqi >= 5
    if pm25 is not None and np.isfinite(pm25).any():
        values = pm25[np.isfinite(pm25)]
        median = float(np.median(values))
        threshold1 = max(float(np.percentile(values, 75)), median + 4)
        threshold2 = max(float(np.percentile(values, 90)), median + 8)
        finite = np.nan_to_num(pm25, nan=-1)
        hot1 = hot1 | (finite >= threshold1)
        hot2 = hot2 | (finite >= threshold2)
    return {"hot1": hot1 & ~hot2, "hot2": hot2}


def mask_polygons(spec: GridSpec, mask: np.ndarray) -> list[Polygon]:
    cells = [
        box(
            spec.west + c * spec.d_lon,
            spec.south + r * spec.d_lat,
            spec.west + (c + 1) * spec.d_lon,
            spec.south + (r + 1) * spec.d_lat,
        )
        for r, c in zip(*np.nonzero(mask), strict=True)
    ]
    if not cells:
        return []
    merged = shapely.union_all(cells).simplify(SIMPLIFY_DEG)
    return _largest(_polygons(merged))


def circle(lat: float, lon: float, radius_m: float, vertices: int = 16) -> Polygon:
    dx = radius_m / (111_320 * math.cos(math.radians(lat)))
    dy = radius_m / 111_320
    return Polygon(
        [
            (lon + dx * math.cos(2 * math.pi * i / vertices), lat + dy * math.sin(2 * math.pi * i / vertices))
            for i in range(vertices)
        ]
    )


def buffered(geometry, radius_m: float) -> list[Polygon]:
    """Buffers a lon/lat geometry by metres: stretch longitude so both axes share a scale, buffer, undo."""
    k = math.cos(math.radians(geometry.centroid.y))
    stretched = affinity.scale(geometry, xfact=k, yfact=1, origin=(0, 0))
    grown = stretched.buffer(radius_m / 111_320)
    return _polygons(affinity.scale(grown, xfact=1 / k, yfact=1, origin=(0, 0)))


def union_tier(polygons: list[Polygon]) -> list[Polygon]:
    if not polygons:
        return []
    return _largest(_polygons(shapely.union_all(polygons).simplify(SIMPLIFY_DEG / 2)))


def _polygons(geometry) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]


def _largest(polygons: list[Polygon]) -> list[Polygon]:
    kept = [p for p in polygons if p.area >= MIN_AREA_DEG2 or (p.area > 0 and len(polygons) <= 3)]
    return sorted(kept, key=lambda p: p.area, reverse=True)[:MAX_POLYGONS_PER_TIER]


def custom_model(kind: str, tiers: dict[str, list[Polygon]]) -> dict | None:
    """GraphHopper custom_model with one area per polygon; None when nothing is penalised."""
    features, priority = [], []
    for tier, polygons in tiers.items():
        factor = FACTORS[kind].get(tier)
        if not polygons or factor is None:
            continue
        ids = [f"{tier}_{index}" for index in range(len(polygons))]
        for area_id, polygon in zip(ids, polygons, strict=True):
            features.append(
                {"type": "Feature", "id": area_id, "properties": {}, "geometry": mapping(polygon)}
            )
        priority.append({"if": " || ".join(f"in_{area_id}" for area_id in ids), "multiply_by": str(factor)})
    if not priority:
        return None
    return {"areas": {"type": "FeatureCollection", "features": features}, "priority": priority}
