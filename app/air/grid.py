"""The pollution grid: CAMS background corrected locally by the stations. Pure NumPy, no I/O.

value(cell) = background(cell) + confidence(cell) * IDW(residuals) * decay
  background  CAMS at that hour, bilinear between the 0.1° CAMS points
  residuals   station value - background at the station, weighted by quality
  confidence  1 next to a station, 0 at RADIUS_M or further
  decay       1 now, exp(-hours_ahead / 6) for forecasts
"""

import io
import math
from dataclasses import dataclass

import numpy as np

from app.air.eaqi import BANDS, POLLUTANTS
from app.config import Bounds

CELL_DEG_LAT = 0.00225  # ~250 m
CELL_DEG_LON = 0.0033  # ~250 m at 47° N
RADIUS_M = 2000.0
MIN_DISTANCE_M = 50.0
DECAY_HOURS = 6.0
EARTH_M_PER_DEG = 111_320.0


@dataclass(frozen=True)
class GridSpec:
    south: float
    west: float
    d_lat: float
    d_lon: float
    rows: int
    cols: int

    @classmethod
    def for_bounds(cls, bounds: Bounds) -> "GridSpec":
        rows = math.ceil((bounds.north - bounds.south) / CELL_DEG_LAT)
        cols = math.ceil((bounds.east - bounds.west) / CELL_DEG_LON)
        return cls(bounds.south, bounds.west, CELL_DEG_LAT, CELL_DEG_LON, rows, cols)

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        lats = self.south + (np.arange(self.rows) + 0.5) * self.d_lat
        lons = self.west + (np.arange(self.cols) + 0.5) * self.d_lon
        return np.meshgrid(lats, lons, indexing="ij")

    def cell(self, lat: float, lon: float) -> tuple[int, int] | None:
        row, col = int((lat - self.south) // self.d_lat), int((lon - self.west) // self.d_lon)
        return (row, col) if 0 <= row < self.rows and 0 <= col < self.cols else None


@dataclass
class StationInput:
    lat: float
    lon: float
    value: float
    weight: float


def bilinear(
    lattice: dict[tuple[float, float], float], lats: np.ndarray, lons: np.ndarray
) -> np.ndarray | None:
    """Interpolates values on a regular lat/lon lattice at arbitrary points (NaN outside or on gaps)."""
    if not lattice:
        return None
    axis_lat = np.array(sorted({lat for lat, _ in lattice}))
    axis_lon = np.array(sorted({lon for _, lon in lattice}))
    table = np.full((axis_lat.size, axis_lon.size), np.nan)
    for (lat, lon), value in lattice.items():
        table[np.searchsorted(axis_lat, lat), np.searchsorted(axis_lon, lon)] = value
    if axis_lat.size < 2 or axis_lon.size < 2:
        return np.full(np.shape(lats), np.nanmean(table))
    i = np.clip(np.searchsorted(axis_lat, lats) - 1, 0, axis_lat.size - 2)
    j = np.clip(np.searchsorted(axis_lon, lons) - 1, 0, axis_lon.size - 2)
    ty = np.clip((lats - axis_lat[i]) / (axis_lat[i + 1] - axis_lat[i]), 0, 1)
    tx = np.clip((lons - axis_lon[j]) / (axis_lon[j + 1] - axis_lon[j]), 0, 1)
    return (
        table[i, j] * (1 - ty) * (1 - tx)
        + table[i, j + 1] * (1 - ty) * tx
        + table[i + 1, j] * ty * (1 - tx)
        + table[i + 1, j + 1] * ty * tx
    )


def _distances(lats: np.ndarray, lons: np.ndarray, lat: float, lon: float) -> np.ndarray:
    x = (lons - lon) * EARTH_M_PER_DEG * math.cos(math.radians(lat))
    y = (lats - lat) * EARTH_M_PER_DEG
    return np.hypot(x, y)


def local_correction(
    lats: np.ndarray, lons: np.ndarray, residuals: list[tuple[float, float, float, float]]
) -> tuple[np.ndarray, np.ndarray]:
    """IDW of (lat, lon, residual, weight) within RADIUS_M, and the confidence of each cell."""
    weighted = np.zeros(np.shape(lats))
    total = np.zeros(np.shape(lats))
    nearest = np.full(np.shape(lats), np.inf)
    for lat, lon, residual, weight in residuals:
        if weight <= 0:
            continue
        distance = _distances(lats, lons, lat, lon)
        nearest = np.minimum(nearest, distance)
        w = np.where(distance <= RADIUS_M, weight / np.maximum(distance, MIN_DISTANCE_M) ** 2, 0.0)
        weighted += w * residual
        total += w
    correction = np.divide(weighted, total, out=np.zeros_like(weighted), where=total > 0)
    confidence = np.clip(1 - nearest / RADIUS_M, 0, 1)
    return correction, confidence


def pollutant_field(
    spec: GridSpec,
    background: dict[tuple[float, float], float],
    stations: list[StationInput],
    decay: float,
) -> tuple[np.ndarray | None, np.ndarray]:
    lats, lons = spec.centres()
    base = bilinear(background, lats, lons)
    if base is None:
        if not stations:
            return None, np.zeros((spec.rows, spec.cols))
        # No model: a flat field at the stations' weighted mean, corrected locally.
        mean = np.average([s.value for s in stations], weights=[max(s.weight, 1e-6) for s in stations])
        base = np.full((spec.rows, spec.cols), mean)
        at_station = np.full(len(stations), mean)
    else:
        at_station = bilinear(
            background, np.array([s.lat for s in stations]), np.array([s.lon for s in stations])
        )
    residuals = [
        (s.lat, s.lon, s.value - float(b), s.weight)
        for s, b in zip(stations, at_station if stations else [], strict=False)
        if np.isfinite(b)
    ]
    correction, confidence = local_correction(lats, lons, residuals)
    values = np.maximum(base + confidence * correction * decay, 0.0)
    return values, confidence


def eaqi_levels(fields: dict[str, np.ndarray]) -> np.ndarray:
    """Per cell: the poorest EAQI level over the available pollutants; 0 where nothing is known."""
    level = None
    for parameter, values in fields.items():
        if parameter not in BANDS:
            continue
        rounded = np.floor(np.nan_to_num(values, nan=-1.0) + 0.5)
        sub = np.searchsorted(np.array(BANDS[parameter]), rounded, side="left") + 1
        sub = np.where(np.isfinite(values) & (values >= 0), sub, 0)
        level = sub if level is None else np.maximum(level, sub)
    return level if level is not None else np.zeros(0, dtype=int)


def decay_for(hours_ahead: float) -> float:
    return 1.0 if hours_ahead <= 0 else math.exp(-hours_ahead / DECAY_HOURS)


def compute(
    spec: GridSpec,
    backgrounds: dict[str, dict[tuple[float, float], float]],
    stations: dict[str, list[StationInput]],
    hours_ahead: float = 0.0,
) -> dict[str, np.ndarray]:
    """All layers for one moment: one per pollutant, `eaqi` (0 = unknown) and `confidence`."""
    decay = decay_for(hours_ahead)
    layers: dict[str, np.ndarray] = {}
    confidence = np.zeros((spec.rows, spec.cols))
    for parameter in POLLUTANTS:
        values, conf = pollutant_field(
            spec, backgrounds.get(parameter, {}), stations.get(parameter, []), decay
        )
        if values is not None:
            layers[parameter] = values
        if parameter == "pm25":
            confidence = conf * decay
    eaqi = eaqi_levels(layers)
    layers["eaqi"] = eaqi.astype(np.float32) if eaqi.size else np.zeros((spec.rows, spec.cols), np.float32)
    layers["confidence"] = confidence
    return layers


def pack(layers: dict[str, np.ndarray]) -> bytes:
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **{name: array.astype(np.float32) for name, array in layers.items()})
    return buffer.getvalue()


def unpack(blob: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(blob), allow_pickle=False) as data:
        return {name: data[name] for name in data.files}


def sample(spec: GridSpec, layer: np.ndarray, lat: float, lon: float) -> float | None:
    cell = spec.cell(lat, lon)
    if cell is None:
        return None
    value = float(layer[cell])
    return value if math.isfinite(value) else None
