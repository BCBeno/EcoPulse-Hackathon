"""Import of RNMCA Excel reports (calitateaer.ro).

- Hourly measurements ("Valori orare") become air_observations rows: OFFICIAL_MEASUREMENT for pollutants,
  WEATHER_CONTEXT for the IS-2 meteorological mast (MTX). Missing values ("-") are not stored.
- Daily/annual aggregates, moving averages and the daily index report go to CSV only: stored as observations
  they would mix with hourly readings in /environment/observations queries.
- Each hourly value is classified on the RNMCA scale (PM2.5, PM10, NO2, O3, SO2). The official daily index is
  the worst hourly class of the day; checked against raport-indici-2026.xls (98-99% agreement, 91% for NO2).
"""

import csv
from collections import Counter
from datetime import UTC, datetime
from itertools import chain

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.integrations.rnmca_excel import IndicesReport, ValuesReport, key
from app.models.entities import DataSource, IngestionRun, Observation, Station
from app.schemas.domain import Category, ObservationData, normalize_value
from app.services.air_quality_service import AQI_THRESHOLDS, AirQualityService

SOURCE = "rnmca"
ATTRIBUTION = "Rețeaua Națională de Monitorizare a Calității Aerului (RNMCA), calitateaer.ro"
BATCH = 500

# The reports carry no coordinates. Geocoded on 2026-10-18 from the addresses published by calitateaer.ro
# (Nominatim and the Overpass junction of the two roads for IS-1), accurate to about 100 m.
# Replace with the official EEA station metadata if available (--stations FILE.json).
STATIONS = {
    "IS-1": {
        "name": "IS-1 Podu de Piatră",
        "address": "Bd. Nicolae Iorga × Șoseaua Națională",
        "type": "trafic",
        "latitude": 47.1571,
        "longitude": 27.5747,
    },
    "IS-2": {
        "name": "IS-2 Decebal-Cantemir",
        "address": "Aleea Decebal nr. 10",
        "type": "fond urban, cu stație meteo",
        "latitude": 47.1511,
        "longitude": 27.5831,
    },
    "IS-3": {
        "name": "IS-3 Oancea-Tătărași",
        "address": "Str. Han Tătar nr. 14",
        "type": "industrial",
        "latitude": 47.1581,
        "longitude": 27.6130,
    },
}
COORDINATES_SOURCE = "geocoded from the station address (OpenStreetMap), ±100 m"

CLASSIFIED = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2", "o3": "O3", "so2": "SO2"}
LABELS = {code: label for _, _, code, label in AQI_THRESHOLDS["PM2.5"]}

UPDATED_COLUMNS = (
    "provider",
    "source_record_id",
    "sensor_id",
    "canonical_sensor_id",
    "parameter",
    "value",
    "original_value",
    "original_unit",
    "unit",
    "observed_at",
    "retrieved_at",
    "latitude",
    "longitude",
    "category",
    "quality",
    "provenance",
)


def rnmca_code(parameter, value):
    """RNMCA class of one hourly concentration, or None for unclassified parameters and missing values."""
    if parameter not in CLASSIFIED or value is None:
        return None
    return AirQualityService.get_aqi_badge(CLASSIFIED[parameter], value)["code"] or None


def meteo_units(parameter, value, unit):
    # Same units as the Sensor.Community weather context already stored (pressure in Pa).
    if parameter == "pressure" and key(unit) in {"mbar", "hpa"}:
        return value * 100, "Pa"
    if parameter == "wind_direction" and key(unit) == "grn":
        return value, "°"
    return value, unit


def observations(
    report: ValuesReport, stations, report_name, aliases=None, retrieved_at=None, parameters=None
):
    """Hourly values as ObservationData, plus counts of what was left out and why.

    `parameters` (a set of parameter names) limits the import; None imports everything.
    """
    aliases = aliases or {}
    retrieved_at = retrieved_at or datetime.now(UTC)
    records, missing, unknown = [], 0, Counter()
    for item in report.hourly:
        column = item.column
        if column.metric != "hourly" or (parameters is not None and column.parameter not in parameters):
            continue
        if item.value is None:
            missing += 1
            continue
        station = stations.get(column.station)
        if station is None:
            unknown[column.station] += 1
            continue
        if column.kind == "meteo":
            converted, unit = meteo_units(column.parameter, item.value, column.unit)
            _, value, unit = normalize_value(converted, unit, column.parameter)
        else:
            _, value, unit = normalize_value(item.value, column.unit, column.parameter)
        sensor_id = f"{column.station}:{column.instrument or column.compound}"
        records.append(
            ObservationData(
                provider=SOURCE,
                source_record_id=f"{column.station}:{column.instrument}:{column.compound}:{item.label}",
                sensor_id=sensor_id,
                canonical_sensor_id=aliases.get(f"{SOURCE}:{sensor_id}"),
                parameter=column.parameter,
                original_value=item.value,
                original_unit=column.unit,
                value=value,
                unit=unit,
                observed_at=item.period_start,
                retrieved_at=retrieved_at,
                latitude=station["latitude"],
                longitude=station["longitude"],
                category=Category.WEATHER if column.kind == "meteo" else Category.OFFICIAL,
                quality={
                    "valid": value is not None,
                    "averaging": "1h",
                    "rnmca_code": rnmca_code(column.parameter, value),
                },
                # Kept short: ~600k rows per year. Attribution lives on the station; the hour ends at
                # observed_at + 1 h ("averaging" in quality).
                provenance={
                    "product": "measurement",
                    "source": "RNMCA",
                    "report": report_name,
                    "instrument": column.instrument,
                    "local_label": item.label,
                },
            )
        )
    return records, missing, unknown


