from typing import Annotated, Literal

from geoalchemy2.elements import WKBElement, WKTElement
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, Field
from shapely import wkt


class LatLon(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


def point_wkt(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def to_latlon(value: WKBElement | WKTElement | str) -> LatLon:
    """Works for values loaded from the database and for EWKT strings set before a refresh."""
    point = wkt.loads(value.split(";", 1)[-1]) if isinstance(value, str) else to_shape(value)
    return LatLon(lat=point.y, lon=point.x)


# GeoJSON geometries (coordinates are [lon, lat]).
Position = Annotated[list[float], Field(min_length=2, max_length=2)]


class GeoPoint(BaseModel):
    type: Literal["Point"]
    coordinates: Position


class GeoLineString(BaseModel):
    type: Literal["LineString"]
    coordinates: list[Position]


class GeoPolygon(BaseModel):
    type: Literal["Polygon"]
    coordinates: list[list[Position]]


Geometry = Annotated[GeoPoint | GeoLineString | GeoPolygon, Field(discriminator="type")]
