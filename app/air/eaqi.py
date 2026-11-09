"""European Air Quality Index levels as shown to users.

The concentration bands (EEA revision 2024, ETC HE report 2024/17) are added with the air
module once they are verified against the report.
"""

from dataclasses import dataclass


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
