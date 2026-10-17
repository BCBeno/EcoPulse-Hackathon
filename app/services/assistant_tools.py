from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.integrations.http import ProviderError
from app.models.entities import Journey, Observation, PlannedWork, WeatherForecast
from app.services.journeys import calculate
from app.services.scoring import aware


def backend_tools(db, user, payload, settings, http):
    async def route_context():
        if payload.route_request is None:
            return {"available": False, "reason": "Validated origin and destination required", "evidence": []}
        try:
            result = await calculate(db, payload.route_request, settings, http)
        except ProviderError as exc:
            return {"available": False, "reason": exc.code, "evidence": []}
        return {
            "available": True,
            "routes": [
                {
                    "id": r["id"],
                    "duration_s": r["duration_s"],
                    "distance_m": r["distance_m"],
                    "confidence": r["confidence"],
                    "environmental_score": r["environmental_score"],
                    "perspectives": r["perspectives"],
                }
                for r in result["routes"]
            ],
            "evidence": [e for r in result["routes"] for e in r["evidence"]],
        }

    async def environment_context():
        now = datetime.now(UTC)
        rows = (
            await db.scalars(
                select(Observation)
                .where(
                    Observation.observed_at >= now - timedelta(hours=2),
                    Observation.observed_at <= now + timedelta(hours=1),
                )
                .limit(50)
            )
        ).all()
        return {
            "available": bool(rows),
            "evidence": [
                {
                    "id": x.id,
                    "category": x.category,
                    "provider": x.provider,
                    "parameter": x.parameter,
                    "value": x.value,
                    "unit": x.unit,
                    "timestamp": aware(x.observed_at).isoformat(),
                }
                for x in rows
            ],
        }

    async def weather_context():
        now = payload.route_request.departure_at if payload.route_request else datetime.now(UTC)
        rows = (
            await db.scalars(
                select(WeatherForecast)
                .where(
                    WeatherForecast.observed_at >= now - timedelta(hours=1),
                    WeatherForecast.observed_at <= now + timedelta(hours=1),
                )
                .limit(20)
            )
        ).all()
        return {
            "available": bool(rows),
            "evidence": [
                {
                    "id": x.id,
                    "category": "WEATHER_CONTEXT",
                    "provider": x.provider,
                    "parameter": x.parameter,
                    "value": x.value,
                    "unit": x.unit,
                    "timestamp": aware(x.observed_at).isoformat(),
                }
                for x in rows
            ],
        }

    async def planned_works():
        now = payload.route_request.departure_at if payload.route_request else datetime.now(UTC)
        rows = (
            await db.scalars(
                select(PlannedWork)
                .where(
                    PlannedWork.verification == "verified",
                    PlannedWork.status.in_(["active", "scheduled"]),
                    PlannedWork.starts_at <= now,
                    PlannedWork.ends_at >= now,
                )
                .limit(20)
            )
        ).all()
        return {
            "available": bool(rows),
            "evidence": [
                {
                    "id": x.id,
                    "category": "OFFICIAL_PLANNED_WORK",
                    "provider": x.original_url,
                    "description": x.title,
                    "timestamp": aware(x.last_verified_at).isoformat(),
                }
                for x in rows
                if x.last_verified_at
            ],
        }

    async def saved_journeys():
        rows = (await db.scalars(select(Journey).where(Journey.owner_id == user.id).limit(20))).all()
        # Do not send precise personal journey coordinates or unrelated histories to the LLM.
        return {"count": len(rows), "evidence": []}

    return {
        "calculate_routes": (
            "Compare valid routes with actual backend evidence and deterministic scores.",
            route_context,
        ),
        "get_environmental_sources": (
            "Retrieve official, low-cost and CAMS cached readings, preserving source classes.",
            environment_context,
        ),
        "get_weather": ("Retrieve weather context for the validated departure time.", weather_context),
        "get_planned_works": (
            "Retrieve verified official works with published dates overlapping departure.",
            planned_works,
        ),
        "get_saved_journeys": ("Count journeys belonging to the authenticated user only.", saved_journeys),
    }
