"""Parser for RNMCA (calitateaer.ro) Excel reports.

Two report types are recognised from the header cell in column A:

- "Zi/Ora": hourly values report. Header rows: station (written once per group), "instrument - compound",
  metric ("Valori orare", "Valori zilnice", ...), unit ("Valoare [µg/m³]"); data rows follow.
- "Data/Timp": daily quality-index report. Header rows: station, compound, metric ("Zilnic"); the values are
  RNMCA codes 1-6. Its "Legenda" sheet carries the official concentration thresholds.

The header is located by that cell rather than by fixed row numbers: real exports place it one row lower than
AIR_QUALITY_INTEGRATION_GUIDE.md states (row 8 instead of 7).

Timestamps "YYYY-MM-DD HH" use hours 01-24 and are the END of the averaging hour, in Romanian local time with
daylight saving: hour 24 is 00:00 of the next day, spring-forward days skip label 03 and autumn days keep 24
labels (one real hour is not reported). Ambiguous autumn labels resolve to the first occurrence (summer time).
The string "-" marks a sensor dropout and becomes None.
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import xlrd

LOCAL = ZoneInfo("Europe/Bucharest")
HOUR = timedelta(hours=1)

STATION_RE = re.compile(r"^\s*([A-Z]{1,3}-\d+)\s*(?:[–—-]\s*(.*?))?\s*$")
UNIT_RE = re.compile(r"\[(.+?)\]")
HOUR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2})$")
DAY_RE = re.compile(r"^(\d{4})[/-](\d{2})[/-](\d{2})$")


def key(text):
    """Diacritic-, case- and whitespace-insensitive form used to match header labels."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", str(text)) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


POLLUTANTS = {
    key(name): parameter
    for name, parameter in {
        "PM 2.5": "pm25",
        "PM 10": "pm10",
        "NO2": "no2",
        "NO": "no",
        "NOx": "nox",
        "SO2": "so2",
        "O3": "o3",
        "CO": "co",
        "Benzen": "benzene",
        "Toluen": "toluene",
        "Etilbenzen": "ethylbenzene",
        "m-Xilen": "m_xylene",
        "o-Xilen": "o_xylene",
        "p-Xilen": "p_xylene",
        "Pb": "pb",
        "Cd": "cd",
        "Ni": "ni",
        "As": "as",
    }.items()
}

METEO = {
    key(name): parameter
    for name, parameter in {
        "Temperatura aer": "temperature",
        "Umiditate relativa": "humidity",
        "Presiunea aerului": "pressure",
        "Viteza vantului": "wind_speed",
        "Directia vantului": "wind_direction",
        "Precipitatii": "precipitation",
        "Radiatia solara": "solar_radiation",
    }.items()
}

METRICS = {
    key(name): metric
    for name, metric in {
        "Valori orare": "hourly",
        "Media mobila orara": "hourly_moving_average",
        "Valori zilnice": "daily",
        "Zilnic": "daily",
        "Maximul zilnic medie mobila": "daily_max_moving_average",
        "Valori anuale": "annual",
        "Maximul anual medie mobila": "annual_max_moving_average",
        "Expunere acumulata anuala": "annual_accumulated_exposure",
    }.items()
}

DAILY_METRICS = {"daily", "daily_max_moving_average"}


def scope(metric):
    """How often a metric changes: "day" and "year" values repeat on every hourly row of their period."""
    if metric in DAILY_METRICS:
        return "day"
    if metric.startswith("annual"):
        return "year"
    return "hour"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", key(text)).strip("_") or "unknown"


class RnmcaFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Column:
    station: str
    station_name: str | None
    instrument: str
    compound: str
    parameter: str
    kind: str  # "pollutant" | "meteo"
    metric: str
    unit: str


@dataclass(frozen=True, slots=True)
class Value:
    column: Column
    period_start: datetime  # UTC
    period_end: datetime  # UTC
    label: str  # the report's own time label, e.g. "2026-01-01 24" or "2026-01-01"
    value: float | None


@dataclass
class ValuesReport:
    interval: str | None
    columns: list[Column]
    hourly: list[Value]
    aggregates: list[Value]
    rows: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IndexValue:
    station: str
    station_name: str | None
    compound: str
    parameter: str
    day: date
    code: int | None


@dataclass
class IndicesReport:
    interval: str | None
    values: list[IndexValue]
    thresholds: dict[str, list[tuple[float, float, int, str]]]
    rows: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- cells and time


