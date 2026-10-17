from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.domain import Coordinates, ReportCreate, RouteRequest, WorkReview


def test_timezone_offset_normalized_before_database():
    local = datetime.now(UTC).astimezone(timezone(timedelta(hours=3)))
    report = ReportCreate(
        latitude=47.15,
        longitude=27.6,
        category="dust",
        description="Synthetic timezone fixture",
        observed_at=local,
    )
    assert report.observed_at.utcoffset() == timedelta(0)
    assert report.observed_at == local
    route = RouteRequest(
        origin=Coordinates(latitude=47.15, longitude=27.6),
        destination=Coordinates(latitude=47.16, longitude=27.61),
        departure_at=local,
    )
    assert route.departure_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", ["2026-10-25T03:30:00", "2026-03-29T03:30:00"])
def test_unqualified_clock_times_rejected(value):
    with pytest.raises(ValidationError):
        RouteRequest(
            origin={"latitude": 47.15, "longitude": 27.6},
            destination={"latitude": 47.16, "longitude": 27.61},
            departure_at=value,
        )


def test_production_requires_explicit_secrets_and_postgres():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, app_env="production", jwt_secret_key="short", database_url="sqlite+aiosqlite://"
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, city_bbox="47,27,46,28")


def test_planned_work_invalid_interval_rejected():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        WorkReview(
            latitude=47.15,
            longitude=27.6,
            status="scheduled",
            starts_at=now,
            ends_at=now - timedelta(hours=1),
        )
