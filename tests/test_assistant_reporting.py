import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.session import Base, Session
from app.models.entities import AssistantAction, Report, User
from app.schemas.domain import MessageCreate
from app.services.assistant_reporting import create_pollution_report


def setup(client, headers):
    conversation = client.post("/api/v1/assistant/conversations", headers=headers).json()["id"]
    return f"/api/v1/assistant/conversations/{conversation}/messages"


def location(**changes):
    return {
        "latitude": 47.165,
        "longitude": 27.6,
        "accuracy_m": 20,
        "captured_at": datetime.now(UTC).isoformat(),
        "source": "browser_geolocation",
        **changes,
    }


def command(**changes):
    return {
        "content": "Vreau să raportez aici că este fum",
        "client_message_id": str(uuid4()),
        **changes,
    }


def counts(client):
    async def query():
        async with Session() as db:
            return (
                await db.scalar(select(func.count()).select_from(Report)),
                await db.scalar(select(func.count()).select_from(AssistantAction)),
            )

    return client.portal.call(query)


def test_report_requests_gps_then_creates_once_and_persists_chat(client, account):
    headers = account()
    endpoint = setup(client, headers)
    body = command()
    first = client.post(endpoint, headers=headers, json=body)
    assert first.json()["action"]["status"] == "needs_location"
    assert counts(client) == (0, 0)
    body["current_location"] = location()
    result = client.post(endpoint, headers=headers, json=body)
    assert result.status_code == 200, result.text
    report = result.json()["action"]["report"]
    assert report["category"] == "smoke"
    assert report["latitude"] == 47.165 and report["longitude"] == 27.6
    assert report["status"] == "pending" and report["severity"] == 2
    assert result.json()["tools_used"] == ["create_pollution_report"]
    assert counts(client) == (1, 1)
    # Repeated requests with the same ID return the original result, even if GPS changes.
    body["current_location"] = location(latitude=47.17)
    replay = client.post(endpoint, headers=headers, json=body)
    assert replay.json() == result.json()
    assert counts(client) == (1, 1)
    conversation = client.get(endpoint.removesuffix("/messages"), headers=headers).json()
    assert len(conversation["messages"]) == 2
    assert conversation["messages"][1]["action"]["report"]["id"] == report["id"]
    assert client.get(f"/api/v1/reports/{report['id']}").status_code == 200
    body["content"] = "Raportează praf aici"
    assert client.post(endpoint, headers=headers, json=body).status_code == 409


@pytest.mark.parametrize(
    "coords,status",
    [
        (location(accuracy_m=201), "needs_location"),
        (location(captured_at=(datetime.now(UTC) - timedelta(minutes=4)).isoformat()), "needs_location"),
        (location(captured_at=(datetime.now(UTC) + timedelta(minutes=4)).isoformat()), "needs_location"),
        (location(latitude=44.4, longitude=26.1), "unsupported_location"),
    ],
)
def test_unreliable_or_unsupported_location_never_writes(client, account, coords, status):
    headers = account()
    endpoint = setup(client, headers)
    result = client.post(endpoint, headers=headers, json=command(current_location=coords))
    assert result.status_code == 200, result.text
    assert result.json()["action"]["status"] == status
    assert counts(client) == (0, 0)


@pytest.mark.parametrize(
    "content",
    [
        "Cum raportez fum aici?",
        "Nu vreau să raportez fum aici",
        "Dacă ar fi fum aici, raportează-l",
        'Exemplu: "raportează fum aici"',
        "Raportează fum mâine aici",
        "Compară rutele disponibile",
        "Explică mesajul «raportează fum aici»",
    ],
)
def test_questions_negations_and_examples_do_not_create(client, account, content):
    headers = account()
    endpoint = setup(client, headers)
    result = client.post(
        endpoint, headers=headers, json=command(content=content, current_location=location())
    )
    assert result.status_code == 200
    assert result.json().get("action", {}).get("status") != "created"
    assert counts(client) == (0, 0)


