async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_checks_database(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["db"] == "ok"


async def test_config_exposes_shared_constants(client):
    response = await client.get("/config")
    assert response.status_code == 200
    body = response.json()
    assert body["city"]["timezone"] == "Europe/Bucharest"
    assert [level["level"] for level in body["aqi_levels"]] == [1, 2, 3, 4, 5, 6]
    assert {c["id"] for c in body["report_categories"]} >= {"smoke", "dust", "construction", "other"}
    assert [s["value"] for s in body["severities"]] == [1, 2, 3]
    assert body["report_rules"]["gone_votes_to_resolve"] == 3
    assert [r["id"] for r in body["flag_reasons"]] == ["spam", "offensive", "personal_data", "wrong"]
    assert [m["id"] for m in body["modes"]] == ["walk", "bike"]


async def test_config_etag_returns_304(client):
    first = await client.get("/config")
    etag = first.headers["etag"]
    second = await client.get("/config", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag


async def test_unknown_route_uses_error_shape(client):
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Nu am găsit ce ai cerut."}}
