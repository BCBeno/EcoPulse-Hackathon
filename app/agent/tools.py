"""The agent's four tools. Each returns a compact summary for the model and full cards for the app.

They call the same services as the REST API, so the chat never computes anything on its own.
Tools only read public data or create drafts: publishing is a separate, user-pressed REST call.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any
from uuid import UUID

import httpx
from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.air import eaqi
from app.air.quality import distance_m
from app.air.router import air_forecast, air_point
from app.air.service import ForecastCache
from app.config import Settings
from app.devices.models import Device
from app.errors import AppError
from app.geo import LatLon, point_wkt
from app.places import service as places
from app.reports.categories import CATEGORIES, SEVERITIES
from app.reports.models import Report, ReportDraft
from app.reports.service import active_filter, to_out
from app.routing.service import Endpoint, RouteRequest, plan

HERE_WORDS = {"aici", "here", "langa mine", "unde sunt", "locatia mea", "pozitia mea", "aproape", "in zona"}
SELECTED_WORDS = {"punctul selectat", "punct selectat", "selectat", "pe harta"}
SAME_PLACE_M = 500

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_air",
            "description": "Air quality now or at a time in Iași, at a place, plus weather; optional hourly "
            "forecast to find the best time to go out.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "Place name in Iași, or 'aici' for the user."},
                    "when": {"type": "string", "description": "ISO 8601 time with offset; omit for now."},
                    "forecast_hours": {"type": "integer", "description": "1-72; only to find the best hours"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_routes",
            "description": "Cleanest, balanced and fastest walk/bike routes between two places in Iași.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Place name, or 'aici' for the user's position.",
                    },
                    "destination": {"type": "string"},
                    "mode": {"type": "string", "enum": ["walk", "bike"]},
                    "when": {
                        "type": "string",
                        "description": "Departure, ISO 8601 with offset; omit for now.",
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_reports",
            "description": "Active community reports (smoke, dust, works, obstacles...) near a place.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "Place name, or 'aici'."},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "radius_m": {"type": "integer", "description": "100-3000, default 1000"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_report",
            "description": "Prepare a public report of a problem the user observes. The user confirms it "
            "with a button; never claim it is published.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "severity": {
                        "type": "integer",
                        "enum": [1, 2, 3],
                        "description": "1 minor, 2 medium, 3 serious",
                    },
                    "description": {
                        "type": "string",
                        "description": "Public description in Romanian, 1-2 sentences, no personal data",
                    },
                    "place": {
                        "type": "string",
                        "description": "Named place, or 'aici' for the user's position.",
                    },
                },
                "required": ["category", "severity", "description"],
            },
        },
    },
]

STATUS = {
    "get_air": "Verific calitatea aerului…",
    "plan_routes": "Caut traseele…",
    "find_reports": "Caut rapoartele din zonă…",
    "draft_report": "Pregătesc raportul…",
}


@dataclass
class AgentContext:
    session: AsyncSession
    settings: Settings
    http: httpx.AsyncClient
    cache: ForecastCache
    device: Device
    conversation_id: UUID
    user_text: str
    location: LatLon | None = None
    map_center: LatLon | None = None
    selected_point: LatLon | None = None


@dataclass
class ToolResult:
    summary: dict[str, Any]
    cards: list[dict] = field(default_factory=list)


@dataclass
class Located:
    point: LatLon
    label: str | None
    source: str  # place | gps | map


class Unresolved(Exception):
    def __init__(self, result: ToolResult):
        self.result = result


def _distance_m(a: LatLon, b: LatLon) -> float:
    return distance_m(a.lat, a.lon, b.lat, b.lon)


async def locate(ctx: AgentContext, place: str | None, *, fallback_center: bool) -> Located | None:
    words = places.normalize(place or "")
    if words in SELECTED_WORDS and ctx.selected_point:
        return Located(ctx.selected_point, None, "map")
    if not words or words in HERE_WORDS:
        if ctx.location:
            return Located(ctx.location, None, "gps")
        if ctx.selected_point:
            return Located(ctx.selected_point, None, "map")
        if fallback_center:
            center = ctx.map_center or LatLon(
                lat=ctx.settings.city_center_lat, lon=ctx.settings.city_center_lon
            )
            return Located(center, "centrul hărții" if ctx.map_center else "centrul orașului", "map")
        return None
    candidates = await places.resolve(ctx.session, ctx.http, ctx.settings.city_bbox, place, ctx.location)
    if not candidates:
        raise Unresolved(ToolResult({"error": f"Nu am găsit locul „{place}” în Iași."}))
    first = candidates[0]
    same = all(_distance_m(first.location, c.location) <= SAME_PLACE_M for c in candidates)
    if len(candidates) > 1 and not same and not first.id.startswith("local:"):
        options = [c.model_dump(mode="json") for c in candidates[:3]]
        raise Unresolved(
            ToolResult(
                {"ambiguous": place, "options": [c.label for c in candidates[:3]], "ask_user": True},
                [{"type": "places", "query": place, "items": options}],
            )
        )
    return Located(first.location, first.name, "place")


def _when(value: str | None, local: tzinfo) -> datetime | None:
    """ISO time from the model; a time without offset is read as local (Europe/Bucharest)."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=local)


