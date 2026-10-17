import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.session import Session
from app.integrations.cams import CamsAirQualityProvider
from app.integrations.http import ProviderError, ProviderHTTP
from app.integrations.official_works import OfficialWorksProvider
from app.integrations.openaq import OpenAQProvider
from app.integrations.openstreetmap import OpenStreetMapFeaturesProvider
from app.integrations.sensor_community import SensorCommunityProvider
from app.integrations.weather import OpenMeteoWeatherProvider
from app.models.entities import (
    DataSource,
    IngestionRun,
    MappedFeature,
    Observation,
    PlannedWork,
    Station,
    WeatherForecast,
    WeatherObservation,
)


class SourceService:
    def __init__(self, settings, http=None):
        self.settings = settings
        self.http = http or ProviderHTTP(settings)
        self.providers = {
            p.name: p(settings, self.http)
            for p in (
                SensorCommunityProvider,
                OpenAQProvider,
                OpenMeteoWeatherProvider,
                CamsAirQualityProvider,
                OpenStreetMapFeaturesProvider,
                OfficialWorksProvider,
            )
        }
        self.locks = {name: asyncio.Lock() for name in self.providers}

    def configuration(self, name):
        enabled = {
            "sensor_community": self.settings.sensor_community_enabled,
            "openaq": self.settings.openaq_enabled,
            "weather": self.settings.openmeteo_weather_enabled,
            "cams": self.settings.openmeteo_air_quality_enabled,
            "osm": self.settings.osm_features_enabled,
            "official_works": self.settings.official_works_enabled,
        }[name]
        missing = (name == "openaq" and not self.settings.openaq_api_key) or (
            name == "official_works" and not self.settings.official_works_feed_url
        )
        return enabled, bool(missing)

    async def initialize(self):
        async with Session() as db:
            for name in self.providers:
                source = await db.get(DataSource, name) or DataSource(name=name)
                source.enabled, source.configuration_missing = self.configuration(name)
                db.add(source)
            await db.commit()

    async def sync(self, name):
        if name not in self.providers:
            raise ProviderError("unknown_source")
        async with self.locks[name]:
            enabled, missing = self.configuration(name)
            if not enabled:
                return {"status": "disabled", "source": name}
            async with Session() as db:
                source = await db.get(DataSource, name) or DataSource(name=name)
                source.enabled, source.configuration_missing = enabled, missing
                source.last_attempt = datetime.now(UTC)
                run = IngestionRun(source=name)
                db.add_all([source, run])
                await db.commit()
                run_id = run.id
            try:
                if missing:
                    raise ProviderError("missing_configuration")
                records = await self.providers[name].fetch()
                async with Session() as db:
                    for record in records:
                        if name == "osm":
                            existing = await db.scalar(
                                select(MappedFeature).where(MappedFeature.source_id == record["source_id"])
                            )
                            if existing:
                                for key, value in record.items():
                                    setattr(existing, key, value)
                            else:
                                db.add(MappedFeature(**record))
                        elif name == "official_works":
                            existing = await db.scalar(
                                select(PlannedWork).where(PlannedWork.source_id == record["source_id"])
                            )
                            if existing:
                                # Material publication changes invalidate prior review, retaining reported dates.
                                changed = (
                                    existing.description != record["description"]
                                    or existing.title != record["title"]
                                )
                                existing.title, existing.description = record["title"], record["description"]
                                existing.retrieved_at = record["retrieved_at"]
                                if changed:
                                    existing.verification = "needs_review"
                            else:
                                db.add(PlannedWork(**record))
                        else:
                            record.canonical_sensor_id = self.settings.source_sensor_aliases.get(
                                f"{record.provider}:{record.sensor_id}", record.canonical_sensor_id
                            )
                            model = WeatherForecast if name == "weather" else Observation
                            if record.provenance.get("product") == "reanalysis":
                                model = WeatherObservation
                            await db.merge(model(id=record.id, **record.model_dump()))
                    if name == "openaq":
                        for station in self.providers[name].stations:
                            coords = station.get("coordinates") or {}
                            if "latitude" not in coords or "longitude" not in coords:
                                continue
                            existing = await db.scalar(
                                select(Station).where(
                                    Station.provider == name, Station.external_id == str(station["id"])
                                )
                            )
                            values = {
                                "provider": name,
                                "external_id": str(station["id"]),
                                "name": station.get("name", ""),
                                "latitude": coords["latitude"],
                                "longitude": coords["longitude"],
                                "details": station,
                            }
                            if existing:
                                for key, value in values.items():
                                    setattr(existing, key, value)
                            else:
                                db.add(Station(**values))
                    source = await db.get(DataSource, name)
                    source.last_success = datetime.now(UTC)
                    source.last_error = None
                    source.records_processed = len(records)
                    stamps = [r.observed_at for r in records if hasattr(r, "observed_at")]
                    if stamps:
                        source.latest_observation_at = max(stamps)
                    run = await db.get(IngestionRun, run_id)
                    run.finished_at, run.status = datetime.now(UTC), "success"
                    run.discovered = run.updated = len(records)
                    await db.commit()
                return {"status": "success", "source": name, "processed": len(records)}
            except Exception as exc:
                code = exc.code if isinstance(exc, ProviderError) else "normalization_or_storage_failed"
                async with Session() as db:
                    source = await db.get(DataSource, name)
                    source.last_error = code
                    run = await db.get(IngestionRun, run_id)
                    run.finished_at, run.status, run.error = datetime.now(UTC), "failed", code
                    await db.commit()
                return {"status": "failed", "source": name, "error": code}
