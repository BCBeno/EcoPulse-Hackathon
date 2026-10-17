from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import (
    admin_user,
    auth_limit,
    current_user,
    geocoding_limit,
    routing_limit,
    write_limit,
)
from app.core.config import get_settings
from app.core.security import create_token, password_hash
from app.db.session import get_db
from app.models.entities import (
    Conversation,
    DataSource,
    IngestionRun,
    Journey,
    MappedFeature,
    Observation,
    PlannedWork,
    Report,
    Station,
    User,
    WeatherForecast,
)
from app.schemas.domain import (
    JourneyCreate,
    Login,
    MessageCreate,
    Register,
    ReportCategory,
    ReportCreate,
    RouteRequest,
    TextReport,
    UserOut,
    WorkReview,
)
from app.services.assistant import answer_with_optional_llm
from app.services.assistant_reporting import create_pollution_report
from app.services.journeys import calculate
from app.services.quick_report import publish_from_text
from app.services.reports import RESOLVE_DISPUTES, create_report, report_output, vote_counts, vote_report
from app.services.scoring import aware

router = APIRouter(prefix="/api/v1")
DB = Annotated[AsyncSession, Depends(get_db)]


async def get_or_404(db, model, key):
    item = await db.get(model, key)
    if item is None:
        raise HTTPException(404, "Record not found")
    return item


@router.post("/auth/register", response_model=UserOut, status_code=201, dependencies=[Depends(auth_limit)])
async def register(payload: Register, db: DB):
    user = User(
        email=str(payload.email).lower(),
        display_name=payload.display_name.strip(),
        password_hash=await run_in_threadpool(password_hash.hash, payload.password),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Email is already registered") from None
    return user


@router.post("/auth/login", dependencies=[Depends(auth_limit)])
async def login(payload: Login, db: DB):
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()))
    # A dummy hash avoids the fast unknown-user path.
    hashed = user.password_hash if user else DUMMY_HASH
    valid = await run_in_threadpool(password_hash.verify, payload.password, hashed)
    if not user or not user.active or not valid:
        raise HTTPException(401, "Invalid credentials")
    return {
        "access_token": create_token(user.id),
        "token_type": "bearer",
        "user": UserOut.model_validate(user),
    }


DUMMY_HASH = password_hash.hash("unavailable-account-dummy-password")


@router.get("/auth/me", response_model=UserOut)
async def me(user=Depends(current_user)):
    return user


@router.get("/config")
async def public_config():
    s = get_settings()
    return {
        "city": s.city_name,
        "timezone": s.city_timezone,
        "center": {"latitude": s.city_latitude, "longitude": s.city_longitude},
        "bbox": s.bounds,
        "tile_url": s.map_tile_url,
        "modes": ["walk", "bike"] + (["motorcycle"] if s.motorcycle_enabled else []),
        "routing_configured": bool(s.valhalla_base_url),
        "report_resolve_disputes": RESOLVE_DISPUTES,
        "attributions": [
            {"label": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"},
            {"label": "Sensor.Community", "url": "https://sensor.community/"},
            {"label": "OpenAQ / original providers", "url": "https://openaq.org/"},
            {
                "label": "Open-Meteo / CAMS (CC BY 4.0)",
                "url": "https://open-meteo.com/en/docs/air-quality-api",
            },
        ],
    }


@router.get("/reports")
async def reports(
    db: DB,
    category: ReportCategory | None = None,
    status: Literal["pending", "confirmed", "resolved", "hidden", "expired"] | None = None,
    bbox: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
):
    query = select(Report).where(Report.status != "hidden")
    now = datetime.now(UTC)
    if status == "hidden":
        raise HTTPException(403, "Hidden reports are not public")
    if status == "expired":
        query = query.where(Report.expires_at <= now, Report.status.in_(["pending", "confirmed"]))
    elif status:
        query = query.where(Report.status == status)
        if status in {"pending", "confirmed"}:
            query = query.where(Report.expires_at > now)
    else:
        query = query.where(Report.status.in_(["pending", "confirmed"]), Report.expires_at > now)
    if category:
        query = query.where(Report.category == category)
    for stamp in (since, until):
        if stamp and stamp.tzinfo is None:
            raise HTTPException(422, "Time filters require timezone offsets")
    if since:
        query = query.where(Report.observed_at >= since.astimezone(UTC))
    if until:
        query = query.where(Report.observed_at <= until.astimezone(UTC))
    if bbox:
        s, w, n, e = validate_bbox(bbox)
        if db.bind.dialect.name == "postgresql":
            query = query.where(func.ST_Intersects(Report.location, func.ST_MakeEnvelope(w, s, e, n, 4326)))
        else:
            query = query.where(Report.latitude.between(s, n), Report.longitude.between(w, e))
    rows = (await db.scalars(query.order_by(Report.created_at.desc()).offset(offset).limit(limit))).all()
    counts = await vote_counts(db, [r.id for r in rows])
    return {
        "items": [await report_output(db, r, counts[r.id]) for r in rows],
        "limit": limit,
        "offset": offset,
    }


def validate_bbox(value):
    try:
        s, w, n, e = [float(x) for x in value.split(",")]
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180 and n - s <= 2 and e - w <= 2):
            raise ValueError()
        return s, w, n, e
    except ValueError:
        raise HTTPException(422, "bbox must be south,west,north,east with maximum span 2 degrees") from None


