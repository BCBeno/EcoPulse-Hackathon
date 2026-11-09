"""Builds the Iași road network for GraphHopper from the Geofabrik extract of Romania.

Downloads romania-latest.osm.pbf (~330 MB, cached) and keeps only highway ways that touch the city
bbox plus a ~5 km margin, with the nodes they reference. Result: data/iasi.osm.pbf (a few MB).

Usage:  uv run python deploy/graphhopper/build_extract.py [--refresh]
"""

import sys
from pathlib import Path

import httpx
import osmium

SOURCE_URL = "https://download.geofabrik.de/europe/romania-latest.osm.pbf"
# south, west, north, east: the city bbox (47.05,27.40,47.26,27.75) plus about 5 km on every side.
SOUTH, WEST, NORTH, EAST = 47.005, 27.334, 47.305, 27.816
DATA = Path(__file__).parent / "data"
USER_AGENT = "EcoPulse/1.0 (+https://ecopulse.bcbeno.me)"


def download(target: Path, refresh: bool) -> None:
    if target.exists() and not refresh:
        return
    partial = target.with_suffix(".part")
    with httpx.stream(
        "GET", SOURCE_URL, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120
    ) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    partial.replace(target)


def inside(location) -> bool:
    return location.valid() and SOUTH <= location.lat <= NORTH and WEST <= location.lon <= EAST


def extract(source: Path, target: Path) -> int:
    ways = 0
    processor = osmium.FileProcessor(str(source), osmium.osm.NODE | osmium.osm.WAY).with_locations()
    with osmium.BackReferenceWriter(str(target), ref_src=str(source), overwrite=True) as writer:
        for obj in processor:
            if obj.is_way() and "highway" in obj.tags and any(inside(n.location) for n in obj.nodes):
                writer.add_way(obj)
                ways += 1
    return ways


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    source, target = DATA / "romania-latest.osm.pbf", DATA / "iasi.osm.pbf"
    download(source, refresh="--refresh" in sys.argv)
    ways = extract(source, target)
    print(f"Wrote {target} with {ways} highway ways ({target.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
