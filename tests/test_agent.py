"""The agent chat: SSE events, the tool loop with a scripted LLM, rules fallback, quota and replay."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agent import rules
from app.agent.llm import Completion, LlmUnavailable, ToolCall
from app.agent.models import LlmUsage
from app.agent.schemas import AgentEvent
from app.agent.service import place_in_text
from app.agent.tools import scrub
from app.air.ingest import store_forecasts
from app.air.service import refresh_now_grid
from app.reports.models import ReportDraft
from tests.test_air_api import lattice_forecasts

HERE = {"lat": 47.1585, "lon": 27.6014, "accuracy_m": 20}


class ScriptedLlm:
    """Plays back completions in order; each item is text, tool calls, or an exception."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[list[dict]] = []

    @property
    def available(self) -> bool:
        return True

    async def stream(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        text, calls = (step, []) if isinstance(step, str) else ("", step)
        for word in text.split(" ") if text else []:
            yield word + " "
        yield Completion(
            provider="fake", model="scripted", text=text, tool_calls=calls, tokens_in=100, tokens_out=20
        )


class NoLlm:
    available = False


@pytest.fixture
def use_llm(app):
    def install(client):
        app.state.llm = client
        return client

    yield install
    if hasattr(app.state, "llm"):
        del app.state.llm


@pytest.fixture
async def air_data(session, settings):
    await store_forecasts(session, lattice_forecasts(20.0), datetime.now(UTC))
    await session.commit()
    await refresh_now_grid(session, settings)


async def conversation(client, headers) -> str:
    response = await client.post("/agent/conversations", headers=headers)
    assert response.status_code == 201
    return response.json()["id"]


async def send(client, headers, conversation_id, text, *, message_id=None, context=None):
    body = {"client_message_id": str(message_id or uuid4()), "text": text, "context": context or {}}
    events = []
    async with client.stream(
        "POST", f"/agent/conversations/{conversation_id}/messages", json=body, headers=headers
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        name = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and name:
                data = json.loads(line.split(":", 1)[1].strip())
                # Every event must match the schema published in openapi.json.
                AgentEvent.model_validate({"event": name, "data": data})
                events.append((name, data))
                name = None
    return events


def kinds(events):
    return [name for name, _ in events]


def text_of(events):
    return "".join(data["text"] for name, data in events if name == "text.delta")


# --- rules engine (pure) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("e fum gros la Podu Roș", "report"),
        ("cum e aerul în Copou?", "air_question"),
        ("pot să ies la alergat acum", "air_question"),
        ("mulțumesc!", "other"),
    ],
)
def test_rules_intent(text, intent):
    assert rules.intent(text) == intent


def test_rules_extraction_and_place():
    assert rules.extract("Salut, vreau să raportez că e fum gros la Podu Roș") == (
        "smoke",
        2,
        "E fum gros la Podu Roș.",
    )
    assert rules.severity_of("miroase urât, nu se mai vede nimic") == 3
    assert place_in_text("e praf mult la podu ros lângă palas") == "Podu Roș"


def test_scrub_removes_personal_data():
    text = scrub("Sunați la 0722 123 456 sau ion@example.com, mașina IS 12 ABC blochează")
    assert "0722" not in text and "example.com" not in text and "IS 12 ABC" not in text


# --- chat over SSE ---------------------------------------------------------------------------------


async def test_rules_mode_prepares_a_draft_at_the_gps_position(client, alice, use_llm, session):
    use_llm(NoLlm())
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "e fum gros aici, nu se mai vede", context={"location": HERE})
    assert kinds(events)[0] == "message.start" and kinds(events)[-1] == "message.end"
    [card] = [data for name, data in events if name == "card"]
    assert card["type"] == "report_draft"
    assert (card["category"], card["severity"], card["location_source"], card["extraction"]) == (
        "smoke",
        3,
        "gps",
        "rules",
    )
    assert "Publică" in text_of(events)
    assert events[-1][1]["engine"] == "rules"
    draft = await session.get(ReportDraft, card["draft_id"])
    assert draft.extraction == "rules"

    published = await client.post(f"/reports/drafts/{card['draft_id']}/publish", json={}, headers=alice)
    assert published.status_code == 201
    history = (await client.get(f"/agent/conversations/{chat}", headers=alice)).json()
    assert history["messages"][1]["cards"][0]["status"] == "published"


async def test_rules_mode_without_location_asks_for_the_map(client, alice, use_llm):
    use_llm(NoLlm())
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "e o groapă mare pe trotuar")
    [card] = [data for name, data in events if name == "card"]
    assert card["needs_location"] is True and card["location"] is None


async def test_rules_mode_answers_air_questions(client, alice, use_llm, air_data):
    use_llm(NoLlm())
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "cum e aerul în Copou?")
    assert "Copou" in text_of(events) and "moderat" in text_of(events).lower()
    assert [data["type"] for name, data in events if name == "card"] == ["air"]