def test_missing_details_and_authorization(client, account):
    headers, other = account(1), account(2)
    endpoint = setup(client, headers)
    assert client.post(endpoint, json=command()).status_code == 401
    assert client.post(endpoint, headers=other, json=command()).status_code == 404
    for content in ["Raportează fum", "Vreau să raportez aici"]:
        result = client.post(endpoint, headers=headers, json=command(content=content))
        assert result.json()["action"]["status"] == "needs_details"
    invalid = command(current_location=location(source="map_click"))
    assert client.post(endpoint, headers=headers, json=invalid).status_code == 422
    invalid = command(current_location=location())
    del invalid["client_message_id"]
    assert client.post(endpoint, headers=headers, json=invalid).status_code == 422
    assert counts(client) == (0, 0)


def test_action_id_cannot_be_reused_in_other_conversation(client, account):
    headers = account()
    first, second = setup(client, headers), setup(client, headers)
    body = command(current_location=location())
    assert client.post(first, headers=headers, json=body).json()["action"]["status"] == "created"
    assert client.post(second, headers=headers, json=body).status_code == 409
    assert counts(client) == (1, 1)


@pytest.mark.parametrize(
    "content", ["vreau sa raporetez fum aici", "vreau sa rapotrez fum aici", "te rog raporteza fum aici"]
)
def test_typo_reports_do_not_fall_through_to_read_tools(client, account, monkeypatch, content):
    from app.core.config import get_settings
    from app.integrations.openrouter import OpenRouterProvider

    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")

    async def unrelated_tool(*args):
        raise AssertionError("A report request must never be answered with a read-only tool")

    monkeypatch.setattr(OpenRouterProvider, "select", unrelated_tool)
    headers = account()
    endpoint = setup(client, headers)
    result = client.post(endpoint, headers=headers, json=command(content=content))
    assert result.json()["action"]["status"] == "needs_location"
    assert counts(client) == (0, 0)


def test_followup_category_resumes_report_and_gps_retry_preserves_description(client, account):
    headers = account()
    endpoint = setup(client, headers)
    first = client.post(endpoint, headers=headers, json=command(content="Vreau să raportez aici"))
    assert first.json()["action"]["status"] == "needs_details"
    body = command(content="fum")
    waiting = client.post(endpoint, headers=headers, json=body)
    assert waiting.json()["action"]["status"] == "needs_location"
    body["current_location"] = location()
    result = client.post(endpoint, headers=headers, json=body)
    assert result.status_code == 200, result.text
    report = result.json()["action"]["report"]
    assert report["description"] == "Vreau să raportez aici. fum"
    assert report["category"] == "smoke" and counts(client) == (1, 1)


def test_greeting_and_cancellation_do_not_call_llm_or_calculate_routes(client, account, monkeypatch):
    from app.core.config import get_settings
    from app.integrations.openrouter import OpenRouterProvider

    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")

    async def forbidden(*args):
        raise AssertionError("A greeting cannot invoke external tools")

    monkeypatch.setattr(OpenRouterProvider, "select", forbidden)
    headers = account()
    endpoint = setup(client, headers)
    route = {
        "origin": {"latitude": 47.15, "longitude": 27.6},
        "destination": {"latitude": 47.19, "longitude": 27.6},
    }
    result = client.post(endpoint, headers=headers, json=command(content="salut", route_request=route))
    assert result.json()["content"].startswith("Salut!")
    assert result.json()["tools_used"] == [] and "routes" not in result.json()
    client.post(endpoint, headers=headers, json=command(content="Vreau să raportez aici"))
    client.post(endpoint, headers=headers, json=command(content="anulează"))
    result = client.post(endpoint, headers=headers, json=command(content="fum", current_location=location()))
    assert result.json().get("action", {}).get("status") != "created"
    assert counts(client) == (0, 0)


