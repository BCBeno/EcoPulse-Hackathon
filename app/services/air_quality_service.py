"""Iași air quality: current CAMS readings and hourly forecast (Open-Meteo), classified on the RNMCA scale.

Follows AIR_QUALITY_INTEGRATION_GUIDE.md (thresholds, request parameters, response contract), with three
deliberate changes from its drop-in module:
- requests go through the shared async ProviderHTTP (10-minute cache, retries) instead of blocking urllib,
  which would stall the event loop;
- missing values (null in the upstream payload) become null with code 0 instead of crashing or reading 0.0;
- times are requested as unix timestamps, so `datetime_utc` is exact across daylight-saving changes, and
  `datetime` keeps the guide's local `YYYY-MM-DDTHH:MM` wall-clock format.
"""

import math
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.http import ProviderError

ENDPOINT = "https://air-quality-api.open-meteo.com/v1/air-quality"
CURRENT_VARIABLES = "european_aqi,pm10,pm2_5,nitrogen_dioxide,ozone,sulphur_dioxide"
HOURLY_VARIABLES = "pm10,pm2_5,nitrogen_dioxide,ozone,sulphur_dioxide,european_aqi"
CACHE_SECONDS = 600
UNIT = "µg/m³"
ATTRIBUTION = "Copernicus Atmosphere Monitoring Service (CAMS), Open-Meteo"

# RNMCA scale (1-6), lower bound inclusive, upper bound exclusive.
AQI_THRESHOLDS = {
    "PM2.5": [
        (0, 5, 1, "Bun"),
        (5, 15, 2, "Acceptabil"),
        (15, 50, 3, "Moderat"),
        (50, 90, 4, "Rău"),
        (90, 140, 5, "Foarte rău"),
        (140, math.inf, 6, "Extrem de rău"),
    ],
    "PM10": [
        (0, 15, 1, "Bun"),
        (15, 45, 2, "Acceptabil"),
        (45, 120, 3, "Moderat"),
        (120, 195, 4, "Rău"),
        (195, 270, 5, "Foarte rău"),
        (270, math.inf, 6, "Extrem de rău"),
    ],
    "NO2": [
        (0, 10, 1, "Bun"),
        (10, 25, 2, "Acceptabil"),
        (25, 60, 3, "Moderat"),
        (60, 100, 4, "Rău"),
        (100, 150, 5, "Foarte rău"),
        (150, math.inf, 6, "Extrem de rău"),
    ],
    # O3 and SO2 are not in the guide; values from the "Legenda indici de calitate" sheet of RNMCA reports.
    # The live index below still follows the guide (PM2.5, PM10, NO2); the report import classifies all five.
    "O3": [
        (0, 60, 1, "Bun"),
        (60, 100, 2, "Acceptabil"),
        (100, 120, 3, "Moderat"),
        (120, 160, 4, "Rău"),
        (160, 180, 5, "Foarte rău"),
        (180, math.inf, 6, "Extrem de rău"),
    ],
    "SO2": [
        (0, 20, 1, "Bun"),
        (20, 40, 2, "Acceptabil"),
        (40, 125, 3, "Moderat"),
        (125, 190, 4, "Rău"),
        (190, 275, 5, "Foarte rău"),
        (275, math.inf, 6, "Extrem de rău"),
    ],
}

RECOMMENDATIONS = {
    1: "Calitate excelentă. Aer curat, sigur pentru toată populația.",
    2: "Calitate bună. Fără restricții pentru activități fizice.",
    3: "Poluare ușoară. Persoanele extrem de sensibile pot resimți iritații.",
    4: "Reduceți efortul fizic intens în aer liber.",
    5: "Nivel dăunător. Evitați plimbările copiilor și vârstnicilor.",
    6: "Alertă sanitară majoră. Rămâneți în interior cu ferestrele închise.",
}

# Alerts start at "Moderat" (guide, section 7.4).
ALERT_CODE = 3

UNKNOWN = {"code": 0, "label": "Necunoscut", "recommendation": None}