def cell_number(sheet, row, col):
    """Numeric cell value, or None for "-", blanks and anything that is not a finite number."""
    kind, raw = sheet.cell_type(row, col), sheet.cell_value(row, col)
    if kind == xlrd.XL_CELL_NUMBER:
        return float(raw) if math.isfinite(raw) else None
    if kind == xlrd.XL_CELL_TEXT:
        text = raw.strip().replace(",", ".")
        if text in {"", "-"}:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if math.isfinite(value) else None
    return None


def text(sheet, row, col):
    value = sheet.cell_value(row, col)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def local_instant(naive):
    """Local wall-clock time to UTC, or None when that time does not exist (spring-forward gap)."""
    aware = naive.replace(tzinfo=LOCAL)
    utc = aware.astimezone(UTC)
    return utc if utc.astimezone(LOCAL).replace(tzinfo=None) == naive else None


def hour_period(label):
    """("YYYY-MM-DD HH" hour-ending local label) -> (UTC start, UTC end, local date the label belongs to)."""
    match = HOUR_RE.match(label)
    if not match:
        return None
    year, month, day, hour = (int(x) for x in match.groups())
    if not 1 <= hour <= 24:
        return None
    try:
        day_start = datetime(year, month, day)
    except ValueError:
        return None
    end = local_instant(day_start + timedelta(hours=hour))
    return (end - HOUR, end, day_start.date()) if end else None


def day_period(day):
    start = local_instant(datetime(day.year, day.month, day.day))
    following = day + timedelta(days=1)
    end = local_instant(datetime(following.year, following.month, following.day))
    return start, end


def year_period(year):
    return local_instant(datetime(year, 1, 1)), local_instant(datetime(year + 1, 1, 1))


# ---------------------------------------------------------------- headers


def open_book(source):
    try:
        if isinstance(source, (bytes, bytearray)):
            return xlrd.open_workbook(file_contents=bytes(source))
        return xlrd.open_workbook(source)
    except OSError:
        raise
    except Exception as exc:  # xlrd raises zipfile, struct and compdoc errors for non-.xls content
        raise RnmcaFormatError(f"not a readable .xls report: {type(exc).__name__}: {exc}") from None


def find_header(sheet, labels):
    for row in range(min(sheet.nrows, 40)):
        if sheet.ncols and key(sheet.cell_value(row, 0)) in labels:
            return row, key(sheet.cell_value(row, 0))
    return None, None


def interval_text(sheet, header_row):
    for row in range(header_row):
        value = str(sheet.cell_value(row, 0))
        if key(value).startswith("interval"):
            return value.split(":", 1)[1].strip() if ":" in value else value
    return None


def parse_station(raw):
    match = STATION_RE.match(raw)
    if not match:
        return raw.strip(), None
    return match.group(1), (match.group(2) or None)


def compound_parameter(compound, instrument):
    k = key(compound)
    if k in METEO or key(instrument) == "mtx":
        return METEO.get(k, slug(compound)), "meteo"
    return POLLUTANTS.get(k, slug(compound)), "pollutant"


# ---------------------------------------------------------------- reports


def parse_report(source):
    """Parse an RNMCA .xls report (path or bytes) into a ValuesReport or IndicesReport."""
    return parse_book(open_book(source))


def parse_book(book):
    """Parse an opened workbook (xlrd Book or anything with the same sheet interface)."""
    sheet = book.sheet_by_index(0)
    header, label = find_header(sheet, {"zi/ora", "data/timp"})
    if header is None:
        raise RnmcaFormatError("no 'Zi/Ora' or 'Data/Timp' header cell in column A of the first sheet")
    if label == "zi/ora":
        return parse_values(sheet, header)
    return parse_indices(book, sheet, header)


