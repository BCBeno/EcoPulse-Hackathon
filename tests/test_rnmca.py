"""RNMCA Excel reports. Sheets are built in memory with the layout of real calitateaer.ro exports
(header in row 8, station written once per column group); all values are synthetic."""

import csv
import json
from datetime import UTC, date, datetime

import pytest
import xlrd
from sqlalchemy import func, select

from app.db.session import Session
from app.integrations import rnmca_excel
from app.integrations.rnmca_excel import (
    IndicesReport,
    RnmcaFormatError,
    ValuesReport,
    hour_period,
    parse_book,
    parse_report,
)
from app.models.entities import DataSource, IngestionRun, Observation, Station
from app.services.rnmca_import import import_values, write_indices_csv, write_values_csv
from app.workers import import_rnmca


class FakeSheet:
    def __init__(self, rows, name="Raport valori date"):
        self.name, self.rows = name, rows
        self.nrows, self.ncols = len(rows), max(len(r) for r in rows)

    def cell_value(self, row, col):
        cells = self.rows[row]
        return cells[col] if col < len(cells) else ""

    def cell_type(self, row, col):
        value = self.cell_value(row, col)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return xlrd.XL_CELL_NUMBER
        return xlrd.XL_CELL_EMPTY if value == "" else xlrd.XL_CELL_TEXT


class FakeBook:
    def __init__(self, *sheets):
        self._sheets = sheets

    def sheet_by_index(self, index):
        return self._sheets[index]

    def sheets(self):
        return list(self._sheets)


