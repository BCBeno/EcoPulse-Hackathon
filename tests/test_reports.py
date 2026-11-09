from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import update

from app.devices.models import Device
from app.geo import point_wkt
from app.reports.models import Report, ReportDraft
from tests.conftest import new_device

CENTER = {"lat": 47.1585, "lon": 27.6014}


def payload(**overrides):
    return {"category": "smoke", "severity": 3, "description": "Fum gros lângă parc.", "location": CENTER} | (
        overrides
    )


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


async def device_id(client, headers) -> UUID:
    return UUID((await client.get("/devices/me", headers=headers)).json()["device_id"])


async def create(client, headers, **overrides) -> dict:
    response = await client.post("/reports", json=payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- creation -------------------------------------------------------------------------------------


async def test_create_report_applies_category_rules(client, alice):
    body = await create(client, alice)
    assert body["status"] == "active"
    assert body["radius_m"] == 400
    assert parse(body["expires_at"]) - parse(body["observed_at"]) == timedelta(hours=6)
    assert body["votes"] == {"still_there": 0, "gone": 0}
    assert body["viewer"] == {"is_author": True, "my_vote": None, "can_vote": False, "can_resolve": True}


async def test_create_requires_token(client):
    response = await client.post("/reports", json=payload())
    assert response.status_code == 401


async def test_create_outside_city_is_rejected(client, alice):
    response = await client.post(
        "/reports", json=payload(location={"lat": 44.43, "lon": 26.10}), headers=alice
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "outside_city"


async def test_validation_errors_are_in_romanian(client, alice):
    response = await client.post("/reports", json=payload(description="x", severity=9), headers=alice)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    fields = {f["field"]: f["message"] for f in error["fields"]}
    assert fields["description"] == "Textul este prea scurt."
    assert fields["severity"] == "Valoarea este prea mare."


@pytest.mark.parametrize(
    ("category", "age", "message"),
    [
        ("smoke", timedelta(minutes=-30), "Momentul observației nu poate fi în viitor."),
        ("smoke", timedelta(hours=49), "Observația trebuie să fie din ultimele 48 de ore."),
        ("traffic", timedelta(hours=3), "Observația e prea veche pentru categoria aleasă."),
    ],
)
async def test_observation_time_rules(client, alice, category, age, message):
    observed = (datetime.now(UTC) - age).isoformat()
    response = await client.post(
        "/reports", json=payload(category=category, observed_at=observed), headers=alice
    )
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [{"field": "observed_at", "message": message}]


async def test_description_whitespace_is_normalised(client, alice):
    body = await create(client, alice, description="  Fum   mult\n\nlângă  parc  ")
    assert body["description"] == "Fum mult lângă parc"


# --- reading --------------------------------------------------------------------------------------


async def test_public_read_has_no_viewer(client, alice):
    created = await create(client, alice)
    response = await client.get(f"/reports/{created['id']}")
    assert response.status_code == 200
    assert response.json()["viewer"] is None


async def test_hidden_report_is_not_found(client, alice, session):
    created = await create(client, alice)
    await session.execute(update(Report).where(Report.id == UUID(created["id"])).values(status="hidden"))
    await session.commit()
    assert (await client.get(f"/reports/{created['id']}")).status_code == 404
    listed = (await client.get("/reports")).json()["items"]
    assert listed == []


async def test_list_filters_by_status_category_and_bbox(client, alice, session):
    smoke = await create(client, alice)
    dust = await create(client, alice, category="dust", location={"lat": 47.20, "lon": 27.70})
    expired = await create(client, alice, category="dust")
    await session.execute(
        update(Report)
        .where(Report.id == UUID(expired["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()

    active = [r["id"] for r in (await client.get("/reports")).json()["items"]]
    assert active == [dust["id"], smoke["id"]]  # newest first
    assert [r["id"] for r in (await client.get("/reports?status=expired")).json()["items"]] == [expired["id"]]
    by_category = (await client.get("/reports?category=smoke")).json()["items"]
    assert [r["id"] for r in by_category] == [smoke["id"]]
    in_box = (await client.get("/reports?south=47.19&west=27.69&north=47.21&east=27.71")).json()["items"]
    assert [r["id"] for r in in_box] == [dust["id"]]


async def test_incomplete_bbox_is_rejected(client):
    response = await client.get("/reports?south=47.1&west=27.5")
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "bbox"


async def test_cursor_pagination(client, alice):
    ids = [(await create(client, alice))["id"] for _ in range(5)]
    first = (await client.get("/reports?limit=2")).json()
    second = (await client.get(f"/reports?limit=2&cursor={first['next_cursor']}")).json()
    third = (await client.get(f"/reports?limit=2&cursor={second['next_cursor']}")).json()
    seen = [r["id"] for page in (first, second, third) for r in page["items"]]
    assert seen == list(reversed(ids))
    assert third["next_cursor"] is None


# --- votes and resolution -------------------------------------------------------------------------


async def test_cannot_vote_on_own_report(client, alice):
    created = await create(client, alice)
    response = await client.post(f"/reports/{created['id']}/votes", json={"kind": "gone"}, headers=alice)
    assert response.status_code == 403


async def test_still_there_vote_extends_expiry(client, alice, bob, session):
    created = await create(client, alice)
    soon = datetime.now(UTC) + timedelta(minutes=10)
    await session.execute(update(Report).where(Report.id == UUID(created["id"])).values(expires_at=soon))
    await session.commit()
    body = (
        await client.post(f"/reports/{created['id']}/votes", json={"kind": "still_there"}, headers=bob)
    ).json()
    assert body["votes"] == {"still_there": 1, "gone": 0}
    assert body["viewer"]["my_vote"] == "still_there"
    assert parse(body["expires_at"]) >= datetime.now(UTC) + timedelta(hours=2, minutes=59)


async def test_changing_vote_replaces_previous(client, alice, bob):
    created = await create(client, alice)
    await client.post(f"/reports/{created['id']}/votes", json={"kind": "still_there"}, headers=bob)
    body = (await client.post(f"/reports/{created['id']}/votes", json={"kind": "gone"}, headers=bob)).json()
    assert body["votes"] == {"still_there": 0, "gone": 1}


async def test_three_gone_votes_resolve_the_report(client, alice):
    created = await create(client, alice)
    voters = [await new_device(client) for _ in range(3)]
    for index, voter in enumerate(voters):
        response = await client.post(f"/reports/{created['id']}/votes", json={"kind": "gone"}, headers=voter)
        assert response.status_code == 200
        assert response.json()["status"] == ("resolved" if index == 2 else "active")
    late = await new_device(client)
    response = await client.post(f"/reports/{created['id']}/votes", json={"kind": "gone"}, headers=late)
    assert response.status_code == 409


async def test_gone_votes_must_outnumber_confirmations(client, alice):
    created = await create(client, alice)
    for kind in ("still_there", "still_there", "still_there", "gone", "gone", "gone"):
        voter = await new_device(client)
        body = (
            await client.post(f"/reports/{created['id']}/votes", json={"kind": kind}, headers=voter)
        ).json()
    assert body["status"] == "active"


async def test_only_author_or_admin_resolves(client, alice, bob, session):
    created = await create(client, alice)
    assert (await client.post(f"/reports/{created['id']}/resolve", headers=bob)).status_code == 403
    response = await client.post(f"/reports/{created['id']}/resolve", headers=alice)
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert (await client.post(f"/reports/{created['id']}/resolve", headers=alice)).status_code == 409

    other = await create(client, alice)
    await session.execute(
        update(Device).where(Device.id == await device_id(client, bob)).values(role="admin")
    )
    await session.commit()
    response = await client.post(f"/reports/{other['id']}/resolve", headers=bob)
    assert response.status_code == 200


async def test_flag_is_idempotent(client, alice, bob):
    created = await create(client, alice)
    for _ in range(2):
        response = await client.post(f"/reports/{created['id']}/flags", json={"reason": "spam"}, headers=bob)
        assert response.status_code == 204


# --- drafts ---------------------------------------------------------------------------------------


async def make_draft(session, owner: UUID, *, location=True, created_at=None) -> UUID:
    draft = ReportDraft(
        device_id=owner,
        category="smoke",
        severity=3,
        description="Fum gros la Podu Roș.",
        original_text="e fum mult la podu ros",
        location=point_wkt(47.1536, 27.5876) if location else None,
        location_label="Podu Roș" if location else None,
        location_source="place" if location else "none",
        extraction="llm",
        created_at=created_at or datetime.now(UTC),
    )
    session.add(draft)
    await session.commit()
    return draft.id


async def test_publish_draft_creates_report(client, alice, session):
    draft = await make_draft(session, await device_id(client, alice))
    response = await client.post(f"/reports/drafts/{draft}/publish", json={}, headers=alice)
    assert response.status_code == 201
    body = response.json()
    assert body["location_label"] == "Podu Roș"
    assert body["location"] == {"lat": 47.1536, "lon": 27.5876}
    again = await client.post(f"/reports/drafts/{draft}/publish", json={}, headers=alice)
    assert again.status_code == 409


async def test_publish_applies_user_corrections(client, alice, session):
    draft = await make_draft(session, await device_id(client, alice))
    corrected = {"location": {"lat": 47.16, "lon": 27.59}, "category": "dust", "severity": 1}
    body = (await client.post(f"/reports/drafts/{draft}/publish", json=corrected, headers=alice)).json()
    assert (body["category"], body["severity"], body["location_label"]) == ("dust", 1, None)
    assert body["location"] == {"lat": 47.16, "lon": 27.59}


async def test_draft_without_location_needs_one(client, alice, session):
    draft = await make_draft(session, await device_id(client, alice), location=False)
    response = await client.post(f"/reports/drafts/{draft}/publish", json={}, headers=alice)
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "location"


async def test_drafts_are_private(client, alice, bob, session):
    draft = await make_draft(session, await device_id(client, alice))
    assert (await client.post(f"/reports/drafts/{draft}/publish", json={}, headers=bob)).status_code == 404


async def test_cancelled_and_expired_drafts_cannot_be_published(client, alice, session):
    owner = await device_id(client, alice)
    draft = await make_draft(session, owner)
    assert (await client.delete(f"/reports/drafts/{draft}", headers=alice)).status_code == 204
    assert (await client.post(f"/reports/drafts/{draft}/publish", json={}, headers=alice)).status_code == 409

    old = await make_draft(session, owner, created_at=datetime.now(UTC) - timedelta(hours=25))
    response = await client.post(f"/reports/drafts/{old}/publish", json={}, headers=alice)
    assert response.status_code == 409
    assert "expirat" in response.json()["error"]["message"]