@router.get("/reports/{report_id}")
async def report_detail(report_id: str, db: DB):
    report = await get_or_404(db, Report, report_id)
    if report.status == "hidden":
        raise HTTPException(404, "Record not found")
    return await report_output(db, report)


@router.post("/reports", status_code=201, dependencies=[Depends(write_limit)])
async def report_create(payload: ReportCreate, db: DB, user=Depends(current_user)):
    return await create_report(db, user, payload)


@router.post("/reports/from-text", status_code=201, dependencies=[Depends(write_limit)])
async def report_from_text(payload: TextReport, request: Request, db: DB, user=Depends(current_user)):
    s, w, n, e = get_settings().bounds
    if not (s <= payload.latitude <= n and w <= payload.longitude <= e):
        raise HTTPException(422, "Locația curentă este în afara zonei acoperite de EcoPulse (Iași).")
    return await publish_from_text(
        db,
        user,
        payload.text.strip(),
        payload.latitude,
        payload.longitude,
        get_settings(),
        request.app.state.http,
    )


@router.get("/reports/{report_id}/feedback")
async def report_feedback(report_id: str, db: DB, user=Depends(current_user)):
    from app.models.entities import Vote

    report = await get_or_404(db, Report, report_id)
    if report.status == "hidden":
        raise HTTPException(404, "Record not found")
    vote = await db.scalar(select(Vote.kind).where(Vote.report_id == report_id, Vote.user_id == user.id))
    reason = (
        "own_report"
        if report.creator_id == user.id
        else "closed"
        if report.status not in {"pending", "confirmed"} or aware(report.expires_at) <= datetime.now(UTC)
        else "already_voted"
        if vote
        else None
    )
    return {
        "can_vote": reason is None,
        "reason": reason,
        "my_vote": vote,
        "can_resolve": report.creator_id == user.id or user.role == "admin",
    }


@router.post("/reports/{report_id}/{action}", dependencies=[Depends(write_limit)])
async def report_action(
    report_id: str,
    action: Literal["confirm", "dispute", "resolve", "flag"],
    db: DB,
    user=Depends(current_user),
):
    report = await get_or_404(db, Report, report_id)
    if report.status == "hidden":
        raise HTTPException(404, "Record not found")
    if action in {"confirm", "dispute"}:
        return await vote_report(db, user, report, action)
    if action == "resolve":
        if user.id != report.creator_id and user.role != "admin":
            raise HTTPException(403, "Only the author or administrator can resolve a report")
        report.status = "resolved"
    else:
        # Flagging does not publicly hide a report or expose reporter identities.
        flags = list(report.moderation.get("flagged_by", []))
        if user.id not in flags:
            flags.append(user.id)
        report.moderation = {**report.moderation, "flagged_by": flags}
    await db.commit()
    return await report_output(db, report)