PREAMBLE = [
    ["", "Public"],
    [""],
    [""],
    [""],
    ["Raport valori date"],
    ["Interval:2025/10/26 00:00:00 - x"],
    [""],
]
HEADER = [
    ["Zi/Ora", "IS-1", "", "", "IS-2", "", "IS-9"],
    ["", "Derenda APM2 - PM 2.5", "", "9830B - CO", "MTX - Presiunea aerului", "9841B - NO2", "9841B - NO2"],
    ["", "Valori orare", "Valori zilnice", "Valori orare", "Valori orare", "Valori orare", "Valori orare"],
    [
        "",
        "Valoare [µg/m³]",
        "Valoare [µg/m³]",
        "Valoare [mg/m³]",
        "Valoare [mbar]",
        "Valoare [µg/m³]",
        "Valoare [µg/m³]",
    ],
]
# Autumn DST change (2025-10-26): 24 labels for 25 real hours; label 03 is the summer-time 03:00.
DATA = [
    ["2025-10-26 02", 12.5, 20.1, 0.5, 1013.2, 30.0, 1.0],
    ["2025-10-26 03", "-", 20.1, "-", "-", -1.0, 1.0],
    ["2025-10-26 04", 16.0, 20.2, 0.4, 1013.0, 26.0, 1.0],
    ["2025-10-26 24", 4.0, 20.1, "-", "-", "-", "-"],
    ["2025-10-27 01", 60.0, 30.2, "-", "-", "-", "-"],
    ["2025-10-26 25", 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
]


def values_book(data=DATA):
    return FakeBook(FakeSheet(PREAMBLE + HEADER + data))


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_values_header_columns_and_forward_fill():
    report = parse_book(values_book())
    assert isinstance(report, ValuesReport)
    assert report.interval == "2025/10/26 00:00:00 - x"
    first, daily, co, pressure, no2, unknown = report.columns
    assert (first.station, first.instrument, first.compound, first.parameter) == (
        "IS-1",
        "Derenda APM2",
        "PM 2.5",
        "pm25",
    )
    assert (daily.station, daily.parameter, daily.metric) == ("IS-1", "pm25", "daily")
    assert (co.parameter, co.unit) == ("co", "mg/m³")
    assert (pressure.station, pressure.kind, pressure.parameter, pressure.unit) == (
        "IS-2",
        "meteo",
        "pressure",
        "mbar",
    )
    assert (no2.station, no2.parameter, no2.kind) == ("IS-2", "no2", "pollutant")
    assert unknown.station == "IS-9"


def test_hour_labels_are_local_hour_ending_with_dst():
    hours = [v for v in parse_book(values_book()).hourly if v.column.parameter == "pm25"]
    assert [(v.label, v.period_start, v.period_end, v.value) for v in hours] == [
        ("2025-10-26 02", utc(2025, 10, 25, 22), utc(2025, 10, 25, 23), 12.5),
        ("2025-10-26 03", utc(2025, 10, 25, 23), utc(2025, 10, 26, 0), None),
        ("2025-10-26 04", utc(2025, 10, 26, 1), utc(2025, 10, 26, 2), 16.0),
        ("2025-10-26 24", utc(2025, 10, 26, 21), utc(2025, 10, 26, 22), 4.0),
        ("2025-10-27 01", utc(2025, 10, 26, 22), utc(2025, 10, 26, 23), 60.0),
    ]


def test_spring_forward_day_is_continuous_and_rejects_missing_hour():
    assert hour_period("2026-03-29 02")[1] == utc(2026, 3, 29, 0)
    assert hour_period("2026-03-29 04")[1] == utc(2026, 3, 29, 1)
    assert hour_period("2026-03-29 03") is None  # 03:00 does not exist that night
    assert hour_period("2026-01-01 24")[1:] == (utc(2026, 1, 1, 22), date(2026, 1, 1))
    for label in ("2026-01-01 00", "2026-01-01 25", "2026-02-30 05", "2026/01/01 05", ""):
        assert hour_period(label) is None


def test_bad_labels_warn_and_daily_values_are_deduplicated():
    report = parse_book(values_book())
    assert report.rows == 5
    assert any("unusable time label '2025-10-26 25'" in w for w in report.warnings)
    assert any("1 repeated daily/annual cells disagreed" in w for w in report.warnings)
    daily = sorted((v.label, v.value, v.period_start, v.period_end) for v in report.aggregates)
    # 2025-10-26 has 25 hours: 00:00 EEST to 24:00 EET.
    assert daily == [
        ("2025-10-26", 20.1, utc(2025, 10, 25, 21), utc(2025, 10, 26, 22)),
        ("2025-10-27", 30.2, utc(2025, 10, 26, 22), utc(2025, 10, 27, 22)),
    ]


def test_unknown_compounds_and_metrics_are_kept_with_warning():
    header = [row[:2] for row in HEADER]
    header[1][1], header[2][1] = "XYZ 1 - Ceva nou", "Valori orare"
    report = parse_book(FakeBook(FakeSheet(PREAMBLE + header + [["2025-01-01 01", 3.0]])))
    assert report.columns[0].parameter == "ceva_nou"
    assert any("Ceva nou" in w for w in report.warnings)


def test_diacritics_and_alternate_labels_match():
    header = [row[:2] for row in HEADER]
    header[1][1], header[2][1] = "MTX - Temperatură aer", "Valori orare"
    report = parse_book(FakeBook(FakeSheet(PREAMBLE + header + [["2025-01-01 01", -3.5]])))
    assert (report.columns[0].parameter, report.columns[0].kind) == ("temperature", "meteo")
    assert report.hourly[0].value == -3.5


@pytest.mark.parametrize(
    "rows",
    [
        [["no header here"]],
        PREAMBLE + [["Zi/Ora", "IS-1"]],
        PREAMBLE + [["Zi/Ora"], [""], [""], [""], ["2025-01-01 01"]],
    ],
)
def test_malformed_reports(rows):
    with pytest.raises(RnmcaFormatError):
        parse_book(FakeBook(FakeSheet(rows)))


def test_unreadable_file_is_a_format_error():
    with pytest.raises(RnmcaFormatError):
        parse_report(b"PK\x03\x04 this is not an .xls workbook")


def indices_book():
    index = FakeSheet(
        [
            ["", "Public"],
            [""],
            [""],
            ["", "", "", "1 – Bun"],
            ["Raport indici de calitate"],
            ["Interval:2026/01/01 00:00:00 - 2026/01/03 00:00:00"],
            [""],
            ["Data/Timp", "IS-1 – Bvd. Nicolae Iorga + Soseaua Nationala", "", "IS-2 – Aleea Decebal"],
            ["", "NO2", "PM 10", "O3"],
            ["", "Zilnic", "Zilnic", "Zilnic"],
            ["2026/01/01", "4", "3", "1"],
            ["2026/01/02", "-", "7", "2"],
        ],
        name="Raport indici de calitate",
    )
    legend = FakeSheet(
        [
            ["", "Public"],
            ["-", "Date lipsă"],
            [""],
            ["NO2"],
            ["Valoare minima[µg/m³]", "Valoare maxima[µg/m³]", "Cod", "Descriere"],
            ["0.0", "10.0", 1.0, "Bun"],
            ["10.0", "25.0", 2.0, "Acceptabil"],
            [""],
            ["PM 10"],
            ["Valoare minima[µg/m³]", "Valoare maxima[µg/m³]", "Cod", "Descriere"],
            ["0.0", "15.0", 1.0, "Bun"],
        ],
        name="Legenda indici de calitate",
    )
    return FakeBook(index, legend)


def test_indices_report_and_legend():
    report = parse_book(indices_book())
    assert isinstance(report, IndicesReport)
    assert report.rows == 2
    by = {(v.station, v.parameter, v.day): v.code for v in report.values}
    assert by[("IS-1", "no2", date(2026, 1, 1))] == 4
    assert by[("IS-1", "pm10", date(2026, 1, 1))] == 3
    assert by[("IS-2", "o3", date(2026, 1, 2))] == 2
    assert by[("IS-1", "no2", date(2026, 1, 2))] is None  # "-"
    assert by[("IS-1", "pm10", date(2026, 1, 2))] is None  # 7 is off the scale
    assert report.values[0].station_name == "Bvd. Nicolae Iorga + Soseaua Nationala"
    assert any("outside the 1-6 scale" in w for w in report.warnings)
    assert report.thresholds == {
        "no2": [(0.0, 10.0, 1, "Bun"), (10.0, 25.0, 2, "Acceptabil")],
        "pm10": [(0.0, 15.0, 1, "Bun")],
    }


def run_import(client, report, name="raport.xls"):
    async def go():
        async with Session() as db:
            return await import_values(db, report, name)

    return client.portal.call(go)


def query(client, statement):
    async def go():
        async with Session() as db:
            return (await db.execute(statement)).all()

    return client.portal.call(go)


def stations_attribution(client):
    return {
        s.details["attribution"] for (s,) in query(client, select(Station).where(Station.provider == "rnmca"))
    }


def test_import_values_into_database(client):
    report = parse_book(values_book())
    summary = run_import(client, report)
    # IS-1: 4 PM2.5 + 2 CO, IS-2: 2 pressure + 3 NO2 values; IS-9 has no coordinates and is skipped.
    assert summary["stored"] == 11
    assert summary["unknown_stations"] == {"IS-9": 3}
    assert summary["missing"] == 11
    assert summary["invalid"] == 1  # negative NO2 kept, marked invalid
    assert summary["first_observed_at"] == utc(2025, 10, 25, 22)

    rows = {
        (o.sensor_id, o.parameter, o.provenance["local_label"]): o
        for (o,) in query(client, select(Observation).where(Observation.provider == "rnmca"))
    }
    co = rows[("IS-1:9830B", "co", "2025-10-26 02")]
    assert (co.value, co.unit, co.original_value, co.original_unit) == (500.0, "µg/m³", 0.5, "mg/m³")
    assert co.category == "OFFICIAL_MEASUREMENT"
    pressure = rows[("IS-2:MTX", "pressure", "2025-10-26 04")]
    assert (pressure.value, pressure.unit, pressure.category) == (101300.0, "Pa", "WEATHER_CONTEXT")
    negative = rows[("IS-2:9841B", "no2", "2025-10-26 03")]
    assert negative.value is None and negative.quality["valid"] is False
    pm = rows[("IS-1:Derenda APM2", "pm25", "2025-10-26 04")]
    assert pm.quality == {"valid": True, "averaging": "1h", "rnmca_code": 3}
    assert pm.observed_at.replace(tzinfo=UTC) == utc(2025, 10, 26, 1)
    assert pm.provenance == {
        "product": "measurement",
        "source": "RNMCA",
        "report": "raport.xls",
        "instrument": "Derenda APM2",
        "local_label": "2025-10-26 04",
    }
    assert stations_attribution(client) == {
        "Rețeaua Națională de Monitorizare a Calității Aerului (RNMCA), calitateaer.ro"
    }
    assert (pm.latitude, pm.longitude) == (47.1571, 27.5747)

    stations = {
        s.external_id: s for (s,) in query(client, select(Station).where(Station.provider == "rnmca"))
    }
    assert sorted(stations) == ["IS-1", "IS-2"]
    assert stations["IS-2"].details["address"] == "Aleea Decebal nr. 10"
    (source,) = query(client, select(DataSource).where(DataSource.name == "rnmca"))[0]
    assert source.records_processed == 11 and source.last_error is None
    (run,) = query(client, select(IngestionRun).where(IngestionRun.source == "rnmca"))[0]
    assert (run.status, run.updated, run.skipped) == ("success", 11, 14)

    # Re-importing the same report updates rows instead of duplicating them.
    run_import(client, report)
    assert query(
        client, select(func.count()).select_from(Observation).where(Observation.provider == "rnmca")
    ) == [(11,)]

    response = client.get(
        "/api/v1/environment/observations",
        params={"category": "OFFICIAL_MEASUREMENT", "parameter": "pm25", "limit": 500},
    )
    assert response.status_code == 200
    assert {x["value"] for x in response.json()["items"] if x["provider"] == "rnmca"} == {
        12.5,
        16.0,
        4.0,
        60.0,
    }
    listed = client.get("/api/v1/environment/stations").json()["items"]
    assert {x["external_id"] for x in listed if x["provider"] == "rnmca"} == {"IS-1", "IS-2"}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_values_csv(tmp_path):
    report = parse_book(values_book())
    path = tmp_path / "values.csv"
    count = write_values_csv(report, path)
    rows = read_csv(path)
    assert count == len(rows) == len(report.hourly) + len(report.aggregates) == 25 + 2
    pm = next(r for r in rows if r["parameter"] == "pm25" and r["local_label"] == "2025-10-26 04")
    assert pm["period_start_utc"] == "2025-10-26T01:00:00Z" and pm["period_end_utc"] == "2025-10-26T02:00:00Z"
    assert (pm["value"], pm["unit"], pm["rnmca_code"], pm["rnmca_label"]) == ("16.0", "µg/m³", "3", "Moderat")
    missing = next(r for r in rows if r["parameter"] == "pm25" and r["local_label"] == "2025-10-26 03")
    assert missing["value"] == "" and missing["rnmca_code"] == ""
    daily = [r for r in rows if r["metric"] == "daily"]
    assert [(r["local_label"], r["value"], r["rnmca_code"]) for r in daily] == [
        ("2025-10-26", "20.1", ""),
        ("2025-10-27", "30.2", ""),
    ]


def test_indices_csv(tmp_path):
    path = tmp_path / "indices.csv"
    assert write_indices_csv(parse_book(indices_book()), path) == 6
    rows = read_csv(path)
    assert rows[0] == {
        "station": "IS-1",
        "station_name": "Bvd. Nicolae Iorga + Soseaua Nationala",
        "compound": "NO2",
        "parameter": "no2",
        "date": "2026-01-01",
        "rnmca_code": "4",
        "rnmca_label": "Rău",
    }


async def test_cli_exports_and_reports_failures(tmp_path, monkeypatch, capsys):
    books = {"values.xls": values_book(), "indici.xls": indices_book()}

    def fake_parse(path):
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if name not in books:
            raise RnmcaFormatError("not a readable .xls report")
        return parse_book(books[name])

    monkeypatch.setattr(import_rnmca, "parse_report", fake_parse)
    code = await import_rnmca.main(
        [
            str(tmp_path / "values.xls"),
            str(tmp_path / "indici.xls"),
            str(tmp_path / "x.xls"),
            "--no-db",
            "--csv",
            str(tmp_path / "out"),
        ]
    )
    summaries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 1
    values, indices, broken = summaries
    assert (values["status"], values["type"], values["csv"]["rows"]) == ("success", "values", 27)
    assert "database" not in values
    assert values["stations"] == ["IS-1", "IS-2", "IS-9"]
    assert (indices["status"], indices["type"], indices["csv"]["rows"]) == ("success", "indices", 6)
    assert broken == {
        "file": str(tmp_path / "x.xls"),
        "status": "failed",
        "error": "not a readable .xls report",
    }
    assert (tmp_path / "out" / "values.csv").exists() and (tmp_path / "out" / "indici.csv").exists()


def test_cli_station_overrides(tmp_path):
    path = tmp_path / "stations.json"
    path.write_text(
        json.dumps(
            {"IS-1": {"latitude": 47.1, "longitude": 27.5}, "IS-9": {"latitude": 47.2, "longitude": 27.6}}
        )
    )
    stations = import_rnmca.load_stations(str(path))
    assert stations["IS-1"]["name"] == "IS-1 Podu de Piatră" and stations["IS-1"]["latitude"] == 47.1
    assert stations["IS-9"]["coordinates_source"] == "--stations stations.json"
    path.write_text(json.dumps({"IS-1": {"latitude": 47.1}}))
    with pytest.raises(SystemExit):
        import_rnmca.load_stations(str(path))


def test_parser_module_has_no_fixed_header_rows():
    # The guide documents rows 7-10; real exports use 8-11. The parser must locate the header itself.
    shifted = FakeBook(FakeSheet([[""]] + PREAMBLE + HEADER + DATA[:1]))
    assert parse_book(shifted).hourly[0].value == 12.5
    assert rnmca_excel.find_header(shifted.sheet_by_index(0), {"zi/ora"})[0] == 8


def test_import_parameter_filter(client):
    async def go():
        async with Session() as db:
            return await import_values(
                db, parse_book(values_book()), "raport.xls", parameters={"pm25", "pressure"}
            )

    summary = client.portal.call(go)
    assert summary["stored"] == 6  # 4 PM2.5 + 2 pressure
    stored = {
        p for (p,) in query(client, select(Observation.parameter).where(Observation.provider == "rnmca"))
    }
    assert stored == {"pm25", "pressure"}


async def test_cli_parameters_option(tmp_path, monkeypatch):
    seen = {}

    async def fake_import(db, report, name, stations, aliases, parameters):
        seen["parameters"] = parameters
        return {"stored": 0}

    monkeypatch.setattr(import_rnmca, "parse_report", lambda path: parse_book(values_book()))
    monkeypatch.setattr(import_rnmca, "import_values", fake_import)
    assert await import_rnmca.main([str(tmp_path / "a.xls"), "--parameters", " pm25, no2 ,,"]) == 0
    assert seen["parameters"] == {"pm25", "no2"}