def number(value):
    """A finite, non-negative concentration, or None for missing or invalid upstream values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


class AirQualityService:
    DEFAULT_LAT = 47.16
    DEFAULT_LON = 27.58

    def __init__(self, http):
        self.http = http

    @classmethod
    def get_aqi_badge(cls, pollutant, value):
        value = number(value)
        bands = AQI_THRESHOLDS.get(pollutant)
        if value is None or not bands:
            return dict(UNKNOWN)
        for low, high, code, label in bands:
            if low <= value < high:
                return {"code": code, "label": label, "recommendation": RECOMMENDATIONS[code]}
        return dict(UNKNOWN)

    @classmethod
    def overall(cls, pm25, pm10, no2):
        """The RNMCA index is the worst sub-index of the classified pollutants that have a value."""
        badges = {
            "pm2_5": cls.get_aqi_badge("PM2.5", pm25),
            "pm10": cls.get_aqi_badge("PM10", pm10),
            "no2": cls.get_aqi_badge("NO2", no2),
        }
        pollutant, worst = max(badges.items(), key=lambda item: item[1]["code"])
        if not worst["code"]:
            return {**UNKNOWN, "pollutant": None}
        return {**worst, "pollutant": pollutant}

    async def fetch_air_quality(self, lat=DEFAULT_LAT, lon=DEFAULT_LON, forecast_days=3):
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": CURRENT_VARIABLES,
            "hourly": HOURLY_VARIABLES,
            "forecast_days": forecast_days,
            "timezone": "auto",
            "timeformat": "unixtime",
        }
        raw = await self.http.get_json(ENDPOINT, params, ttl=CACHE_SECONDS)
        return self.parse(raw, lat, lon)

    @classmethod
    def parse(cls, raw, lat=DEFAULT_LAT, lon=DEFAULT_LON):
        if not isinstance(raw, dict) or not isinstance(raw.get("current"), dict):
            raise ProviderError("unexpected_response_schema")
        hourly = raw.get("hourly") if isinstance(raw.get("hourly"), dict) else {}
        zone = local_zone(raw)
        curr = raw["current"]

        pm25, pm10, no2 = (number(curr.get(k)) for k in ("pm2_5", "pm10", "nitrogen_dioxide"))
        current_summary = {
            "timestamp": local_time(curr.get("time"), zone),
            "timestamp_utc": utc_time(curr.get("time")),
            "pm2_5": {"value": pm25, "unit": UNIT, **cls.get_aqi_badge("PM2.5", pm25)},
            "pm10": {"value": pm10, "unit": UNIT, **cls.get_aqi_badge("PM10", pm10)},
            "no2": {"value": no2, "unit": UNIT, **cls.get_aqi_badge("NO2", no2)},
            "ozone": {"value": number(curr.get("ozone")), "unit": UNIT},
            "so2": {"value": number(curr.get("sulphur_dioxide")), "unit": UNIT},
            "european_aqi": number(curr.get("european_aqi")),
            "rnmca": cls.overall(pm25, pm10, no2),
        }
        current_summary["alert"] = current_summary["pm2_5"]["code"] >= ALERT_CODE

        def series(name, index):
            values = hourly.get(name)
            return number(values[index]) if isinstance(values, list) and index < len(values) else None

        forecast_timeline = []
        times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
        for i, stamp in enumerate(times):
            when = utc_time(stamp)
            if when is None:
                continue
            f_pm25, f_pm10, f_no2 = series("pm2_5", i), series("pm10", i), series("nitrogen_dioxide", i)
            badge = cls.get_aqi_badge("PM2.5", f_pm25)
            rnmca = cls.overall(f_pm25, f_pm10, f_no2)
            forecast_timeline.append(
                {
                    "datetime": local_time(stamp, zone),
                    "datetime_utc": when,
                    "pm2_5": f_pm25,
                    "pm10": f_pm10,
                    "no2": f_no2,
                    "ozone": series("ozone", i),
                    "so2": series("sulphur_dioxide", i),
                    "european_aqi": series("european_aqi", i),
                    "aqi_pm25_code": badge["code"],
                    "aqi_pm25_label": badge["label"],
                    "aqi_code": rnmca["code"],
                    "aqi_label": rnmca["label"],
                }
            )

        return {
            "city": "Iasi",
            "coordinates": {"latitude": lat, "longitude": lon},
            "timezone": zone.key if isinstance(zone, ZoneInfo) else raw.get("timezone"),
            "current": current_summary,
            "forecast_hourly": forecast_timeline,
            "provenance": {
                "attribution": ATTRIBUTION,
                "model": "CAMS European air quality forecast",
                "license": "CC BY 4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "limitation": "Regional grid estimate (~11 km); not a street measurement",
                "scale": "RNMCA 1-6 (PM2.5, PM10, NO2)",
            },
        }


def local_zone(raw):
    try:
        return ZoneInfo(raw["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        offset = raw.get("utc_offset_seconds")
        return timezone(timedelta(seconds=offset)) if isinstance(offset, int) else UTC


def epoch(stamp):
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(stamp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def utc_time(stamp):
    when = epoch(stamp)
    return when.isoformat().replace("+00:00", "Z") if when else None


def local_time(stamp, zone):
    when = epoch(stamp)
    return when.astimezone(zone).strftime("%Y-%m-%dT%H:%M") if when else None
