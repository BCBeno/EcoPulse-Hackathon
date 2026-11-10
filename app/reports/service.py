"""Report rules: creation, drafts, votes, resolution and flags."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Bounds, Settings
from app.devices.models import Device
from app.errors import conflict, forbidden, invalid, not_found, outside_city
from app.geo import LatLon, point_wkt, to_latlon
from app.reports.categories import CATEGORIES, GONE_VOTES_TO_RESOLVE, MAX_OBSERVED_AGE_H
from app.reports.models import Report, ReportDraft, ReportFlag, ReportVote
from app.reports.schemas import DraftPublish, ReportCreate, ReportOut, ReportPage, Viewer, VoteCounts

DRAFT_TTL = timedelta(hours=24)
FUTURE_TOLERANCE = timedelta(minutes=5)


def public_status(report: Report, now: datetime) -> str:
    if report.status == "active" and report.expires_at <= now:
        return "expired"
    return report.status


def _check_location(location: LatLon, settings: Settings) -> None:
    if not settings.city_bbox.contains(location.lat, location.lon):
        raise outside_city()


def _expiry(category: str, observed_at: datetime, now: datetime) -> datetime:
    if observed_at > now + FUTURE_TOLERANCE:
        raise invalid("observed_at", "Momentul observației nu poate fi în viitor.")
    if observed_at < now - timedelta(hours=MAX_OBSERVED_AGE_H):
        raise invalid("observed_at", "Observația trebuie să fie din ultimele 48 de ore.")
    expires_at = observed_at + timedelta(hours=CATEGORIES[category].duration_h)
    if expires_at <= now:
        raise invalid("observed_at", "Observația e prea veche pentru categoria aleasă.")
    return expires_at


async def create_report(
    session: AsyncSession,
    device: Device,
    data: ReportCreate,
    settings: Settings,
    *,
    source: str = "form",
    extraction: str = "manual",
    original_text: str | None = None,
    now: datetime | None = None,
) -> Report:
    now = now or datetime.now(UTC)
    _check_location(data.location, settings)
    observed_at = data.observed_at or now
    report = Report(
        device_id=device.id,
        category=data.category,
        severity=data.severity,
        description=data.description or f"{CATEGORIES[data.category].label}.",
        original_text=original_text,
        location=point_wkt(data.location.lat, data.location.lon),
        location_label=data.location_label,
        radius_m=CATEGORIES[data.category].radius_m,
        observed_at=observed_at,
        expires_at=_expiry(data.category, observed_at, now),
        source=source,
        extraction=extraction,
        created_at=now,
    )
    session.add(report)
    await session.flush()
    return report


async def visible_report(session: AsyncSession, report_id: UUID, *, for_update: bool = False) -> Report:
    report = await session.get(Report, report_id, with_for_update=for_update)
    if report is None or report.status == "hidden":
        raise not_found("Raportul nu există.")
    return report


async def _vote_counts(session: AsyncSession, ids: list[UUID]) -> dict[UUID, VoteCounts]:
    counts: dict[UUID, VoteCounts] = {i: VoteCounts() for i in ids}
    if not ids:
        return counts
    rows = await session.execute(
        select(ReportVote.report_id, ReportVote.kind, func.count())
        .where(ReportVote.report_id.in_(ids))
        .group_by(ReportVote.report_id, ReportVote.kind)
    )
    for report_id, kind, total in rows:
        setattr(counts[report_id], kind, total)
    return counts


async def _my_votes(session: AsyncSession, ids: list[UUID], device: Device | None) -> dict[UUID, str]:
    if device is None or not ids:
        return {}
    rows = await session.execute(
        select(ReportVote.report_id, ReportVote.kind).where(
            ReportVote.report_id.in_(ids), ReportVote.device_id == device.id
        )
    )
    return {report_id: kind for report_id, kind in rows}


def _viewer(report: Report, device: Device, my_vote: str | None, status: str) -> Viewer:
    is_author = report.device_id == device.id
    return Viewer(
        is_author=is_author,
        my_vote=my_vote,
        can_vote=status == "active" and not is_author,
        can_resolve=status == "active" and (is_author or device.role == "admin"),
    )


async def to_out(
    session: AsyncSession, reports: list[Report], device: Device | None, now: datetime | None = None
) -> list[ReportOut]:
    now = now or datetime.now(UTC)
    ids = [r.id for r in reports]
    counts = await _vote_counts(session, ids)
    mine = await _my_votes(session, ids, device)
    output = []
    for report in reports:
        status = public_status(report, now)
        output.append(
            ReportOut(
                id=report.id,
                category=report.category,
                severity=report.severity,
                description=report.description,
                location=to_latlon(report.location),
                location_label=report.location_label,
                radius_m=report.radius_m,
                observed_at=report.observed_at,
                expires_at=report.expires_at,
                status=status,
                votes=counts[report.id],
                created_at=report.created_at,
                viewer=_viewer(report, device, mine.get(report.id), status) if device else None,
            )
        )
    return output


async def one_out(session: AsyncSession, report: Report, device: Device | None) -> ReportOut:
    return (await to_out(session, [report], device))[0]


def _encode_cursor(report: Report) -> str:
    raw = f"{report.created_at.isoformat()}|{report.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, report_id = raw.split("|")
        return datetime.fromisoformat(stamp), UUID(report_id)
    except ValueError:
        raise invalid("cursor", "Cursor invalid.") from None


async def list_reports(
    session: AsyncSession,
    *,
    status: str,
    category: str | None,
    bbox: Bounds | None,
    limit: int,
    cursor: str | None,
    device: Device | None,
    now: datetime | None = None,
) -> ReportPage:
    now = now or datetime.now(UTC)
    query = select(Report)
    if status == "active":
        query = query.where(Report.status == "active", Report.expires_at > now)
    elif status == "expired":
        query = query.where(Report.status == "active", Report.expires_at <= now)
    else:
        query = query.where(Report.status == "resolved")
    if category:
        query = query.where(Report.category == category)
    if bbox:
        envelope = func.ST_MakeEnvelope(bbox.west, bbox.south, bbox.east, bbox.north, 4326)
        query = query.where(func.ST_Intersects(Report.location, envelope))
    if cursor:
        query = query.where(tuple_(Report.created_at, Report.id) < tuple_(*_decode_cursor(cursor)))
    rows = list(
        await session.scalars(query.order_by(Report.created_at.desc(), Report.id.desc()).limit(limit + 1))
    )
    page, more = rows[:limit], len(rows) > limit
    return ReportPage(
        items=await to_out(session, page, device, now),
        next_cursor=_encode_cursor(page[-1]) if more and page else None,
    )


async def vote(
    session: AsyncSession, device: Device, report_id: UUID, kind: str, now: datetime | None = None
) -> Report:
    now = now or datetime.now(UTC)
    report = await visible_report(session, report_id, for_update=True)
    if report.device_id == device.id:
        raise forbidden("Nu poți vota propriul raport.")
    if public_status(report, now) != "active":
        raise conflict("Raportul nu mai este activ.")
    await session.execute(
        insert(ReportVote)
        .values(report_id=report.id, device_id=device.id, kind=kind, created_at=now)
        .on_conflict_do_update(
            index_elements=[ReportVote.report_id, ReportVote.device_id],
            set_={"kind": kind, "created_at": now},
        )
    )
    counts = (await _vote_counts(session, [report.id]))[report.id]
    if kind == "still_there":
        half = timedelta(hours=CATEGORIES[report.category].duration_h / 2)
        report.expires_at = max(report.expires_at, now + half)
    if counts.gone >= GONE_VOTES_TO_RESOLVE and counts.gone > counts.still_there:
        report.status, report.resolved_by, report.resolved_at = "resolved", "community", now
    await session.flush()
    return report


async def resolve(
    session: AsyncSession, device: Device, report_id: UUID, now: datetime | None = None
) -> Report:
    now = now or datetime.now(UTC)
    report = await visible_report(session, report_id, for_update=True)
    is_author = report.device_id == device.id
    if not (is_author or device.role == "admin"):
        raise forbidden("Doar autorul poate închide raportul.")
    if public_status(report, now) != "active":
        raise conflict("Raportul nu mai este activ.")
    report.status, report.resolved_at = "resolved", now
    report.resolved_by = "author" if is_author else "admin"
    await session.flush()
    return report


async def flag(session: AsyncSession, device: Device, report_id: UUID, reason: str) -> None:
    report = await visible_report(session, report_id)
    await session.execute(
        insert(ReportFlag)
        .values(report_id=report.id, device_id=device.id, reason=reason)
        .on_conflict_do_nothing()
    )


async def _own_pending_draft(
    session: AsyncSession, device: Device, draft_id: UUID, now: datetime
) -> ReportDraft:
    draft = await session.get(ReportDraft, draft_id, with_for_update=True)
    if draft is None or draft.device_id != device.id:
        raise not_found("Ciorna nu există.")
    if draft.status == "published":
        raise conflict("Raportul a fost deja publicat.")
    if draft.status == "cancelled":
        raise conflict("Ciorna a fost anulată.")
    if draft.created_at + DRAFT_TTL <= now:
        raise conflict("Ciorna a expirat. Descrie din nou problema.")
    return draft


async def publish_draft(
    session: AsyncSession,
    device: Device,
    draft_id: UUID,
    changes: DraftPublish,
    settings: Settings,
    now: datetime | None = None,
) -> Report:
    now = now or datetime.now(UTC)
    draft = await _own_pending_draft(session, device, draft_id, now)
    location = changes.location or (to_latlon(draft.location) if draft.location is not None else None)
    if location is None:
        raise invalid("location", "Alege locul pe hartă înainte de publicare.")
    data = ReportCreate(
        category=changes.category or draft.category,
        severity=changes.severity or draft.severity,
        description=changes.description or draft.description,
        location=location,
        location_label=None if changes.location else draft.location_label,
    )
    report = await create_report(
        session,
        device,
        data,
        settings,
        source="agent",
        extraction=draft.extraction,
        original_text=draft.original_text,
        now=now,
    )
    draft.status, draft.report_id = "published", report.id
    await session.flush()
    return report


async def cancel_draft(session: AsyncSession, device: Device, draft_id: UUID) -> None:
    draft = await _own_pending_draft(session, device, draft_id, datetime.now(UTC))
    draft.status = "cancelled"
    await session.flush()


def active_filter(now: datetime):
    """SQL condition for reports currently shown on the map."""
    return and_(Report.status == "active", Report.expires_at > now)
