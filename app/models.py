"""Imports every table so `Base.metadata` is complete (used by Alembic and tests)."""

from app.agent.models import LlmUsage
from app.air.models import (
    GridSnapshot,
    Measurement,
    ModelForecast,
    OsmFeature,
    Sensor,
    SourceRun,
    WeatherHour,
)
from app.db import Base
from app.devices.models import Device
from app.places.models import PlaceCache
from app.reports.models import Report, ReportDraft, ReportFlag, ReportVote

__all__ = [
    "Base",
    "Device",
    "GridSnapshot",
    "LlmUsage",
    "Measurement",
    "ModelForecast",
    "OsmFeature",
    "PlaceCache",
    "Report",
    "ReportDraft",
    "ReportFlag",
    "ReportVote",
    "Sensor",
    "SourceRun",
    "WeatherHour",
]