@router.post("/admin/reports/{report_id}/hide")
async def hide_report(report_id: str, db: DB, user=Depends(admin_user)):
    report = await get_or_404(db, Report, report_id)
    report.status = "hidden"
    report.moderation = {
        **report.moderation,
        "moderator_id": user.id,
        "hidden_at": datetime.now(UTC).isoformat(),
    }
    await db.commit()
    return {"status": "hidden"}


@router.get("/geocoding/search", dependencies=[Depends(geocoding_limit)])
async def geocode(request: Request, q: str = Query(min_length=3, max_length=200)):
    return {"items": await request.app.state.geocoder.search(q)}


@router.post("/routes/alternatives", dependencies=[Depends(routing_limit)])
async def route_alternatives(payload: RouteRequest, request: Request, db: DB):
    return await calculate(db, payload, get_settings(), request.app.state.http)


@router.get("/environment/observations")
async def environment(
    db: DB,
    category: Literal[
        "OFFICIAL_MEASUREMENT", "LOW_COST_SENSOR_MEASUREMENT", "MODEL_FORECAST", "WEATHER_CONTEXT"
    ]
    | None = None,
    parameter: str | None = Query(None, max_length=60),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    query = select(Observation)
    if category:
        query = query.where(Observation.category == category)
    if parameter:
        query = query.where(Observation.parameter == parameter)
    for stamp in (since, until):
        if stamp and stamp.tzinfo is None:
            raise HTTPException(422, "Time filters require timezone offsets")
    # Filter on the valid time, so clients can ask for current readings rather than the newest rows.
    # Normalised to UTC: SQLite stores timestamps without their offset.
    if since:
        query = query.where(Observation.observed_at >= since.astimezone(UTC))
    if until:
        query = query.where(Observation.observed_at <= until.astimezone(UTC))
    rows = (
        await db.scalars(
            query.order_by(Observation.retrieved_at.desc(), Observation.observed_at.desc()).limit(limit)
        )
    ).all()
    return {
        "items": [measurement_output(x) for x in rows],
        "available": bool(rows),
        "note": "An empty cache is not evidence of clean air.",
    }


def measurement_output(x):
    age = (datetime.now(UTC) - aware(x.retrieved_at)).total_seconds()
    if x.category != "MODEL_FORECAST" and x.provenance.get("product") != "forecast":
        age = (datetime.now(UTC) - aware(x.observed_at)).total_seconds()
    return {
        "id": x.id,
        "provider": x.provider,
        "sensor_id": x.sensor_id,
        "source_record_id": x.source_record_id,
        "category": x.category,
        "parameter": x.parameter,
        "value": x.value,
        "unit": x.unit,
        "original_value": x.original_value,
        "original_unit": x.original_unit,
        "latitude": x.latitude,
        "longitude": x.longitude,
        "observed_at": aware(x.observed_at),
        "retrieved_at": aware(x.retrieved_at),
        "freshness": "fresh" if age <= (21600 if x.category == "MODEL_FORECAST" else 7200) else "stale",
        "quality": x.quality,
        "provenance": x.provenance,
    }


@router.get("/environment/weather")
async def weather(db: DB, limit: int = Query(200, ge=1, le=500)):
    rows = (
        await db.scalars(
            select(WeatherForecast)
            .where(WeatherForecast.observed_at >= datetime.now(UTC))
            .order_by(WeatherForecast.observed_at)
            .limit(limit)
        )
    ).all()
    return {"items": [measurement_output(x) for x in rows], "available": bool(rows)}


@router.get("/environment/stations")
async def stations(db: DB):
    rows = (await db.scalars(select(Station).where(Station.enabled))).all()
    return {
        "items": [
            {
                "id": x.id,
                "external_id": x.external_id,
                "provider": x.provider,
                "name": x.name,
                "latitude": x.latitude,
                "longitude": x.longitude,
                "metadata": x.details,
            }
            for x in rows
        ]
    }


@router.get("/disruptions/features")
async def mapped_features(db: DB, limit: int = Query(100, ge=1, le=500)):
    rows = (
        await db.scalars(select(MappedFeature).order_by(MappedFeature.retrieved_at.desc()).limit(limit))
    ).all()
    return {
        "items": [
            {
                "id": x.id,
                "source_id": x.source_id,
                "category": "OSM_MAPPED_FEATURE",
                "latitude": x.latitude,
                "longitude": x.longitude,
                "geometry": x.geometry,
                "tags": x.tags,
                "retrieved_at": x.retrieved_at,
                "provenance": x.provenance,
            }
            for x in rows
        ]
    }


@router.get("/disruptions/planned")
async def planned_works(db: DB):
    # Only works that can still affect the map: cancelled, finished or unlocated works are omitted.
    rows = (
        await db.scalars(
            select(PlannedWork)
            .where(
                PlannedWork.verification == "verified",
                PlannedWork.status.in_(["scheduled", "active"]),
                PlannedWork.ends_at >= datetime.now(UTC),
                PlannedWork.latitude.is_not(None),
                PlannedWork.longitude.is_not(None),
            )
            .order_by(PlannedWork.starts_at)
            .limit(100)
        )
    ).all()
    return {"items": [work_output(x) for x in rows]}


def work_output(x):
    return {
        "id": x.id,
        "title": x.title,
        "description": x.description,
        "original_url": x.original_url,
        "latitude": x.latitude,
        "longitude": x.longitude,
        "starts_at": x.starts_at,
        "ends_at": x.ends_at,
        "status": x.status,
        "verification": x.verification,
        "last_verified_at": x.last_verified_at,
        "category": "OFFICIAL_PLANNED_WORK",
        "provenance": x.provenance,
    }


@router.get("/admin/works/review", dependencies=[Depends(admin_user)])
async def work_review_queue(db: DB):
    rows = (
        await db.scalars(select(PlannedWork).where(PlannedWork.verification == "needs_review").limit(100))
    ).all()
    return {"items": [work_output(x) for x in rows]}


@router.put("/admin/works/{work_id}", dependencies=[Depends(admin_user)])
async def review_work(work_id: str, payload: WorkReview, db: DB):
    item = await get_or_404(db, PlannedWork, work_id)
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    item.verification, item.last_verified_at, item.extraction_confidence = "verified", datetime.now(UTC), 1.0
    await db.commit()
    return work_output(item)


@router.get("/admin/data-sources/status", dependencies=[Depends(admin_user)])
async def source_status(db: DB):
    rows = (await db.scalars(select(DataSource))).all()
    return {
        "items": [
            {
                "name": x.name,
                "enabled": x.enabled,
                "configuration_missing": x.configuration_missing,
                "last_attempt": x.last_attempt,
                "last_success": x.last_success,
                "latest_observation_at": x.latest_observation_at,
                "freshness_seconds": max(0, (datetime.now(UTC) - aware(x.last_success)).total_seconds())
                if x.last_success
                else None,
                "records_processed": x.records_processed,
                "last_error": x.last_error,
            }
            for x in rows
        ]
    }


@router.get("/admin/data-sources/runs", dependencies=[Depends(admin_user)])
async def source_runs(db: DB):
    rows = (await db.scalars(select(IngestionRun).order_by(IngestionRun.created_at.desc()).limit(100))).all()
    return {
        "items": [
            {
                "id": x.id,
                "source": x.source,
                "started_at": x.created_at,
                "finished_at": x.finished_at,
                "status": x.status,
                "discovered": x.discovered,
                "updated": x.updated,
                "skipped": x.skipped,
                "error": x.error,
            }
            for x in rows
        ]
    }


@router.post("/journeys", status_code=201, dependencies=[Depends(write_limit)])
async def journey_create(payload: JourneyCreate, db: DB, user=Depends(current_user)):
    item = Journey(owner_id=user.id, name=payload.name, request=payload.request.model_dump(mode="json"))
    db.add(item)
    await db.commit()
    return journey_output(item)


def journey_output(item):
    return {"id": item.id, "name": item.name, "request": item.request, "created_at": item.created_at}


async def owned(db, model, key, user):
    item = await get_or_404(db, model, key)
    if item.owner_id != user.id:
        raise HTTPException(404, "Record not found")
    return item


@router.get("/journeys")
async def journeys(db: DB, user=Depends(current_user)):
    rows = (
        await db.scalars(
            select(Journey).where(Journey.owner_id == user.id).order_by(Journey.created_at.desc()).limit(100)
        )
    ).all()
    return {"items": [journey_output(x) for x in rows]}


@router.get("/journeys/{journey_id}")
async def journey_get(journey_id: str, db: DB, user=Depends(current_user)):
    return journey_output(await owned(db, Journey, journey_id, user))


@router.put("/journeys/{journey_id}")
async def journey_update(journey_id: str, payload: JourneyCreate, db: DB, user=Depends(current_user)):
    item = await owned(db, Journey, journey_id, user)
    item.name, item.request = payload.name, payload.request.model_dump(mode="json")
    await db.commit()
    return journey_output(item)


@router.delete("/journeys/{journey_id}", status_code=204)
async def journey_delete(journey_id: str, db: DB, user=Depends(current_user)):
    await db.delete(await owned(db, Journey, journey_id, user))
    await db.commit()
    return Response(status_code=204)


@router.post("/assistant/conversations", status_code=201)
async def conversation_create(db: DB, user=Depends(current_user)):
    item = Conversation(owner_id=user.id, messages=[])
    db.add(item)
    await db.commit()
    return {"id": item.id, "messages": []}


@router.get("/assistant/conversations/{conversation_id}")
async def conversation_get(conversation_id: str, db: DB, user=Depends(current_user)):
    item = await owned(db, Conversation, conversation_id, user)
    return {"id": item.id, "messages": item.messages}


@router.post(
    "/assistant/conversations/{conversation_id}/messages",
    dependencies=[Depends(routing_limit), Depends(write_limit)],
)
async def conversation_message(
    conversation_id: str, payload: MessageCreate, request: Request, db: DB, user=Depends(current_user)
):
    item = await owned(db, Conversation, conversation_id, user)
    message_id = str(payload.client_message_id) if payload.client_message_id else None
    previous_index = next(
        (
            i
            for i, m in enumerate(item.messages)
            if message_id and m.get("client_message_id") == message_id and m["role"] == "user"
        ),
        None,
    )
    if previous_index is not None:
        if item.messages[previous_index]["content"] != payload.content:
            raise HTTPException(409, "Message identifier has already been used")
        previous = item.messages[previous_index + 1]
        previous_status = previous.get("action", {}).get("status")
        if previous_status not in {"needs_location", "needs_location_choice"} or (
            previous_status == "needs_location_choice" and payload.location_candidate_id is None
        ):
            return {k: v for k, v in previous.items() if k not in {"role", "client_message_id"}}
    if previous_index is None and len(item.messages) >= 100:
        raise HTTPException(409, "Conversation limit reached; create a new conversation")
    result = await create_pollution_report(
        db, user, payload, get_settings(), conversation_id, item.messages, geocoder=request.app.state.geocoder
    )
    if result is None:
        result = await answer_with_optional_llm(db, user, payload, get_settings(), request.app.state.http)
    # A concurrent retry may have rolled back its reservation; refresh after tool execution.
    await db.refresh(item)
    pair = [
        {"role": "user", "content": payload.content, "client_message_id": message_id},
        {"role": "assistant", **jsonable_encoder(result), "client_message_id": message_id},
    ]
    messages = list(item.messages)
    existing = next(
        (
            i
            for i, m in enumerate(messages)
            if message_id and m.get("client_message_id") == message_id and m["role"] == "user"
        ),
        None,
    )
    if existing is None:
        messages.extend(pair)
    else:
        messages[existing : existing + 2] = pair
    item.messages = messages
    await db.commit()
    return result


@router.get("/health/ready")
async def readiness(db: DB):
    await db.execute(text("SELECT 1"))
    return {"status": "ready"}