async def test_llm_tool_loop_streams_status_cards_and_text(client, alice, use_llm, air_data, session):
    llm = use_llm(
        ScriptedLlm(
            [ToolCall(id="c1", name="get_air", arguments='{"place": "Copou"}')],
            "În Copou aerul e moderat acum.",
        )
    )
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "cum e aerul în Copou?")
    assert kinds(events) == ["message.start", "status", "card", *["text.delta"] * 6, "message.end"]
    assert text_of(events).strip() == "În Copou aerul e moderat acum."
    assert events[-1][1]["engine"] == "llm" and events[-1][1]["quota"]["used"] == 1
    # The tool result went back to the model as data.
    tool_message = llm.calls[1][-1]
    assert tool_message["role"] == "tool" and json.loads(tool_message["content"])["place"] == "Copou"
    usage = await session.scalar(select(LlmUsage))
    assert (usage.messages, usage.requests, usage.tokens) == (1, 2, 240)


async def test_llm_draft_report_is_not_published(client, alice, use_llm):
    use_llm(
        ScriptedLlm(
            [
                ToolCall(
                    id="c1",
                    name="draft_report",
                    arguments=json.dumps(
                        {
                            "category": "dust",
                            "severity": 2,
                            "description": "Praf de la un șantier.",
                            "place": "aici",
                        }
                    ),
                )
            ],
            "Am pregătit raportul. Verifică și apasă Publică.",
        )
    )
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "e praf de la santier", context={"location": HERE})
    [card] = [data for name, data in events if name == "card"]
    assert card["status"] == "pending" and card["extraction"] == "llm"
    assert (await client.get("/reports")).json()["items"] == []


async def test_same_client_message_id_replays_without_new_usage(client, alice, use_llm, session):
    llm = use_llm(ScriptedLlm("Salut! Te pot ajuta cu aerul și traseele."))
    chat = await conversation(client, alice)
    message_id = uuid4()
    first = await send(client, alice, chat, "salut", message_id=message_id)
    again = await send(client, alice, chat, "salut", message_id=message_id)
    assert first[0][1]["message_id"] == again[0][1]["message_id"]
    assert text_of(again) == text_of(first).strip() or text_of(again).strip() == text_of(first).strip()
    assert len(llm.calls) == 1
    assert (await session.scalar(select(LlmUsage))).messages == 1


async def test_quota_exhausted_falls_back_to_rules(client, alice, use_llm, settings, app):
    llm = use_llm(ScriptedLlm())
    chat = await conversation(client, alice)
    me = (await client.get("/devices/me", headers=alice)).json()
    async with app.state.sessionmaker() as db:
        from app.agent.quota import local_day

        db.add(
            LlmUsage(
                day=local_day(settings), device_id=me["device_id"], messages=settings.agent_daily_messages
            )
        )
        await db.commit()
    events = await send(client, alice, chat, "e fum aici", context={"location": HERE})
    assert events[-1][1]["engine"] == "rules"
    assert text_of(events).startswith("Ai folosit cele 30 de mesaje") or "30" in text_of(events)
    assert llm.calls == []


async def test_provider_failure_falls_back_to_rules(client, alice, use_llm):
    use_llm(ScriptedLlm(LlmUnavailable("down")))
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "e fum aici", context={"location": HERE})
    assert events[-1][1]["engine"] == "rules"
    assert "nu e disponibil" in text_of(events)


async def test_conversations_are_private(client, alice, bob):
    chat = await conversation(client, alice)
    assert (await client.get(f"/agent/conversations/{chat}", headers=bob)).status_code == 404
    listed = (await client.get("/agent/conversations", headers=alice)).json()["items"]
    assert [c["id"] for c in listed] == [chat]


async def test_failure_after_a_tool_keeps_its_card_without_a_second_draft(client, alice, use_llm, session):
    use_llm(
        ScriptedLlm(
            [
                ToolCall(
                    id="c1",
                    name="draft_report",
                    arguments='{"category": "smoke", "severity": 3, "description": "Fum.", "place": "aici"}',
                )
            ],
            LlmUnavailable("rate limited"),
        )
    )
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "e fum gros aici", context={"location": HERE})
    assert [data["type"] for name, data in events if name == "card"] == ["report_draft"]
    assert "apasă Publică" in text_of(events)
    drafts = (await session.scalars(select(ReportDraft))).all()
    assert len(drafts) == 1


def test_groq_models_become_separate_providers():
    from app.agent.llm import providers
    from tests.conftest import make_settings

    chain = providers(
        make_settings(groq_api_key="k", groq_model="a, b", openrouter_api_key="", openrouter_model="")
    )
    assert [p.name for p in chain] == ["groq:a", "groq:b"]


def test_every_tool_has_an_executor():
    from app.agent import tools

    names = {tool["function"]["name"] for tool in tools.TOOLS}
    assert names == set(tools.EXECUTORS) == set(tools.ALLOWED_ARGS) == set(tools.STATUS)


async def test_empty_reply_is_retried_then_replaced(client, alice, use_llm):
    llm = use_llm(ScriptedLlm("", "Aerul e bun acum."))
    chat = await conversation(client, alice)
    events = await send(client, alice, chat, "salut")
    assert text_of(events).strip() == "Aerul e bun acum."
    assert llm.calls[1][-1]["role"] == "system"

    use_llm(ScriptedLlm("", ""))
    events = await send(client, alice, chat, "salut din nou")
    assert text_of(events) == "Nu am reușit să formulez un răspuns acum. Încearcă din nou, te rog."
