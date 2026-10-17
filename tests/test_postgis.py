"""Opt-in test for an isolated PostgreSQL/PostGIS database; never points at the application database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.skipif(not os.environ.get("TEST_POSTGIS_URL"), reason="Requires isolated TEST_POSTGIS_URL")
async def test_postgis_migration_and_spatial_query():
    url = os.environ["TEST_POSTGIS_URL"]
    if url == os.environ.get("DATABASE_URL") or "test" not in url.rsplit("/", 1)[-1]:
        pytest.fail("Use a dedicated database with test in its name")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": url, "APP_ENV": "test"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", "head"],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            assert "POSTGIS" in (await db.scalar(text("SELECT PostGIS_Full_Version()"))).upper()
            assert await db.scalar(
                text(
                    "SELECT ST_DWithin(ST_SetSRID(ST_MakePoint(27.6,47.15),4326)::geography, ST_SetSRID(ST_MakePoint(27.6001,47.1501),4326)::geography,100)"
                )
            )
            indexes = (
                (await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='reports'")))
                .scalars()
                .all()
            )
            assert "idx_reports_location" in indexes
            assert (
                await db.scalar(
                    text(
                        "SELECT data_type FROM information_schema.columns WHERE table_name='reports' AND column_name='location'"
                    )
                )
                == "USER-DEFINED"
            )
    finally:
        await engine.dispose()
