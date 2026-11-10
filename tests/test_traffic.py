"""HERE traffic: parsing, levels, storage, /traffic, routing penalties and the agent tool."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.traffic import service
from app.traffic.provider import parse

# Shaped like the HERE Traffic v7 flow example (locationReferencing=shape); speeds in m/s.
SAMPLE = {
    "sourceUpdated": "2026-11-10T08:00:00Z",
    "results": [
        {
            "location": {
                "description": "Bulevardul Ștefan cel Mare și Sfânt",
                "length": 600.0,
                "shape": {
                    "links": [
                        {
                            "points": [{"lat": 47.160, "lng": 27.585}, {"lat": 47.160, "lng": 27.590}],
                            "length": 380.0,
                            "functionalClass": 2,
                        },
                        {
                            "points": [{"lat": 47.160, "lng": 27.590}, {"lat": 47.160, "lng": 27.595}],
                            "length": 220.0,
                            "functionalClass": 2,
                        },
                    ]
                },
            },
            "currentFlow": {
                "speed": 2.5,
                "speedUncapped": 2.5,
                "freeFlow": 11.1,
                "jamFactor": 8.4,
                "confidence": 0.95,
                "traversability": "open",
            },
        },
        {
            "location": {
                "length": 300.0,
                "shape": {
                    "links": [{"points": [{"lat": 47.17, "lng": 27.57}, {"lat": 47.171, "lng": 27.571}]}]
                },
            },
            "currentFlow": {"speed": 13.0, "freeFlow": 13.9, "jamFactor": 0.4, "traversability": "open"},
        },
        {"location": {"shape": {"links": []}}, "currentFlow": {}},
    ],
}


def test_parse_flow_segments():
    updated, segments = parse(SAMPLE)
    assert updated == datetime(2026, 11, 10, 8, tzinfo=UTC)
    assert len(segments) == 2  # one line per road; its links are joined
    first = segments[0]
    assert first.coordinates == [[27.585, 47.16], [27.59, 47.16], [27.595, 47.16]]
    assert (first.functional_class, first.length_m) == (2, 600.0)
    assert (first.jam_factor, first.speed_kmh, first.free_flow_kmh, first.closed) == (8.4, 9.0, 40.0, False)
    assert segments[1].description is None


@pytest.mark.parametrize(
    ("jam", "closed", "key"),
    [
        (0.0, False, "free"),
        (3.0, False, "moderate"),
        (6.5, False, "heavy"),
        (8.4, False, "jammed"),
        (2, True, "closed"),
    ],
)
def test_levels(jam, closed, key):
    assert service.level(jam, closed).key == key


@pytest.fixture
async def stored(session):
    updated, segments = parse(SAMPLE)
    await service.store(session, updated, segments, datetime.now(UTC))
    await session.commit()


async def test_traffic_endpoint(client, stored):
    response = await client.get("/traffic")
    body = response.json()
    assert body["status"] == "ok"
    assert [s["level"] for s in body["segments"]] == ["jammed", "free"]
    assert body["segments"][0]["geometry"]["type"] == "LineString"
    again = await client.get("/traffic", headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304


async def test_traffic_not_configured(client):
    assert (await client.get("/traffic")).json()["status"] == "not_configured"


async def test_config_lists_traffic_levels(client):
    levels = (await client.get("/config")).json()["traffic_levels"]
    assert [level["key"] for level in levels] == ["free", "moderate", "heavy", "jammed", "closed"]


async def test_stale_traffic_is_ignored(session, stored):
    later = datetime.now(UTC) + timedelta(hours=1)
    assert await service.current(session, later) == []


def gh_path(lat: float, minutes: float) -> dict:
    coords = [[27.580 + 0.002 * i, lat] for i in range(11)]
    return {"distance": 1500, "time": minutes * 60_000, "points": {"coordinates": coords}, "details": {}}


def fake_graphhopper(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    model = body.get("custom_model")
    if model:
        assert any(f["id"].startswith("traffic_") for f in model["areas"]["features"])
        return httpx.Response(200, json={"paths": [gh_path(47.170, 24)]})
    return httpx.Response(200, json={"paths": [gh_path(47.160, 20)]})


async def test_routes_avoid_a_jammed_road(client, app, stored):
    original = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_graphhopper))
    try:
        response = await client.post(
            "/routes",
            json={"origin": {"lat": 47.16, "lon": 27.58}, "destination": {"lat": 47.16, "lon": 27.60}},
        )
    finally:
        await app.state.http.aclose()
        app.state.http = original
    body = response.json()
    assert [r["kinds"] for r in body["routes"]] == [["cleanest"], ["fastest"]]
    assert body["routes"][1]["warnings"] == [
        "Trece pe lângă trafic intens: Bulevardul Ștefan cel Mare și Sfânt."
    ]


async def test_agent_traffic_tool(client, alice, app, stored):
    from app.agent.llm import ToolCall
    from tests.test_agent import ScriptedLlm, conversation, send

    llm = ScriptedLlm(
        [ToolCall(id="t1", name="get_traffic", arguments="{}")], "E aglomerat pe Ștefan cel Mare."
    )
    app.state.llm = llm
    try:
        chat = await conversation(client, alice)
        events = await send(client, alice, chat, "e trafic în oraș?")
    finally:
        del app.state.llm
    cards = [data for name, data in events if name == "card"]
    assert cards, llm.calls[1][-1]["content"].encode("ascii", "backslashreplace").decode()
    [card] = cards
    assert card["type"] == "traffic" and card["center"] is None
    assert {s["level"] for s in card["segments"]} == {"jammed"}


# --- billing guard -----------------------------------------------------------------------------------


class CountingTransport(httpx.MockTransport):
    def __init__(self, status: int = 200):
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            assert "advancedFeatures" not in str(request.url)  # billed as Advanced Traffic
            return httpx.Response(status, json=SAMPLE if status == 200 else {"error": "x"})

        super().__init__(handler)


def ingestor(app, settings, transport, **overrides):
    from app.air.ingest import Ingestor

    return Ingestor(
        app.state.sessionmaker, httpx.AsyncClient(transport=transport), settings.model_copy(update=overrides)
    )


async def add_runs(app, count: int, status: str = "ok", at: datetime | None = None):
    from app.air.models import SourceRun

    async with app.state.sessionmaker() as db:
        for _ in range(count):
            db.add(SourceRun(source="here_traffic", status=status, started_at=at or datetime.now(UTC)))
        await db.commit()


async def test_monthly_cap_stops_requests(app, settings, monkeypatch):
    import app.air.ingest as ingest

    monkeypatch.setattr(ingest, "is_night", lambda hour: False)
    transport = CountingTransport()
    await add_runs(app, 3, at=datetime.now(UTC) - timedelta(minutes=1))
    run = await ingestor(app, settings, transport, here_api_key="k", here_monthly_cap=3).traffic()
    assert (run.status, run.error, transport.calls) == ("skipped", "monthly_cap_reached", 0)
    run = await ingestor(app, settings, transport, here_api_key="k", here_monthly_cap=4).traffic()
    assert (run.status, run.records, transport.calls) == ("ok", 2, 1)


async def test_failed_request_counts_and_is_not_retried(app, settings, monkeypatch):
    import app.air.ingest as ingest

    monkeypatch.setattr(ingest, "is_night", lambda hour: False)
    transport = CountingTransport(status=503)
    run = await ingestor(app, settings, transport, here_api_key="k").traffic()
    assert (run.status, transport.calls) == ("failed", 1)


async def test_night_polls_less_often(app, settings, monkeypatch):
    import app.air.ingest as ingest

    monkeypatch.setattr(ingest, "is_night", lambda hour: True)
    transport = CountingTransport()
    await add_runs(app, 1, at=datetime.now(UTC) - timedelta(minutes=10))
    assert await ingestor(app, settings, transport, here_api_key="k").traffic() is None
    assert transport.calls == 0


async def test_cleanup_keeps_billed_runs_for_the_month(app):
    from sqlalchemy import func, select

    from app.air.ingest import cleanup
    from app.air.models import SourceRun

    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    await add_runs(app, 2, at=ten_days_ago)
    async with app.state.sessionmaker() as db:
        db.add(SourceRun(source="cams", status="ok", started_at=ten_days_ago))
        await db.commit()
        await cleanup(db, datetime.now(UTC))
        await db.commit()
        sources = (await db.execute(select(SourceRun.source, func.count()).group_by(SourceRun.source))).all()
    assert dict(sources) == {"here_traffic": 2}
