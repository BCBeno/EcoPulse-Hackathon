import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.integrations.http import ProviderError
from app.integrations.routing import ValhallaRoutingProvider
from app.models.entities import MappedFeature, Observation, PlannedWork, Report, WeatherForecast
from app.services.air_quality import (
    ATTRIBUTION,
    LIMITATION,
    forecast_observations,
    horizon_end,
    route_air_quality,
)
from app.services.avoidance import (
    annotate_routes,
    distinct_routes,
    endpoint_in_zone,
    exclusion_ring,
    incident_zones,
    intersects,
    mapped_construction_zones,
    recommend,
    select_exclusions,
)
from app.services.scoring import EnvironmentalRouteScoringService


async def calculate(db, request, settings, http):
    provider = ValhallaRoutingProvider(settings, http)
    routes = await provider.routes(request)

    stamp = request.departure_at
    observations = (
        await db.scalars(
            select(Observation).where(
                Observation.observed_at >= stamp - timedelta(hours=2),
                Observation.observed_at <= stamp + timedelta(hours=1),
            )
        )
    ).all()
    reports = (
        await db.scalars(select(Report).where(Report.expires_at > stamp, Report.observed_at <= stamp))
    ).all()
    features = (await db.scalars(select(MappedFeature))).all()
    works = (
        await db.scalars(
            select(PlannedWork).where(
                PlannedWork.verification == "verified",
                PlannedWork.starts_at <= stamp,
                PlannedWork.ends_at >= stamp,
            )
        )
    ).all()
    weather = (
        await db.scalars(
            select(WeatherForecast).where(
                WeatherForecast.observed_at >= stamp - timedelta(hours=1),
                WeatherForecast.observed_at <= stamp + timedelta(hours=1),
            )
        )
    ).all()
    enabled = request.preferences.get("avoid_reported_incidents", True)
    zones = incident_zones(reports, works, stamp, settings.route_avoidance_radius_m)
    include_mapped = request.preferences.get("avoid_mapped_construction", False)
    if include_mapped:
        zones.extend(mapped_construction_zones(features, settings.route_avoidance_radius_m))
    selected, endpoint_zones, capped = select_exclusions(
        routes, zones, request, settings.route_max_avoidance_zones
    )
    status = "disabled" if not enabled else "not_needed"
    warnings = []
    attempts = 0
    searched = set()
    if enabled and selected:
        status = "no_clear_detour"
        try:
            async with asyncio.timeout(settings.route_detour_timeout_seconds):
                while True:
                    # Recheck every candidate against all zones, including those first encountered on a detour.
                    eligible = [z for z in zones if not endpoint_in_zone(request, z)]
                    if any(not any(intersects(r, z) for z in eligible) for r in routes):
                        status = "alternative_found"
                        break
                    key = tuple(sorted(z["id"] for z in selected))
                    if key in searched or attempts >= settings.route_max_detour_attempts:
                        capped = True
                        break
                    searched.add(key)
                    attempts += 1
                    vertices = max(4, min(24, settings.route_exclusion_vertex_budget // len(selected) - 1))
                    alternatives = await provider.routes(
                        request, exclude_polygons=[exclusion_ring(z, vertices) for z in selected]
                    )
                    # Providers may ignore exclusions. Preserve only road geometry that clears the requested zones.
                    clear = [r for r in alternatives if not any(intersects(r, z) for z in selected)]
                    if not clear:
                        warnings.append("No returned detour clears the requested incident areas.")
                        break
                    status = "alternative_found"
                    routes.extend(clear)
                    selected, endpoint_zones, more = select_exclusions(
                        routes, zones, request, settings.route_max_avoidance_zones
                    )
                    capped = capped or more
        except (ProviderError, TimeoutError):
            if status != "alternative_found":
                status = "unavailable"
            warnings.append("A detour search could not be completed; the valid routes remain available.")
    if enabled and endpoint_zones:
        warnings.append(
            "An incident area includes the origin or destination; complete avoidance cannot be guaranteed."
        )
        if not selected:
            status = "endpoint_in_zone"
    if enabled and capped:
        warnings.append("The avoidance search was limited to the highest-priority nearby incidents.")
    routes = annotate_routes(distinct_routes(routes), zones)
    relevant = [z for z in zones if any(intersects(r, z) for r in routes)]
    longest = max(r["duration_s"] for r in routes)
    cams = (
        await db.scalars(
            select(Observation).where(
                Observation.category == "MODEL_FORECAST",
                Observation.observed_at >= stamp - timedelta(hours=1),
                Observation.observed_at <= stamp + timedelta(seconds=longest, hours=1),
            )
        )
    ).all()
    forecasts, air_status = await forecast_observations(cams, request, settings, http)
    scorer = EnvironmentalRouteScoringService()
    scored = scorer.compare(
        [scorer.score(route, stamp, observations, reports, features, works, weather) for route in routes]
    )
    for route in scored:
        route["air_quality"] = route_air_quality(route, stamp, forecasts, air_status)
    recommendation = recommend(scored, relevant, enabled, settings.route_max_detour_ratio)
    if status == "alternative_found":
        fastest = min(scored, key=lambda r: r["duration_s"])
        if recommendation["id"] == fastest["id"] and any(
            r["avoidance"]["remaining_weight"] < fastest["avoidance"]["remaining_weight"]
            and r["duration_s"] > fastest["duration_s"] * settings.route_max_detour_ratio
            for r in scored
        ):
            status = "no_practical_detour"
        elif recommendation["avoidance"]["remaining_count"]:
            status = "partial_detour"
    if len(scored) == 1:
        warnings.append("Only one valid alternative returned by provider.")
    if air_status == "beyond_forecast_horizon":
        warnings.append("Departure is beyond the CAMS air quality forecast horizon.")
    return {
        "routes": scored,
        "departure_at": request.departure_at,
        "mode": request.mode,
        "warnings": warnings,
        "recommended_route_id": recommendation["id"],
        "avoidance": {
            "enabled": enabled,
            "status": status,
            "zones": [z for z in zones if any(intersects(r, z) for r in routes)],
            "endpoint_zone_ids": [z["id"] for z in endpoint_zones],
            "search_limited": capped,
            "attempts": attempts,
            "include_mapped_construction": include_mapped,
        },
        "air_quality_forecast": {
            "status": air_status,
            "horizon_end": horizon_end(settings).isoformat(),
            "scale": "RNMCA 1-6",
            "attribution": ATTRIBUTION,
            "limitation": LIMITATION,
        },
        "attribution": "© OpenStreetMap contributors / Valhalla",
    }
