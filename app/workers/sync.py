import asyncio
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.db.session import Base, engine
from app.services.sources import SourceService


async def main():
    settings = get_settings()
    if engine.dialect.name == "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    service = SourceService(settings)
    await service.initialize()
    if not settings.source_sync_enabled:
        await service.http.close()
        return
    intervals = {
        "sensor_community": settings.sensor_community_sync_interval_minutes,
        "openaq": settings.openaq_sync_interval_minutes,
        "weather": settings.weather_sync_interval_minutes,
        "cams": settings.cams_sync_interval_minutes,
        "osm": settings.osm_sync_interval_hours * 60,
        "osm_roads": settings.osm_roads_sync_interval_hours * 60,
        "official_works": settings.official_works_sync_interval_hours * 60,
    }
    scheduler = AsyncIOScheduler(timezone="UTC")
    for index, (name, minutes) in enumerate(intervals.items()):
        if service.configuration(name)[0]:
            from datetime import UTC, datetime, timedelta

            scheduler.add_job(
                service.sync,
                "interval",
                minutes=minutes,
                args=[name],
                id=name,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC) + timedelta(seconds=3 + index * 5),
            )
    scheduler.start()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await service.http.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