def _level_label(level: int | None) -> str | None:
    return next((lvl.label for lvl in eaqi.LEVELS if lvl.level == level), None)


async def get_air(ctx: AgentContext, place: str | None = None, when: str | None = None, forecast_hours=None):
    where = await locate(ctx, place, fallback_center=True)
    point = await air_point(
        ctx.session, ctx.settings, ctx.cache, where.point.lat, where.point.lon, _when(when, ctx.settings.tz)
    )
    point.place_label = where.label
    card: dict[str, Any] = {"type": "air", "point": point.model_dump(mode="json"), "forecast": None}
    summary: dict[str, Any] = {
        "place": where.label or "locația utilizatorului",
        "time": point.at.astimezone(ctx.settings.tz).strftime("%Y-%m-%d %H:%M"),
        "kind": point.kind,
        "eaqi": point.eaqi.level,
        "eaqi_label": _level_label(point.eaqi.level),
        "dominant": point.eaqi.dominant,
        "ug_m3": {k: v.value for k, v in point.pollutants.items()},
        "confidence": point.confidence,
        "notes": point.notes,
        "weather": point.weather.model_dump() if point.weather else None,
    }
    if isinstance(forecast_hours, int) and forecast_hours > 0:
        hours = min(forecast_hours, 72)
        forecast = await air_forecast(
            ctx.session, ctx.settings, ctx.cache, where.point.lat, where.point.lon, hours
        )
        card["forecast"] = forecast.model_dump(mode="json")
        if forecast.best_window:
            tz = ctx.settings.tz
            summary["best_window"] = {
                "start": forecast.best_window.start.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                "end": forecast.best_window.end.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                "eaqi_label": _level_label(forecast.best_window.eaqi_level),
            }
    return ToolResult(summary, [card])


async def plan_routes(
    ctx: AgentContext, origin: str, destination: str, mode: str = "walk", when: str | None = None
):
    start = await locate(ctx, origin, fallback_center=False)
    if start is None:
        return ToolResult(
            {"error": "Nu știu de unde pleci: cere un loc de plecare sau permisiunea de locație."}
        )
    end = await locate(ctx, destination, fallback_center=False)
    if end is None:
        return ToolResult({"error": "Nu știu unde vrei să ajungi."})
    request = RouteRequest(
        origin=Endpoint(lat=start.point.lat, lon=start.point.lon, label=start.label),
        destination=Endpoint(lat=end.point.lat, lon=end.point.lon, label=end.label),
        mode="bike" if mode == "bike" else "walk",
        depart_at=_when(when, ctx.settings.tz),
    )
    result = await plan(ctx.session, ctx.http, ctx.settings, request, ctx.cache)
    card = {
        "type": "routes",
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json", by_alias=True),
    }
    summary = {
        "from": start.label or "poziția ta",
        "to": end.label,
        "mode": request.mode,
        "summary": result.summary,
        "routes": [
            {
                "kinds": r.kinds,
                "minutes": round(r.duration_s / 60),
                "km": round(r.distance_m / 1000, 1),
                "vs_fastest_pct": r.exposure.vs_fastest_pct,
                "worst_level": _level_label(r.exposure.max_eaqi_level),
                "warnings": r.warnings,
            }
            for r in result.routes
        ],
        "notes": result.notes,
    }
    return ToolResult(summary, [card])