def parse_values(sheet, header):
    if sheet.nrows < header + 4:
        raise RnmcaFormatError("values report header is incomplete")
    warnings, unknown = [], set()
    columns = {}
    station = station_name = instrument_text = ""
    for col in range(1, sheet.ncols):
        station_text = text(sheet, header, col)
        if station_text:
            station, station_name = parse_station(station_text)
        instrument_cell = text(sheet, header + 1, col)
        if instrument_cell:
            instrument_text = instrument_cell
        metric_text = text(sheet, header + 2, col)
        if not (station and instrument_text and metric_text):
            continue
        instrument, sep, compound = instrument_text.partition(" - ")
        if not sep:
            instrument, compound = "", instrument_text
        instrument, compound = instrument.strip(), compound.strip()
        parameter, kind = compound_parameter(compound, instrument)
        if key(compound) not in POLLUTANTS and key(compound) not in METEO:
            unknown.add(compound)
        unit_match = UNIT_RE.search(text(sheet, header + 3, col))
        metric = METRICS.get(key(metric_text), slug(metric_text))
        columns[col] = Column(
            station=station,
            station_name=station_name,
            instrument=instrument,
            compound=compound,
            parameter=parameter,
            kind=kind,
            metric=metric,
            unit=unit_match.group(1).strip() if unit_match else "",
        )
    if not columns:
        raise RnmcaFormatError("values report has no data columns")
    if unknown:
        warnings.append(f"unmapped compounds kept under slug names: {', '.join(sorted(unknown))}")

    hourly, aggregates = [], {}
    conflicts = rows = skipped = 0
    for row in range(header + 4, sheet.nrows):
        label = text(sheet, row, 0)
        if not label:
            continue
        period = hour_period(label)
        if period is None:
            skipped += 1
            if skipped <= 5:
                warnings.append(f"row {row + 1}: unusable time label {label!r}")
            continue
        start, end, local_day = period
        rows += 1
        for col, column in columns.items():
            value = cell_number(sheet, row, col)
            period_scope = scope(column.metric)
            if period_scope == "hour":
                hourly.append(Value(column, start, end, label, value))
                continue
            # Daily and annual values repeat on every hourly row: keep one per day / year.
            group = local_day if period_scope == "day" else local_day.year
            known = aggregates.get((col, group))
            if known is None or known.value is None:
                if period_scope == "day":
                    p_start, p_end = day_period(local_day)
                    agg_label = local_day.isoformat()
                else:
                    p_start, p_end = year_period(local_day.year)
                    agg_label = str(local_day.year)
                aggregates[(col, group)] = Value(column, p_start, p_end, agg_label, value)
            elif value is not None and abs(known.value - value) > 1e-9:
                conflicts += 1
    if skipped > 5:
        warnings.append(f"{skipped} rows skipped for unusable time labels")
    if conflicts:
        warnings.append(
            f"{conflicts} repeated daily/annual cells disagreed with the first value; kept the first"
        )
    return ValuesReport(
        interval=interval_text(sheet, header),
        columns=list(columns.values()),
        hourly=hourly,
        aggregates=list(aggregates.values()),
        rows=rows,
        warnings=warnings,
    )


def parse_indices(book, sheet, header):
    if sheet.nrows < header + 3:
        raise RnmcaFormatError("indices report header is incomplete")
    warnings = []
    columns = {}
    station = station_name = ""
    for col in range(1, sheet.ncols):
        station_text = text(sheet, header, col)
        if station_text:
            station, station_name = parse_station(station_text)
        compound = text(sheet, header + 1, col)
        if station and compound:
            parameter, _ = compound_parameter(compound, "")
            columns[col] = (station, station_name, compound, parameter)
    values, rows, invalid = [], 0, 0
    for row in range(header + 3, sheet.nrows):
        match = DAY_RE.match(text(sheet, row, 0))
        if not match:
            continue
        try:
            day = date(*(int(x) for x in match.groups()))
        except ValueError:
            continue
        rows += 1
        for col, (station, station_name, compound, parameter) in columns.items():
            number = cell_number(sheet, row, col)
            code = int(number) if number is not None and number.is_integer() and 1 <= number <= 6 else None
            if number is not None and code is None:
                invalid += 1
            values.append(IndexValue(station, station_name, compound, parameter, day, code))
    if invalid:
        warnings.append(f"{invalid} index cells were outside the 1-6 scale and were treated as missing")
    return IndicesReport(
        interval=interval_text(sheet, header),
        values=values,
        thresholds=parse_legend(book),
        rows=rows,
        warnings=warnings,
    )


def parse_legend(book):
    """Official thresholds from the "Legenda" sheet: {parameter: [(min, max, code, label), ...]}."""
    sheet = next((s for s in book.sheets() if key(s.name).startswith("legenda")), None)
    if sheet is None:
        return {}
    thresholds, current = {}, None
    for row in range(sheet.nrows):
        first = text(sheet, row, 0)
        if key(first) in POLLUTANTS and not text(sheet, row, 1):
            current = POLLUTANTS[key(first)]
            thresholds[current] = []
            continue
        if current is None or sheet.ncols < 4:
            continue
        low, high = cell_number(sheet, row, 0), cell_number(sheet, row, 1)
        code = cell_number(sheet, row, 2)
        if low is None or high is None or code is None:
            continue
        thresholds[current].append((low, high, int(code), text(sheet, row, 3)))
    return {k: v for k, v in thresholds.items() if v}
