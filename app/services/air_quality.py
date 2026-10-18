from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.integrations.cams import CamsAirQualityProvider
from app.integrations.http import ProviderError
from app.models.entities import Observation
from app.services.scoring import aware

# RNMCA index (1-6) upper bounds in µg/m³; the last band is open-ended.
RNMCA_BANDS = {
    "pm25": (5, 15, 50, 90, 140),
    "pm10": (15, 45, 120, 195, 270),
    "no2": (10, 25, 60, 100, 150),
}
RNMCA_LEVELS = {
    1: ("Bun", "Calitate excelentă. Aer curat, sigur pentru toată populația."),
    2: ("Acceptabil", "Calitate bună. Fără restricții pentru activități fizice."),
    3: ("Moderat", "Poluare ușoară. Persoanele extrem de sensibile pot resimți iritații."),
    4: ("Rău", "Reduceți efortul fizic intens în aer liber."),
    5: ("Foarte rău", "Nivel dăunător. Evitați plimbările copiilor și vârstnicilor."),
    6: ("Extrem de rău", "Alertă sanitară majoră. Rămâneți în interior cu ferestrele închise."),
}
FORECAST_PARAMETERS = ("pm25", "pm10", "no2", "o3", "so2", "european_aqi")
LIMITATION = "Prognoză CAMS regională (~11 km); nu reflectă diferențele dintre străzi."
ATTRIBUTION = "Copernicus Atmosphere Monitoring Service (CAMS), Open-Meteo (CC BY 4.0)"


def level(code):
    label, recommendation = RNMCA_LEVELS[code]
    return {"code": code, "label": label, "recommendation": recommendation}


def classify(parameter, value):
    bands = RNMCA_BANDS.get(parameter)
    if bands is None or value is None:
        return None
    return level(next((i + 1 for i, high in enumerate(bands) if value < high), 6))


def horizon_end(settings, now=None):
    return (now or datetime.now(UTC)) + timedelta(days=settings.forecast_days)


def usable(observation, now, latitude, longitude):
    # Grid forecasts are ~11 km cells; a 0.2° box keeps the city cell and its neighbours.
    return (
        observation.category == "MODEL_FORECAST"
        and observation.value is not None
        and observation.parameter in FORECAST_PARAMETERS
        and (now - aware(observation.retrieved_at)).total_seconds() <= 21600
        and abs(observation.latitude - latitude) <= 0.2
        and abs(observation.longitude - longitude) <= 0.2
    )


def trip_forecast(observations, start, end, latitude, longitude, now=None):
    """Worst forecast hour per pollutant over the trip window, classified on the RNMCA scale."""
    now = now or datetime.now(UTC)
    start, end = aware(start), aware(end)
    hours = {}
    for observation in observations:
        stamp = aware(observation.observed_at)
        if not usable(observation, now, latitude, longitude):
            continue
        # CAMS stamps mark the start of each hour: keep the hour in progress at departure up to arrival.
        if not (start - timedelta(hours=1) < stamp <= end):
            continue
        hours.setdefault(stamp, {})[observation.parameter] = observation.value
    if not hours:
        return None
    hourly = []
    for stamp in sorted(hours):
        values = hours[stamp]
        codes = [c["code"] for c in (classify(p, values.get(p)) for p in RNMCA_BANDS) if c]
        hourly.append(
            {
                "time": stamp.isoformat(),
                **{p: values.get(p) for p in FORECAST_PARAMETERS},
                "index": level(max(codes)) if codes else None,
            }
        )
    pollutants = {}
    for parameter in FORECAST_PARAMETERS:
        series = [h[parameter] for h in hourly if h[parameter] is not None]
        if not series:
            continue
        worst = max(series)
        pollutants[parameter] = {
            "max": worst,
            "mean": round(sum(series) / len(series), 1),
            "unit": "EAQI" if parameter == "european_aqi" else "µg/m³",
            **({"index": classify(parameter, worst)} if parameter in RNMCA_BANDS else {}),
        }
    codes = [p["index"]["code"] for p in pollutants.values() if "index" in p]
    return {
        "index": level(max(codes)) if codes else None,
        "dominant_pollutant": max(
            (k for k in pollutants if "index" in pollutants[k]),
            key=lambda k: pollutants[k]["index"]["code"],
            default=None,
        ),
        "pollutants": pollutants,
        "hourly": hourly,
    }


async def forecast_observations(db_rows, request, settings, http, now=None):
    """Cached CAMS rows for the trip, or a live CAMS fetch when the cache does not cover departure."""
    now = now or datetime.now(UTC)
    departure = aware(request.departure_at)
    if departure > horizon_end(settings, now):
        return [], "beyond_forecast_horizon"
    latitude = (request.origin.latitude + request.destination.latitude) / 2
    longitude = (request.origin.longitude + request.destination.longitude) / 2
    cached = [
        o
        for o in db_rows
        if usable(o, now, latitude, longitude)
        and abs((aware(o.observed_at) - departure).total_seconds()) <= 3600
    ]
    if cached:
        return list(db_rows), "cached"
    if not settings.openmeteo_air_quality_enabled:
        return [], "unavailable"
    try:
        live = await CamsAirQualityProvider(settings, http).forecast(latitude, longitude)
    except ProviderError:
        return [], "unavailable"
    return live, "live"


def route_air_quality(route, departure, observations, status, now=None):
    if status in {"beyond_forecast_horizon", "unavailable"}:
        return {"available": False, "status": status}
    coordinates = route["geometry"]["coordinates"]
    latitude = sum(c[1] for c in coordinates) / len(coordinates)
    longitude = sum(c[0] for c in coordinates) / len(coordinates)
    arrival = aware(departure) + timedelta(seconds=route["duration_s"])
    forecast = trip_forecast(observations, departure, arrival, latitude, longitude, now)
    if forecast is None:
        return {"available": False, "status": "no_forecast_for_window"}
    return {
        "available": True,
        "status": status,
        "window": {"start": aware(departure).isoformat(), "end": arrival.isoformat()},
        **forecast,
        "scale": "RNMCA 1-6",
        "attribution": ATTRIBUTION,
        "limitation": LIMITATION,
    }


async def point_forecast(db, begin, end, latitude, longitude, settings, http):
    """RNMCA forecast for a time window at a point: cached CAMS rows first, then a live CAMS fetch."""
    if aware(begin) > horizon_end(settings):
        return None, "beyond_forecast_horizon"
    rows = (
        await db.scalars(
            select(Observation).where(
                Observation.category == "MODEL_FORECAST",
                Observation.observed_at >= begin - timedelta(hours=1),
                Observation.observed_at <= end,
            )
        )
    ).all()
    forecast = trip_forecast(rows, begin, end, latitude, longitude)
    if forecast is not None:
        return forecast, "cached"
    if not settings.openmeteo_air_quality_enabled:
        return None, "unavailable"
    # The sync worker may not have run yet; the shared HTTP cache bounds upstream calls.
    try:
        live = await CamsAirQualityProvider(settings, http).forecast(latitude, longitude)
    except ProviderError:
        return None, "unavailable"
    forecast = trip_forecast(live, begin, end, latitude, longitude)
    return forecast, "live" if forecast else "unavailable"
