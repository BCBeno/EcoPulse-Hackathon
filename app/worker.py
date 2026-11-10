"""Background process: syncs the data sources and recomputes the pollution grid.

Run exactly one instance:  uv run python -m app.worker
"""

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.air.ingest import Ingestor
from app.air.service import refresh_now_grid
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.http import create_http_client
from app.logs import configure_logging

log = logging.getLogger("ecopulse.worker")


async def refresh_grid(sessionmaker, settings: Settings) -> None:
    try:
        async with sessionmaker() as session:
            snapshot = await refresh_now_grid(session, settings)
        log.info("Grid refreshed (%s)", snapshot.inputs)
    except Exception:
        log.exception("Grid refresh failed")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    http = create_http_client(settings)
    ingest = Ingestor(sessionmaker, http, settings)

    async def sensors_then_grid() -> None:
        await ingest.sensor_community()
        await refresh_grid(sessionmaker, settings)

    async def cams_then_grid() -> None:
        await ingest.cams()
        await refresh_grid(sessionmaker, settings)

    scheduler = AsyncIOScheduler(timezone="UTC")
    start = datetime.now(UTC) + timedelta(seconds=2)
    jobs = [
        (cams_then_grid, {"minutes": settings.cams_every_min}, 0),
        (ingest.weather, {"minutes": settings.weather_every_min}, 5),
        (sensors_then_grid, {"minutes": settings.sensor_community_every_min}, 10),
        (ingest.openaq, {"minutes": settings.openaq_every_min}, 15),
        (ingest.traffic, {"minutes": settings.traffic_every_min}, 20),
        # Hourly attempts; the job itself waits OSM_EVERY_H after a success.
        (ingest.osm, {"hours": 1}, 30),
        (ingest.cleanup, {"hours": 6}, 60),
    ]
    if settings.sync_enabled:
        for job, interval, delay in jobs:
            scheduler.add_job(
                job,
                "interval",
                **interval,
                next_run_time=start + timedelta(seconds=delay),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
    scheduler.start()
    log.info("Worker started (%d jobs)", len(scheduler.get_jobs()))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await http.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
