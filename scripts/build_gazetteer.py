"""Builds app/places/gazetteer.json: well-known Iași places with coordinates looked up once in Nominatim.

The list of names is curated here; coordinates come from OpenStreetMap (© OpenStreetMap contributors).
Usage:  uv run python scripts/build_gazetteer.py   (respects Nominatim's 1 request/second policy)
"""

import json
import time
from pathlib import Path

import httpx

TARGET = Path(__file__).resolve().parents[1] / "app" / "places" / "gazetteer.json"
BBOX = (47.05, 27.40, 47.26, 27.75)  # south, west, north, east
USER_AGENT = "EcoPulse/1.0 (+https://ecopulse.bcbeno.me)"

# (id, display name, Nominatim query, kind, aliases)
PLACES = [
    ("copou", "Copou", "Copou, Iași", "neighbourhood", ["parcul copou"]),
    ("tatarasi", "Tătărași", "Tătărași, Iași", "neighbourhood", []),
    ("podu-ros", "Podu Roș", "Podu Roș, Iași", "neighbourhood", ["podul ros"]),
    ("podu-de-piatra", "Podu de Piatră", "Podu de Piatră, Iași", "neighbourhood", []),
    ("nicolina", "Nicolina", "Nicolina, Iași", "neighbourhood", []),
    ("dacia", "Dacia", "Dacia, Iași", "neighbourhood", []),
    ("alexandru-cel-bun", "Alexandru cel Bun", "Alexandru cel Bun, Iași", "neighbourhood", ["alexandru"]),
    ("pacurari", "Păcurari", "Păcurari, Iași", "neighbourhood", []),
    ("canta", "Canta", "Canta, Iași", "neighbourhood", []),
    ("mircea-cel-batran", "Mircea cel Bătrân", "Mircea cel Bătrân, Iași", "neighbourhood", ["mircea"]),
    ("galata", "Galata", "Galata, Iași", "neighbourhood", []),
    ("frumoasa", "Frumoasa", "Frumoasa, Iași", "neighbourhood", []),
    ("tudor-vladimirescu", "Tudor Vladimirescu", "Tudor Vladimirescu, Iași", "neighbourhood", ["tudor"]),
    ("ticau", "Țicău", "Țicău, Iași", "neighbourhood", []),
    ("sararie", "Sărărie", "Sărărie, Iași", "neighbourhood", []),
    ("bucium", "Bucium", "Bucium, Iași", "neighbourhood", []),
    ("cug", "CUG", "Zona Industrială CUG, Iași", "neighbourhood", ["zona industriala"]),
    ("gara", "Gara Iași", "Gara Iași", "landmark", ["gara", "gara de nord", "gara mare"]),
    ("piata-unirii", "Piața Unirii", "Piața Unirii, Iași", "landmark", ["unirii", "unire"]),
    ("palas", "Palas", "Palas Mall, Iași", "landmark", ["palas mall"]),
    ("iulius-mall", "Iulius Mall", "Iulius Mall, Iași", "landmark", ["iulius"]),
    (
        "universitate",
        "Universitatea Cuza",
        "Universitatea Alexandru Ioan Cuza, Iași",
        "landmark",
        ["uaic", "cuza", "universitate"],
    ),
    (
        "teatrul-national",
        "Teatrul Național",
        "Teatrul Național Vasile Alecsandri, Iași",
        "landmark",
        ["teatru"],
    ),
    (
        "gradina-botanica",
        "Grădina Botanică",
        "Grădina Botanică Anastasie Fătu, Iași",
        "landmark",
        ["botanica"],
    ),
    (
        "spital-sf-spiridon",
        "Spitalul Sf. Spiridon",
        "Spitalul Sfântul Spiridon, Iași",
        "landmark",
        ["sf spiridon", "spiridon"],
    ),
    ("targu-cucu", "Târgu Cucu", "Târgu Cucu, Iași", "landmark", ["targu cucului"]),
    ("hala-centrala", "Hala Centrală", "Hala Centrală, Iași", "landmark", ["hala"]),
    ("ciric", "Ciric", "Ciric, Iași", "neighbourhood", []),
    ("aeroport", "Aeroportul Iași", "Aeroportul Internațional Iași", "landmark", ["aeroport"]),
    ("tehnopolis", "Tehnopolis", "Tehnopolis, Iași", "landmark", []),
]


def main() -> None:
    south, west, north, east = BBOX
    found, missing = [], []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for place_id, name, query, kind, aliases in PLACES:
            response = client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "viewbox": f"{west},{north},{east},{south}",
                    "bounded": 1,
                    "accept-language": "ro",
                },
            )
            time.sleep(1.1)
            results = response.json() if response.status_code == 200 else []
            if not results:
                missing.append(name)
                continue
            lat, lon = round(float(results[0]["lat"]), 5), round(float(results[0]["lon"]), 5)
            found.append(
                {"id": place_id, "name": name, "kind": kind, "aliases": aliases, "lat": lat, "lon": lon}
            )
            print(f"{name:28} {lat:.5f} {lon:.5f}  <- {results[0].get('display_name', '')[:70]}")
    TARGET.write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(found)} places; not found: {missing or 'none'}")


if __name__ == "__main__":
    main()
