from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.entities import Report, Vote
from app.services.scoring import aware

EXPIRY_HOURS = {
    "construction": 24,
    "traffic": 2,
    "smoke": 6,
    "industrial": 12,
    "machinery": 8,
    "dust": 8,
    "other": 6,
}


async def vote_counts(db, report_ids):
    """Vote totals per report, loaded with one grouped query."""
    counts = {report_id: {} for report_id in report_ids}
    if not counts:
        return counts
    rows = await db.execute(
        select(Vote.report_id, Vote.kind, func.count())
        .where(Vote.report_id.in_(list(counts)))
        .group_by(Vote.report_id, Vote.kind)
    )
    for report_id, kind, total in rows:
        counts[report_id][kind] = total
    return counts


async def report_output(db, report, counts=None):
    if counts is None:
        counts = (await vote_counts(db, [report.id]))[report.id]
    age = max(0, (datetime.now(UTC) - aware(report.observed_at)).total_seconds() / 3600)
    confidence = max(0, min(90, 25 + counts.get("confirm", 0) * 12 - counts.get("dispute", 0) * 18 - age * 2))
    status = (
        "expired"
        if aware(report.expires_at) <= datetime.now(UTC) and report.status in {"pending", "confirmed"}
        else report.status
    )
    return {
        "id": report.id,
        "category": report.category,
        "description": report.description,
        "latitude": report.latitude,
        "longitude": report.longitude,
        "observed_at": aware(report.observed_at),
        "expires_at": aware(report.expires_at),
        "severity": report.severity,
        "status": status,
        "confirmations": counts.get("confirm", 0),
        "disputes": counts.get("dispute", 0),
        "confidence": round(confidence),
        "source_category": "COMMUNITY_REPORT",
    }


async def create_report(db, user, payload, *, commit=True):
    s, w, n, e = get_settings().bounds
    if not (s <= payload.latitude <= n and w <= payload.longitude <= e):
        raise HTTPException(422, "Report is outside the supported city area")
    report = Report(
        **payload.model_dump(),
        creator_id=user.id,
        location=f"SRID=4326;POINT({payload.longitude} {payload.latitude})",
        expires_at=payload.observed_at + timedelta(hours=EXPIRY_HOURS[payload.category]),
    )
    db.add(report)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return await report_output(db, report)


async def vote_report(db, user, report, kind):
    if report.creator_id == user.id:
        raise HTTPException(403, "You cannot vote on your own report")
    if report.status not in {"pending", "confirmed"} or aware(report.expires_at) <= datetime.now(UTC):
        raise HTTPException(409, "Report is no longer open for feedback")
    vote = Vote(report_id=report.id, user_id=user.id, kind=kind)
    db.add(vote)
    try:
        await db.flush()
        counts = dict(
            (
                await db.execute(
                    select(Vote.kind, func.count()).where(Vote.report_id == report.id).group_by(Vote.kind)
                )
            ).all()
        )
        report.status = (
            "confirmed"
            if counts.get("confirm", 0) >= 2 and counts.get("confirm", 0) > counts.get("dispute", 0)
            else "pending"
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You have already voted on this report") from None
    return await report_output(db, report)