async def upsert_observations(db, records):
    dialect = db.get_bind().dialect.name
    insert = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}.get(dialect)
    if insert is None:
        for record in records:
            await db.merge(Observation(id=record.id, **record.model_dump()))
        return
    for start in range(0, len(records), BATCH):
        rows = [{"id": r.id, **r.model_dump()} for r in records[start : start + BATCH]]
        statement = insert(Observation).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[Observation.id],
            set_={name: getattr(statement.excluded, name) for name in UPDATED_COLUMNS},
        )
        await db.execute(statement)


async def upsert_stations(db, stations, used):
    for external_id in sorted(used):
        meta = stations[external_id]
        values = {
            "provider": SOURCE,
            "external_id": external_id,
            "name": meta["name"],
            "latitude": meta["latitude"],
            "longitude": meta["longitude"],
            "details": {
                "address": meta.get("address"),
                "type": meta.get("type"),
                "coordinates_source": meta.get("coordinates_source", COORDINATES_SOURCE),
                "attribution": ATTRIBUTION,
            },
        }
        existing = await db.scalar(
            select(Station).where(Station.provider == SOURCE, Station.external_id == external_id)
        )
        if existing:
            for name, value in values.items():
                setattr(existing, name, value)
        else:
            db.add(Station(**values))


async def import_values(db, report: ValuesReport, report_name, stations=None, aliases=None, parameters=None):
    """Store the hourly measurements of a values report; returns a summary. Idempotent per report."""
    stations = stations or STATIONS
    started = datetime.now(UTC)
    records, missing, unknown = observations(report, stations, report_name, aliases, started, parameters)
    run = IngestionRun(source=SOURCE)
    db.add(run)
    await upsert_observations(db, records)
    await upsert_stations(db, stations, {r.sensor_id.split(":", 1)[0] for r in records})

    source = await db.get(DataSource, SOURCE) or DataSource(name=SOURCE, records_processed=0)
    latest = max((r.observed_at for r in records), default=None)
    previous = source.latest_observation_at
    if previous is not None and previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    source.enabled, source.configuration_missing = True, False
    source.last_attempt = source.last_success = datetime.now(UTC)
    source.latest_observation_at = max(filter(None, (previous, latest)), default=None)
    source.records_processed = (source.records_processed or 0) + len(records)
    source.last_error = None
    run.finished_at, run.status = datetime.now(UTC), "success"
    run.discovered = len(records) + missing + sum(unknown.values())
    run.updated, run.skipped = len(records), missing + sum(unknown.values())
    db.add(source)
    await db.commit()
    return {
        "stored": len(records),
        "invalid": sum(r.value is None for r in records),
        "missing": missing,
        "unknown_stations": dict(unknown),
        "first_observed_at": min((r.observed_at for r in records), default=None),
        "last_observed_at": latest,
    }


# ---------------------------------------------------------------- CSV

VALUE_FIELDS = [
    "station",
    "station_name",
    "instrument",
    "compound",
    "parameter",
    "metric",
    "period_start_utc",
    "period_end_utc",
    "local_label",
    "value",
    "unit",
    "rnmca_code",
    "rnmca_label",
]
INDEX_FIELDS = ["station", "station_name", "compound", "parameter", "date", "rnmca_code", "rnmca_label"]


def iso(when):
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_values_csv(report: ValuesReport, path):
    """Every cell of the report in long format; missing values are empty. Returns the row count."""
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(VALUE_FIELDS)
        for item in chain(report.hourly, report.aggregates):
            column = item.column
            code = rnmca_code(column.parameter, item.value) if column.metric == "hourly" else None
            writer.writerow(
                [
                    column.station,
                    column.station_name or "",
                    column.instrument,
                    column.compound,
                    column.parameter,
                    column.metric,
                    iso(item.period_start),
                    iso(item.period_end),
                    item.label,
                    "" if item.value is None else repr(item.value),
                    column.unit,
                    code or "",
                    LABELS.get(code, "") if code else "",
                ]
            )
            count += 1
    return count


def write_indices_csv(report: IndicesReport, path):
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(INDEX_FIELDS)
        for item in report.values:
            writer.writerow(
                [
                    item.station,
                    item.station_name or "",
                    item.compound,
                    item.parameter,
                    item.day.isoformat(),
                    item.code or "",
                    LABELS.get(item.code, "") if item.code else "",
                ]
            )
            count += 1
    return count
