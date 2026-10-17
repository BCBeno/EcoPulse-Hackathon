import httpx
import pytest

from app.services.quick_report import deterministic

ENDPOINT = "/api/v1/reports/from-text"


def body(**changes):
    return {"text": "vreau sa raportez o groapa foarte mare pe strada, e periculos", "latitude": 47.16, "longitude": 27.59, **changes}


@pytest.mark.parametrize(
    ("text", "category", "severity", "description"),
    [
        ("vreau să raportez o groapă mare pe trotuar", "infrastructure", 2, "O groapă mare pe trotuar."),
        ("Salut, raportez că  miroase a fum de plastic ars", "smoke", 2, "Miroase a fum de plastic ars."),
        ("un copac cazut blocheaza trotuarul, e periculos", "obstruction", 4, "Un copac cazut blocheaza trotuarul, e periculos."),
        ("strada e inundata dupa ploaie", "flooding", 2, "Strada e inundata dupa ploaie."),
        ("ceva ciudat", "other", 2, "Ceva ciudat."),
    ],
)
def test_deterministic_rewrite(text, category, severity, description):
    result = deterministic(text)
    assert (result.category, result.severity, result.description) == (category, severity, description)


def test_publishes_without_questions_and_requires_auth(client, account):
    assert client.post(ENDPOINT, json=body()).status_code == 401
    headers = account()
    response = client.post(ENDPOINT, headers=headers, json=body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["engine"] == "deterministic"
    assert data["report"]["category"] == "infrastructure" and data["report"]["severity"] == 4
    assert data["report"]["description"].startswith("O groapa foarte mare")
    assert client.get("/api/v1/reports").json()["items"][0]["id"] == data["report"]["id"]


def test_rejects_location_outside_city(client, account):
    response = client.post(ENDPOINT, headers=account(), json=body(latitude=44.43, longitude=26.1))
    assert response.status_code == 422


def test_llm_rewrite_used_and_falls_back_on_failure(client, account, monkeypatch):
    from app.core.config import get_settings
    from app.main import app

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    replies = iter(
        [
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": 'Sigur: {"category": "infrastructure", "severity": 3, "description": "Groapă adâncă pe carosabil, periculoasă pentru bicicliști."}'}}]},
            ),
            httpx.Response(500),
        ]
    )

    async def post(url, **kwargs):
        assert "openrouter.ai" in url and kwargs["json"]["messages"][1]["content"]
        response = next(replies)
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr(app.state.http.client, "post", post)
    headers = account()
    first = client.post(ENDPOINT, headers=headers, json=body()).json()
    assert first["engine"] == "llm"
    assert first["report"]["description"] == "Groapă adâncă pe carosabil, periculoasă pentru bicicliști."
    assert first["report"]["severity"] == 3
    second = client.post(ENDPOINT, headers=headers, json=body(text="praf de la santier")).json()
    assert second["engine"] == "deterministic" and second["report"]["category"] == "dust"
