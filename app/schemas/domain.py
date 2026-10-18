import hashlib
import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class Category(StrEnum):
    OFFICIAL = "OFFICIAL_MEASUREMENT"
    SENSOR = "LOW_COST_SENSOR_MEASUREMENT"
    MODEL = "MODEL_FORECAST"
    WEATHER = "WEATHER_CONTEXT"
    OSM = "OSM_MAPPED_FEATURE"
    WORK = "OFFICIAL_PLANNED_WORK"
    COMMUNITY = "COMMUNITY_REPORT"
    SECONDARY = "SECONDARY_SOURCE_ANNOUNCEMENT"


class UTCModel(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def normalize_timestamp(cls, value):
        return value.astimezone(UTC) if isinstance(value, datetime) else value


class Coordinates(UTCModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class ObservationData(Coordinates):
    provider: str
    source_record_id: str
    sensor_id: str
    canonical_sensor_id: str | None = None
    parameter: str
    original_value: float | None
    original_unit: str
    value: float | None
    unit: str
    observed_at: AwareDatetime
    retrieved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    category: Category
    quality: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)

    @property
    def id(self):
        key = f"{self.provider}:{self.sensor_id}:{self.parameter}:{self.observed_at.astimezone(UTC).isoformat()}:{self.provenance.get('product', 'measurement')}"
        return hashlib.sha256(key.encode()).hexdigest()


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def normalize_value(value, unit, parameter):
    original = numeric(value)
    valid = original
    if valid is not None and (
        (parameter not in {"temperature", "temperature_2m"} and valid < 0)
        or (parameter in {"humidity", "relative_humidity_2m"} and valid > 100)
    ):
        valid = None
    canonical = unit.replace("μ", "u").replace("µ", "u").replace("³", "3").strip()
    if parameter in {"pm25", "pm10", "no2", "o3", "so2", "co"}:
        if canonical in {"ug/m3", "ug/m^3"}:
            return original, valid, "µg/m³"
        if canonical in {"mg/m3", "mg/m^3"}:
            return original, valid * 1000 if valid is not None else None, "µg/m³"
        # Gas mixing ratios require temperature/pressure: no implicit conversion.
        return original, None, unit
    return original, valid, unit


class Register(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)


class Login(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    display_name: str
    role: str


ReportCategory = Literal[
    "construction",
    "traffic",
    "smoke",
    "industrial",
    "machinery",
    "dust",
    "infrastructure",
    "flooding",
    "obstruction",
    "lighting",
    "other",
]


class ReportCreate(Coordinates):
    category: ReportCategory
    description: str = Field(min_length=5, max_length=2000)
    severity: int = Field(default=2, ge=1, le=5)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def observation_time(self):
        now = datetime.now(UTC)
        if self.observed_at > now + timedelta(minutes=5) or self.observed_at < now - timedelta(hours=48):
            raise ValueError("Observation must be within the last 48 hours and not in the future")
        return self


class TextReport(Coordinates):
    """Free-text report published at the device's current location, without follow-up questions."""

    text: str = Field(min_length=2, max_length=2000)
    accuracy_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Chosen explicitly by the user (e.g. «Fum»); otherwise inferred from the text.
    category: ReportCategory | None = None


class AskPayload(BaseModel):
    """A question for the assistant; the optional position is the device's or a point on the map."""

    text: str = Field(min_length=2, max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)


class RouteRequest(UTCModel):
    origin: Coordinates
    destination: Coordinates
    mode: Literal["walk", "bike", "motorcycle"] = "walk"
    departure_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    preferences: dict[str, bool] = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def departure_window(self):
        now = datetime.now(UTC)
        if self.departure_at < now - timedelta(minutes=15) or self.departure_at > now + timedelta(days=30):
            raise ValueError("Departure must be now or within 30 days")
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        return self


class JourneyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    request: RouteRequest


class BrowserLocation(Coordinates):
    accuracy_m: float = Field(ge=0, allow_inf_nan=False)
    captured_at: AwareDatetime
    source: Literal["browser_geolocation"]


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    route_request: RouteRequest | None = None
    client_message_id: UUID | None = None
    current_location: BrowserLocation | None = None
    location_candidate_id: UUID | None = None


class WorkReview(Coordinates):
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: Literal["scheduled", "active", "cancelled", "expired"]

    @model_validator(mode="after")
    def dates(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("End date must follow start date")
        return self
