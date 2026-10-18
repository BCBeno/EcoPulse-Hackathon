"""Import RNMCA Excel reports downloaded from calitateaer.ro.

    python -m app.workers.import_rnmca REPORT.xls [REPORT.xls ...] [--csv DIR] [--no-db] [--stations FILE.json]

Values reports ("Raport valori date") are stored in the database (hourly measurements) and, with --csv,
exported in full in long format. Index reports ("Raport indici de calitate") are exported to CSV only.
Prints one JSON summary per file; exits with status 1 if any file could not be imported.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.db.session import Base, Session, engine
from app.integrations.rnmca_excel import IndicesReport, RnmcaFormatError, parse_report
from app.models.entities import DataSource, IngestionRun
from app.services.rnmca_import import (
    SOURCE,
    STATIONS,
    import_values,
    write_indices_csv,
    write_values_csv,
)


def load_stations(path):
    if path is None:
        return STATIONS
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    stations = {**STATIONS}
    for external_id, meta in data.items():
        if not {"latitude", "longitude"} <= meta.keys():
            raise SystemExit(f"--stations: {external_id} needs latitude and longitude")
        stations[external_id] = {
            **STATIONS.get(external_id, {"name": external_id}),
            **meta,
            "coordinates_source": meta.get("coordinates_source", f"--stations {Path(path).name}"),
        }
    return stations


async def record_failure(error):
    async with Session() as db:
        source = await db.get(DataSource, SOURCE) or DataSource(name=SOURCE, records_processed=0)
        source.last_attempt, source.last_error = datetime.now(UTC), error[:250]
        db.add_all(
            [
                source,
                IngestionRun(
                    source=SOURCE, finished_at=datetime.now(UTC), status="failed", error=error[:250]
                ),
            ]
        )
        await db.commit()


async def import_file(path, args, stations):
    summary = {"file": str(path)}
    try:
        report = parse_report(str(path))
    except (RnmcaFormatError, OSError) as exc:
        return {**summary, "status": "failed", "error": str(exc)}
    is_index = isinstance(report, IndicesReport)
    summary.update(type="indices" if is_index else "values", interval=report.interval, rows=report.rows)
    summary["warnings"] = report.warnings
    if args.csv:
        out = Path(args.csv) / f"{path.stem}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = write_indices_csv(report, out) if is_index else write_values_csv(report, out)
        summary["csv"] = {"path": str(out), "rows": rows}
    if is_index:
        summary["stations"] = sorted({v.station for v in report.values})
        summary["note"] = "index reports are exported to CSV only"
    else:
        summary["stations"] = sorted({c.station for c in report.columns})
        summary["parameters"] = sorted({c.parameter for c in report.columns})
        if not args.no_db:
            try:
                async with Session() as db:
                    stored = await import_values(
                        db, report, path.name, stations, get_settings().source_sensor_aliases, args.parameters
                    )
            except Exception as exc:  # recorded for /admin/data-sources, then reported
                await record_failure(f"{path.name}: {type(exc).__name__}")
                return {**summary, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
            summary["database"] = stored
    return {**summary, "status": "success"}


async def main(argv=None):
    parser = argparse.ArgumentParser(description="Import RNMCA (calitateaer.ro) Excel reports.")
    parser.add_argument("reports", nargs="+", type=Path, help=".xls files exported from calitateaer.ro")
    parser.add_argument("--csv", metavar="DIR", help="also write <report>.csv files to this directory")
    parser.add_argument(
        "--no-db", action="store_true", help="parse (and export) without touching the database"
    )
    parser.add_argument("--stations", metavar="FILE.json", help="station coordinates overriding the defaults")
    parser.add_argument(
        "--parameters",
        metavar="LIST",
        type=lambda text: {p.strip() for p in text.split(",") if p.strip()},
        help="comma-separated parameters to store (e.g. pm25,pm10,no2,o3,so2); default: all",
    )
    args = parser.parse_args(argv)
    stations = load_stations(args.stations)
    if not args.no_db and engine.dialect.name == "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    failed = False
    try:
        for path in args.reports:
            summary = await import_file(path, args, stations)
            failed |= summary["status"] != "success"
            print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    finally:
        await engine.dispose()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
