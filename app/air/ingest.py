"""Source synchronisation: fetch -> normalise -> batch upsert, with a run log and data retention."""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.models import Conversation
from app.air.models import (
    GridSnapshot,
    Measurement,
    ModelForecast,
    OsmFeature,
    Sensor,
    SourceRun,
    WeatherHour,
)
from app.air.providers import open_meteo, openaq, osm, sensor_community
from app.air.providers.records import ForecastValue, OsmRecord, StationReading, WeatherRecord
from app.config import Settings
from app.geo import point_wkt
from app.http import SourceError
from app.traffic import provider as here_traffic
from app.traffic import service as traffic_service

log = logging.getLogger("ecopulse.ingest")

MEASUREMENT_RETENTION = timedelta(days=7)
FORECAST_RETENTION = timedelta(hours=24)
GRID_RETENTION = timedelta(hours=6)
RUN_RETENTION = timedelta(days=7)
# Runs of billed sources are the request counter for the monthly cap: keep them past a month.
BILLED_RUN_RETENTION = timedelta(days=40)
BILLED_SOURCES = ("here_traffic",)
CONVERSATION_RETENTION = timedelta(days=30)
OSM_STALE_AFTER = timedelta(hours=48)
BATCH = 1000


class SourceNotConfigured(Exception):
    pass