async def find_reports(
    ctx: AgentContext, place: str | None = None, category: str | None = None, radius_m=1000
):
    where = await locate(ctx, place, fallback_center=True)
    radius = max(100, min(radius_m if isinstance(radius_m, int) else 1000, 3000))
    center = func.ST_GeogFromText(point_wkt(where.point.lat, where.point.lon))
    query = select(Report).where(
        active_filter(datetime.now(UTC)),
        func.ST_DWithin(cast(Report.location, Geography), center, radius),
    )
    if category in CATEGORIES:
        query = query.where(Report.category == category)
    reports = list(await ctx.session.scalars(query.order_by(Report.created_at.desc()).limit(10)))
    items = await to_out(ctx.session, reports, ctx.device)
    summary = {
        "place": where.label or "locația utilizatorului",
        "radius_m": radius,
        "count": len(items),
        "reports": [
            {
                "category": CATEGORIES[r.category].label,
                "severity": SEVERITIES[r.severity],
                "description": r.description,
                "distance_m": round(_distance_m(where.point, r.location)),
                "votes": r.votes.model_dump(),
            }
            for r in items
        ],
    }
    card = {
        "type": "reports",
        "center": where.point.model_dump(),
        "items": [r.model_dump(mode="json") for r in items],
    }
    return ToolResult(summary, [card])


PERSONAL_DATA = [
    re.compile(r"\b(?:\+?40|0)7\d{2}[\s.-]?\d{3}[\s.-]?\d{3}\b"),  # Romanian mobile numbers
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),  # e-mail addresses
    re.compile(r"\b(?:IS|B|[A-Z]{2})[\s-]?\d{2,3}[\s-]?[A-Z]{3}\b"),  # number plates
]


def scrub(text: str) -> str:
    for pattern in PERSONAL_DATA:
        text = pattern.sub("[ascuns]", text)
    return re.sub(r"\s+", " ", text).strip()[:600]


async def draft_report(
    ctx: AgentContext, category: str, severity: int, description: str, place: str | None = None
):
    if category not in CATEGORIES:
        category = "other"
    severity = severity if severity in (1, 2, 3) else 2
    description = scrub(description or "")
    if len(description) < 5:
        description = scrub(ctx.user_text)
    where = await locate(ctx, place, fallback_center=False)
    label = where.label if where else None
    if where and where.source == "gps" and label is None:
        label = await places.reverse(ctx.session, ctx.http, where.point.lat, where.point.lon)
    if where and not ctx.settings.city_bbox.contains(where.point.lat, where.point.lon):
        return ToolResult({"error": "Locul e în afara Iașului; EcoPulse acoperă doar orașul."})
    draft = ReportDraft(
        device_id=ctx.device.id,
        conversation_id=ctx.conversation_id,
        category=category,
        severity=severity,
        description=description,
        original_text=ctx.user_text[:1000],
        location=point_wkt(where.point.lat, where.point.lon) if where else None,
        location_label=label,
        location_source=where.source if where else "none",
        extraction="llm",
    )
    ctx.session.add(draft)
    await ctx.session.flush()
    card = draft_card(draft, where.point if where else None)
    summary = {
        "draft_id": str(draft.id),
        "category": CATEGORIES[category].label,
        "severity": SEVERITIES[severity],
        "description": description,
        "place": label,
        "needs_location": where is None,
        "published": False,
        "tell_user": "Verifică ciorna și apasă Publică." if where else "Alege locul pe hartă, apoi Publică.",
    }
    return ToolResult(summary, [card])


def draft_card(draft: ReportDraft, point: LatLon | None) -> dict:
    return {
        "type": "report_draft",
        "draft_id": str(draft.id),
        "status": draft.status,
        "category": draft.category,
        "severity": draft.severity,
        "description": draft.description,
        "location": point.model_dump() if point else None,
        "location_label": draft.location_label,
        "location_source": draft.location_source,
        "needs_location": point is None,
        "extraction": draft.extraction,
    }


EXECUTORS = {
    "get_air": get_air,
    "plan_routes": plan_routes,
    "find_reports": find_reports,
    "draft_report": draft_report,
}
ALLOWED_ARGS = {
    "get_air": {"place", "when", "forecast_hours"},
    "plan_routes": {"origin", "destination", "mode", "when"},
    "find_reports": {"place", "category", "radius_m"},
    "draft_report": {"category", "severity", "description", "place"},
}


async def execute(ctx: AgentContext, name: str, arguments: dict) -> ToolResult:
    executor = EXECUTORS.get(name)
    if executor is None:
        return ToolResult({"error": f"Unealtă necunoscută: {name}"})
    args = {k: v for k, v in arguments.items() if k in ALLOWED_ARGS[name]}
    try:
        return await executor(ctx, **args)
    except Unresolved as unresolved:
        return unresolved.result
    except AppError as error:
        return ToolResult({"error": error.message})
    except TypeError:
        return ToolResult({"error": "Argumente lipsă sau invalide pentru unealtă."})
