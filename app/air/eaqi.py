"""European Air Quality Index (EAQI): levels, labels and concentration bands.

Bands as published on https://airindex.eea.europa.eu/AQI/index.html (revision described in
ETC HE report 2024/17), checked 2026-11-09. The EEA defines the index on hourly concentrations;
a value is rounded to a whole µg/m³ and compared with the inclusive upper limits of levels 1-5.
Colours are the commonly used EEA palette; the EEA page itself does not list hex codes.
"""

import math
from dataclasses import dataclass

# Inclusive upper limits (µg/m³) of levels 1..5; anything above is level 6.
BANDS: dict[str, tuple[int, int, int, int, int]] = {
    "pm25": (5, 15, 50, 90, 140),
    "pm10": (15, 45, 120, 195, 270),
    "no2": (10, 25, 60, 100, 150),
    "o3": (60, 100, 120, 160, 180),
    "so2": (20, 40, 125, 190, 275),
}
POLLUTANTS = tuple(BANDS)


def sub_index(parameter: str, value: float | None) -> int | None:
    bands = BANDS.get(parameter)
    if bands is None or value is None or not math.isfinite(value) or value < 0:
        return None
    rounded = math.floor(value + 0.5)
    return next((level for level, upper in enumerate(bands, start=1) if rounded <= upper), 6)


def overall(values: dict[str, float | None]) -> tuple[int | None, str | None]:
    """The poorest sub-index and the pollutant responsible for it."""
    best: tuple[int | None, str | None] = (None, None)
    for parameter, value in values.items():
        level = sub_index(parameter, value)
        if level is not None and (best[0] is None or level > best[0]):
            best = (level, parameter)
    return best


@dataclass(frozen=True)
class Level:
    level: int
    key: str
    label: str
    color: str
    advice: str


LEVELS = (
    Level(1, "good", "Bun", "#50F0E6", "Aerul e bun. Bucură-te de activitățile în aer liber."),
    Level(2, "fair", "Acceptabil", "#50CCAA", "Poți face liniștit activități în aer liber."),
    Level(
        3,
        "moderate",
        "Moderat",
        "#F0E641",
        "Dacă ai simptome sau ești sensibil, redu efortul intens în aer liber.",
    ),
    Level(
        4,
        "poor",
        "Slab",
        "#FF5050",
        "Redu efortul intens afară, mai ales dacă ai probleme respiratorii sau cardiace.",
    ),
    Level(
        5,
        "very_poor",
        "Foarte slab",
        "#960032",
        "Evită efortul intens afară. Copiii, vârstnicii și persoanele sensibile "
        "să stea mai mult în interior.",
    ),
    Level(
        6,
        "extremely_poor",
        "Extrem de slab",
        "#7D2181",
        "Evită activitățile în aer liber și rămâi cât mai mult în interior.",
    ),
)

NOTE = "Nivel orientativ, calculat pe valoarea orară."
