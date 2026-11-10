"""Report categories and severities: the single definition, exposed to the frontend via /config."""

from dataclasses import dataclass
from typing import Literal

CategoryId = Literal[
    "smoke",
    "dust",
    "industrial",
    "construction",
    "machinery",
    "traffic",
    "obstruction",
    "flooding",
    "infrastructure",
    "lighting",
    "other",
]
Affects = Literal["air", "obstacle", "air_obstacle", "info"]
# Icon keys the frontend maps to its own icon set.
IconKey = Literal[
    "smoke",
    "dust",
    "factory",
    "construction",
    "machinery",
    "traffic",
    "obstruction",
    "flooding",
    "infrastructure",
    "lighting",
    "other",
]


@dataclass(frozen=True)
class Category:
    id: CategoryId
    label: str
    icon: IconKey
    affects: Affects
    duration_h: int
    radius_m: int


CATEGORIES: dict[str, Category] = {
    c.id: c
    for c in (
        Category("smoke", "Fum", "smoke", "air", 6, 400),
        Category("dust", "Praf", "dust", "air", 8, 250),
        Category("industrial", "Poluare industrială / miros", "factory", "air", 12, 500),
        Category("construction", "Șantier", "construction", "air_obstacle", 24, 200),
        Category("machinery", "Utilaje", "machinery", "air", 8, 150),
        Category("traffic", "Trafic intens", "traffic", "air", 2, 300),
        Category("obstruction", "Obstacol", "obstruction", "obstacle", 24, 50),
        Category("flooding", "Apă / inundație", "flooding", "obstacle", 24, 80),
        Category("infrastructure", "Infrastructură stricată", "infrastructure", "obstacle", 72, 30),
        Category("lighting", "Iluminat stricat", "lighting", "info", 72, 50),
        Category("other", "Altceva", "other", "info", 6, 100),
    )
}

SEVERITIES = {1: "Mică", 2: "Medie", 3: "Mare"}
FLAG_REASONS = {
    "spam": "Spam sau reclamă",
    "offensive": "Limbaj jignitor",
    "personal_data": "Conține date personale",
    "wrong": "Informație greșită",
}

# Community resolution: at least this many "gone" votes, and more "gone" than "still_there".
GONE_VOTES_TO_RESOLVE = 3
DESCRIPTION_MAX = 600
ORIGINAL_TEXT_MAX = 1000
MAX_OBSERVED_AGE_H = 48
