import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.session import Session
from app.integrations.http import ProviderError, ProviderHTTP
from app.integrations.llm import OpenAIResponsesProvider
from app.models.entities import DataSource, IngestionRun, Observation
from app.schemas.domain import ObservationData
from app.services.sources import SourceService


def test_sync_persists_and_is_idempotent(client):
    async def run():
        service = SourceService(Settings(_env_file=None))

        class FixtureProvider:
            async def fetch(self):
                return [
                    ObservationData(
                        provider="sensor_community",
                        source_record_id="synthetic-record",
                        sensor_id="synthetic-sensor",
                        parameter="pm25",
                        original_value=10,
                        value=10,
                        original_unit="µg/m³",
                        unit="µg/m³",
                        observed_at=datetime(2020, 1, 1, tzinfo=UTC),
                        latitude=47.15,
                        longitude=27.6,
                        category="LOW_COST_SENSOR_MEASUREMENT",
                    )
                ]

        service.providers["sensor_community"] = FixtureProvider()
        try:
            await service.sync("sensor_community")
            await service.sync("sensor_community")
            async with Session() as db:
                assert await db.scalar(select(func.count()).select_from(Observation)) == 1
                source = await db.get(DataSource, "sensor_community")
                assert source.last_success is not None and source.last_error is None
                assert await db.scalar(select(func.count()).select_from(IngestionRun)) == 2
        finally:
            await service.http.close()

    client.portal.call(run)


def test_missing_key_recorded_without_fake_success(client):
    async def run():
        settings = Settings(_env_file=None)
        settings.openaq_api_key = ""
        service = SourceService(settings)
        try:
            result = await service.sync("openaq")
            assert result["status"] == "failed"
            async with Session() as db:
                source = await db.get(DataSource, "openaq")
                assert source.configuration_missing and source.last_success is None
                assert source.last_error == "missing_configuration"
        finally:
            await service.http.close()

    client.portal.call(run)


def test_provider_failure_keeps_api_available(client):
    async def run():
        service = SourceService(Settings(_env_file=None))

        class Broken:
            async def fetch(self):
                raise ProviderError("synthetic_failure")

        service.providers["weather"] = Broken()
        try:
            assert (await service.sync("weather"))["status"] == "failed"
        finally:
            await service.http.close()

    client.portal.call(run)
    assert client.get("/health").status_code == 200


async def test_llm_tools_and_grounded_reference_validation():
    settings = Settings(_env_file=None, llm_api_key="synthetic-key", llm_model="synthetic-model")
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        assert body["store"] is False
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "get_sources",
                            "arguments": "{}",
                            "call_id": "synthetic-call",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"focus": "sources", "evidence_ids": ["synthetic-evidence"]}
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    async def source_tool():
        return {"evidence": [{"id": "synthetic-evidence"}]}

    http = ProviderHTTP(settings, httpx.MockTransport(handle))
    selection, result = await OpenAIResponsesProvider(settings, http).select(
        "What sources?", {"get_sources": ("Retrieve fixture data", source_tool)}
    )
    assert selection.evidence_ids == ["synthetic-evidence"] and "get_sources" in result
    assert calls[1]["input"][-1]["type"] == "function_call_output"
    await http.close()


async def test_llm_rejects_unknown_tools_and_missing_key():
    settings = Settings(_env_file=None, llm_api_key="synthetic-key", llm_model="synthetic-model")
    http = ProviderHTTP(
        settings,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "output": [
                        {"type": "function_call", "name": "execute_sql", "arguments": "{}", "call_id": "x"}
                    ]
                },
            )
        ),
    )
    with pytest.raises(ProviderError, match="unknown_tool"):
        await OpenAIResponsesProvider(settings, http).select("test", {})
    settings.llm_api_key = ""
    with pytest.raises(ProviderError, match="missing_configuration"):
        await OpenAIResponsesProvider(settings, http).select("test", {})
    await http.close()