def test_named_location_choices_publish_only_the_selected_server_point(client, account, monkeypatch):
    from app.main import app

    searches = []

    async def search(query):
        searches.append(query)
        return [
            {"label": "Parcul Copou, Iași", "latitude": 47.177, "longitude": 27.572},
            {"label": "Bulevardul Carol I, Copou, Iași", "latitude": 47.175, "longitude": 27.574},
        ]

    monkeypatch.setattr(app.state.geocoder, "search", search)
    headers = account()
    endpoint = setup(client, headers)
    body = command(content="este fum la copou")
    first = client.post(endpoint, headers=headers, json=body)
    action = first.json()["action"]
    assert action["status"] == "needs_location_choice" and len(action["candidates"]) == 2
    assert searches == ["copou"] and counts(client) == (0, 0)
    # A retry must retain the same candidate IDs and not re-geocode.
    assert client.post(endpoint, headers=headers, json=body).json() == first.json()
    body["location_candidate_id"] = str(uuid4())
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    body["location_candidate_id"] = action["candidates"][1]["id"]
    body["current_location"] = location()  # Device GPS cannot replace the explicitly selected named place.
    result = client.post(endpoint, headers=headers, json=body)
    assert result.status_code == 200, result.text
    report = result.json()["action"]["report"]
    assert report["latitude"] == 47.175 and report["longitude"] == 27.574
    assert report["category"] == "smoke"
    assert "Bulevardul Carol I" in result.json()["content"]
    assert client.post(endpoint, headers=headers, json=body).json() == result.json()
    assert counts(client) == (1, 1) and len(searches) == 1


def test_named_location_followup_and_no_result_do_not_substitute_gps(client, account, monkeypatch):
    from app.main import app

    searches = []

    async def search(query):
        searches.append(query)
        return (
            [{"label": "Palas, Iași", "latitude": 47.155, "longitude": 27.588}]
            if query.lower() == "palas"
            else []
        )

    monkeypatch.setattr(app.state.geocoder, "search", search)
    headers = account()
    endpoint = setup(client, headers)
    unknown = client.post(
        endpoint, headers=headers, json=command(content="este fum la pakas", current_location=location())
    )
    assert unknown.json()["action"]["status"] == "needs_details" and counts(client) == (0, 0)
    result = client.post(endpoint, headers=headers, json=command(content="Palas"))
    assert result.json()["action"]["status"] == "created"
    assert result.json()["action"]["report"]["longitude"] == 27.588
    assert searches == ["pakas", "Palas"]
    assert counts(client) == (1, 1)


def test_named_place_question_does_not_publish_or_search(client, account, monkeypatch):
    from app.main import app

    async def forbidden(*args):
        raise AssertionError("A question must not be geocoded as a report")

    monkeypatch.setattr(app.state.geocoder, "search", forbidden)
    headers = account()
    endpoint = setup(client, headers)
    for content in ["Este fum la Copou?", "Nu este fum la Copou", "Dacă este fum la Copou, raportează-l"]:
        result = client.post(endpoint, headers=headers, json=command(content=content))
        assert result.status_code == 200 and result.json().get("action", {}).get("status") != "created"
    assert counts(client) == (0, 0)


def test_tool_failure_rolls_back_report_and_idempotency_key(client, account, monkeypatch):
    from app.services import reports

    headers = account()
    endpoint = setup(client, headers)

    async def fail(*args):
        raise RuntimeError("Synthetic failure after insertion, before commit")

    monkeypatch.setattr(reports, "report_output", fail)
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        client.post(endpoint, headers=headers, json=command(current_location=location()))
    assert counts(client) == (0, 0)


async def test_concurrent_retries_create_one_report(tmp_path):
    # Independent sessions against one file exercise the database uniqueness constraint.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            user = User(email="fixture@example.com", display_name="Fixture", password_hash="fixture")
            db.add(user)
            await db.commit()
            owner_id = user.id
        payload = MessageCreate.model_validate(command(current_location=location()))
        settings = Settings(_env_file=None)

        async def deliver():
            async with sessions() as db:
                user = await db.get(User, owner_id)
                result = await create_pollution_report(db, user, payload, settings, "fixture-conversation")
                await db.commit()
                return result

        first, second = await asyncio.gather(deliver(), deliver())
        assert first == second
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Report)) == 1
            assert await db.scalar(select(func.count()).select_from(AssistantAction)) == 1
    finally:
        await engine.dispose()