class SourceSkipped(Exception):
    """A deliberate skip (e.g. a billing cap), recorded as `skipped` with the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


NIGHT_HOURS = (22, 6)  # local time: [22:00, 06:00) polls traffic less often


def is_night(local_hour: int) -> bool:
    start, end = NIGHT_HOURS
    return local_hour >= start or local_hour < end


async def billed_requests(session: AsyncSession, source: str, since: datetime) -> tuple[int, datetime | None]:
    """Requests made to a paid source since `since` (successful or failed; skips do not count)."""
    count, last = (
        await session.execute(
            select(func.count(), func.max(SourceRun.started_at)).where(
                SourceRun.source == source,
                SourceRun.status.in_(("ok", "failed")),
                SourceRun.started_at >= since,
            )
        )
    ).one()
    return count, last


async def store_readings(session: AsyncSession, readings: list[StationReading], now: datetime) -> int:
    if not readings:
        return 0
    stations: dict[tuple[str, str], StationReading] = {}
    for reading in readings:
        stations[(reading.provider, reading.external_id)] = reading
    rows = [
        {
            "provider": r.provider,
            "external_id": r.external_id,
            "kind": r.kind,
            "location": point_wkt(r.lat, r.lon),
            "meta": r.meta,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        for r in stations.values()
    ]
    statement = insert(Sensor).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Sensor.provider, Sensor.external_id],
        set_={
            "kind": statement.excluded.kind,
            "location": statement.excluded.location,
            "meta": statement.excluded.meta,
            "last_seen_at": statement.excluded.last_seen_at,
        },
    ).returning(Sensor.id, Sensor.provider, Sensor.external_id)
    ids = {
        (provider, external): sensor_id for sensor_id, provider, external in await session.execute(statement)
    }
    values = {(ids[(r.provider, r.external_id)], r.parameter, r.observed_at): r.value for r in readings}
    measurement_rows = [
        {"sensor_id": s, "parameter": p, "observed_at": t, "value": v, "retrieved_at": now}
        for (s, p, t), v in values.items()
    ]
    for start in range(0, len(measurement_rows), BATCH):
        chunk = insert(Measurement).values(measurement_rows[start : start + BATCH])
        await session.execute(
            chunk.on_conflict_do_update(
                index_elements=[Measurement.sensor_id, Measurement.parameter, Measurement.observed_at],
                set_={"value": chunk.excluded.value, "retrieved_at": chunk.excluded.retrieved_at},
            )
        )
    return len(measurement_rows)


async def store_forecasts(session: AsyncSession, values: list[ForecastValue], now: datetime) -> int:
    rows = [
        {
            "point_id": v.point_id,
            "parameter": v.parameter,
            "valid_at": v.valid_at,
            "lat": v.lat,
            "lon": v.lon,
            "value": v.value,
            "retrieved_at": now,
        }
        for v in values
    ]
    for start in range(0, len(rows), BATCH):
        chunk = insert(ModelForecast).values(rows[start : start + BATCH])
        await session.execute(
            chunk.on_conflict_do_update(
                index_elements=[ModelForecast.point_id, ModelForecast.parameter, ModelForecast.valid_at],
                set_={"value": chunk.excluded.value, "retrieved_at": chunk.excluded.retrieved_at},
            )
        )
    return len(rows)


async def store_weather(session: AsyncSession, records: list[WeatherRecord], now: datetime) -> int:
    if not records:
        return 0
    rows = [vars(r) | {"retrieved_at": now} for r in records]
    statement = insert(WeatherHour).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[WeatherHour.valid_at],
            set_={key: statement.excluded[key] for key in rows[0] if key != "valid_at"},
        )
    )
    return len(rows)


async def store_osm(session: AsyncSession, records: list[OsmRecord], now: datetime) -> int:
    if records:
        rows = [
            {
                "osm_id": r.osm_id,
                "kind": r.kind,
                "name": r.name,
                "geometry": func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(r.geometry)), 4326),
                "tags": r.tags,
                "retrieved_at": now,
            }
            for r in records
        ]
        statement = insert(OsmFeature).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[OsmFeature.osm_id],
                set_={
                    "name": statement.excluded.name,
                    "geometry": statement.excluded.geometry,
                    "tags": statement.excluded.tags,
                    "retrieved_at": statement.excluded.retrieved_at,
                },
            )
        )
    # Features no longer returned for two days are considered gone.
    await session.execute(delete(OsmFeature).where(OsmFeature.retrieved_at < now - OSM_STALE_AFTER))
    return len(records)


async def cleanup(session: AsyncSession, now: datetime) -> int:
    removed = 0
    for statement in (
        delete(Measurement).where(Measurement.observed_at < now - MEASUREMENT_RETENTION),
        delete(ModelForecast).where(ModelForecast.valid_at < now - FORECAST_RETENTION),
        delete(WeatherHour).where(WeatherHour.valid_at < now - FORECAST_RETENTION),
        delete(GridSnapshot).where(GridSnapshot.computed_at < now - GRID_RETENTION),
        delete(SourceRun).where(
            SourceRun.started_at < now - RUN_RETENTION, SourceRun.source.not_in(BILLED_SOURCES)
        ),
        delete(SourceRun).where(SourceRun.started_at < now - BILLED_RUN_RETENTION),
        delete(Conversation).where(Conversation.updated_at < now - CONVERSATION_RETENTION),
    ):
        removed += (await session.execute(statement)).rowcount or 0
    return removed


async def _not_configured(session: AsyncSession, now: datetime) -> int:
    raise SourceNotConfigured


class Ingestor:
    """Runs each source with a run log; a failing source never affects the others."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], http: httpx.AsyncClient, settings: Settings
    ):
        self.sessionmaker, self.http, self.settings = sessionmaker, http, settings

    async def run(self, source: str, job: Callable[[AsyncSession, datetime], Awaitable[int]]) -> SourceRun:
        now = datetime.now(UTC)
        async with self.sessionmaker() as session:
            run = SourceRun(source=source, started_at=now)
            session.add(run)
            await session.commit()
            try:
                run.records = await job(session, now)
                run.status = "ok"
            except SourceNotConfigured:
                await session.rollback()
                run.status, run.error = "skipped", "not_configured"
            except SourceSkipped as skip:
                await session.rollback()
                run.status, run.error = "skipped", skip.reason
            except SourceError as error:
                await session.rollback()
                run.status, run.error = "failed", error.code
                log.warning("Source %s failed: %s", source, error.code)
            except Exception:
                await session.rollback()
                run.status, run.error = "failed", "internal_error"
                log.exception("Source %s crashed", source)
            run.finished_at = datetime.now(UTC)
            session.add(run)
            await session.commit()
            return run

    async def sensor_community(self) -> SourceRun:
        async def job(session, now):
            readings = await sensor_community.fetch(self.http, self.settings.city_bbox)
            return await store_readings(session, readings, now)

        return await self.run("sensor_community", job)

    async def openaq(self) -> SourceRun:
        async def job(session, now):
            if not self.settings.openaq_api_key:
                raise SourceNotConfigured
            center = (self.settings.city_center_lat, self.settings.city_center_lon)
            readings = await openaq.fetch(
                self.http, self.settings.openaq_api_key, center, self.settings.city_bbox
            )
            return await store_readings(session, readings, now)

        return await self.run("openaq", job)

    async def cams(self) -> SourceRun:
        async def job(session, now):
            values = await open_meteo.fetch_cams(self.http, self.settings.city_bbox)
            return await store_forecasts(session, values, now)

        return await self.run("cams", job)

    async def weather(self) -> SourceRun:
        async def job(session, now):
            records = await open_meteo.fetch_weather(
                self.http, self.settings.city_center_lat, self.settings.city_center_lon
            )
            return await store_weather(session, records, now)

        return await self.run("weather", job)

    async def osm(self) -> SourceRun:
        async def job(session, now):
            return await store_osm(session, await osm.fetch(self.http, self.settings.city_bbox), now)

        return await self.run("osm", job)

    async def traffic(self) -> SourceRun | None:
        """HERE is billed per request: hard monthly cap, slower polling at night, no retries."""
        settings = self.settings
        if not settings.here_api_key:
            return await self.run("here_traffic", _not_configured)
        now = datetime.now(UTC)
        local = now.astimezone(settings.tz)
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with self.sessionmaker() as session:
            used, last = await billed_requests(session, "here_traffic", month_start)
        if (
            is_night(local.hour)
            and last
            and now - last < timedelta(minutes=settings.here_night_every_min - 1)
        ):
            return None  # quiet hours: no request, nothing to log
        cap_reached = used >= settings.here_monthly_cap

        async def job(session, now):
            if cap_reached:
                log.warning(
                    "HERE monthly cap reached (%d/%d): traffic paused", used, settings.here_monthly_cap
                )
                raise SourceSkipped("monthly_cap_reached")
            observed_at, segments = await here_traffic.fetch(
                self.http, self.settings.here_api_key, self.settings.city_bbox
            )
            return await traffic_service.store(session, observed_at, segments, now)

        return await self.run("here_traffic", job)

    async def cleanup(self) -> SourceRun:
        return await self.run("cleanup", cleanup)


async def latest_runs(session: AsyncSession) -> dict[str, SourceRun]:
    """The most recent finished run of each source."""
    newest = (
        select(SourceRun.source, func.max(SourceRun.started_at).label("started_at"))
        .where(SourceRun.status != "running")
        .group_by(SourceRun.source)
        .subquery()
    )
    rows = await session.scalars(
        select(SourceRun).join(
            newest, (SourceRun.source == newest.c.source) & (SourceRun.started_at == newest.c.started_at)
        )
    )
    return {run.source: run for run in rows}


async def last_success(session: AsyncSession) -> dict[str, datetime]:
    rows = await session.execute(
        select(SourceRun.source, func.max(SourceRun.finished_at))
        .where(SourceRun.status == "ok")
        .group_by(SourceRun.source)
    )
    return {source: finished for source, finished in rows}
