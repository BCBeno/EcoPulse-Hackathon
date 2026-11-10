"""One chat turn: quota, history, the LLM tool loop (or rules), persistence, and the SSE events."""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import prompts, rules, tools
from app.agent.llm import Completion, LlmClient, LlmUnavailable
from app.agent.models import Conversation, LlmUsage, Message
from app.agent.quota import Quota, device_quota, local_day
from app.air import eaqi
from app.config import Settings
from app.devices.models import Device
from app.geo import LatLon
from app.ids import uuid7
from app.places.service import GAZETTEER, normalize
from app.reports.models import ReportDraft

log = logging.getLogger("ecopulse.agent")

MAX_TOOL_ROUNDS = 3
EMPTY_REPLY_NUDGE = "Răspunde acum utilizatorului, pe scurt, în text simplu, folosind datele de mai sus."
EMPTY_REPLY_FALLBACK = "Nu am reușit să formulez un răspuns acum. Încearcă din nou, te rog."
HISTORY_MESSAGES = 6
TRACE_CHARS = 300
MAX_ACCURACY_M = 1000
ACTIVE_DEVICES: set[UUID] = set()  # one open stream per device (single API process)


class GpsLocation(LatLon):
    accuracy_m: float = Field(ge=0, le=100_000)


class MessageContext(BaseModel):
    location: GpsLocation | None = None
    map_center: LatLon | None = None
    selected_point: LatLon | None = None


class MessageIn(BaseModel):
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=1000)
    context: MessageContext = Field(default_factory=MessageContext)


@dataclass
class Turn:
    """What the assistant produced in one turn."""

    text: str = ""
    cards: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    engine: str = "rules"
    provider: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    requests: int = 0


def event(name: str, data: dict) -> dict:
    return {"event": name, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def global_tokens_today(session: AsyncSession, settings: Settings) -> int:
    total = await session.scalar(select(func.sum(LlmUsage.tokens)).where(LlmUsage.day == local_day(settings)))
    return int(total or 0)


async def record_usage(session: AsyncSession, settings: Settings, device: Device, turn: Turn) -> None:
    values = {
        "day": local_day(settings),
        "device_id": device.id,
        "messages": 1 if turn.engine == "llm" else 0,
        "requests": turn.requests,
        "tokens": turn.tokens_in + turn.tokens_out,
    }
    statement = insert(LlmUsage).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[LlmUsage.day, LlmUsage.device_id],
            set_={
                "messages": LlmUsage.messages + statement.excluded.messages,
                "requests": LlmUsage.requests + statement.excluded.requests,
                "tokens": LlmUsage.tokens + statement.excluded.tokens,
            },
        )
    )


def _history_entry(message: Message) -> dict:
    if message.role == "user":
        return {"role": "user", "content": message.text}
    content = message.text
    if message.tool_trace:
        facts = json.dumps(message.tool_trace, ensure_ascii=False, default=str)[:TRACE_CHARS]
        content = f"{content}\n[date folosite: {facts}]"
    return {"role": "assistant", "content": content}


async def history(session: AsyncSession, conversation_id: UUID, before: datetime) -> list[dict]:
    rows = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.created_at < before)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_MESSAGES)
    )
    return [_history_entry(m) for m in reversed(list(rows)) if m.text or m.tool_trace]


def place_in_text(text: str) -> str | None:
    """The longest curated Iași place name mentioned in the text, for the rules engine."""
    words = f" {normalize(text)} "
    best = None
    for entry in GAZETTEER:
        for name in [entry["name"], *entry["aliases"]]:
            key = normalize(name)
            if f" {key} " in words and (best is None or len(key) > len(normalize(best))):
                best = entry["name"]
    return best


async def rules_turn(ctx: tools.AgentContext, reason: str | None) -> Turn:
    turn = Turn(engine="rules")
    prefix = f"{reason} " if reason else ""
    kind = rules.intent(ctx.user_text)
    place = place_in_text(ctx.user_text)
    if kind == "report":
        category, severity, description = rules.extract(ctx.user_text)
        result = await tools.draft_report(ctx, category, severity, description, place)
        for card in result.cards:
            card["extraction"] = "rules"
        if result.cards:
            draft_id = UUID(result.cards[0]["draft_id"])
            draft = await ctx.session.get(ReportDraft, draft_id)
            draft.extraction = "rules"
        turn.cards = result.cards
        where = f" la {result.summary['place']}" if result.summary.get("place") else ""
        if "error" in result.summary:
            turn.text = prefix + result.summary["error"]
        elif result.summary.get("needs_location"):
            label = rules.category_label(category).lower()
            turn.text = f"{prefix}Am pregătit raportul ({label}). Alege locul pe hartă, apoi apasă Publică."
        else:
            label = rules.category_label(category).lower()
            turn.text = f"{prefix}Am pregătit raportul: {label}{where}. Verifică ciorna și apasă Publică."
    elif kind == "air_question":
        result = await tools.get_air(ctx, place)
        turn.cards = result.cards
        summary = result.summary
        if "error" in summary:
            turn.text = prefix + summary["error"]
        elif summary["eaqi"] is None:
            turn.text = f"{prefix}Nu am date despre aer pentru {summary['place']} acum."
        else:
            level = next(lvl for lvl in eaqi.LEVELS if lvl.level == summary["eaqi"])
            pm25 = summary["ug_m3"].get("pm25")
            details = f" (PM2.5 {pm25} µg/m³)" if pm25 is not None else ""
            notes = (" " + " ".join(summary["notes"])) if summary["notes"] else ""
            turn.text = (
                f"{prefix}Acum, {summary['place']}: aer {level.label.lower()}{details}. {level.advice}{notes}"
            )
    else:
        turn.text = (
            f"{prefix}Fără asistentul complet pot doar să pregătesc rapoarte și să spun cum e aerul. "
            "Pentru trasee folosește planificatorul de pe hartă."
        )
    turn.trace = [{"rules": kind}]
    return turn


async def llm_turn(
    ctx: tools.AgentContext, client: LlmClient, messages: list[dict], turn: Turn
) -> AsyncIterator[dict]:
    for round_index in range(MAX_TOOL_ROUNDS + 1):
        allow_tools = round_index < MAX_TOOL_ROUNDS
        completion: Completion | None = None
        async for item in client.stream(messages, tools.TOOLS if allow_tools else None):
            if isinstance(item, str):
                turn.text += item
                yield event("text.delta", {"text": item})
            else:
                completion = item
        assert completion is not None
        turn.engine, turn.provider, turn.model = "llm", completion.provider, completion.model
        turn.tokens_in += completion.tokens_in
        turn.tokens_out += completion.tokens_out
        turn.requests += 1
        if not completion.tool_calls:
            if not turn.text.strip() and round_index < MAX_TOOL_ROUNDS:
                # An empty answer (e.g. the budget went to reasoning): ask once more, text only.
                messages.append({"role": "system", "content": EMPTY_REPLY_NUDGE})
                async for item in client.stream(messages, None):
                    if isinstance(item, str):
                        turn.text += item
                        yield event("text.delta", {"text": item})
                    else:
                        turn.tokens_in += item.tokens_in
                        turn.tokens_out += item.tokens_out
                        turn.requests += 1
            if not turn.text.strip():
                turn.text = EMPTY_REPLY_FALLBACK
                yield event("text.delta", {"text": EMPTY_REPLY_FALLBACK})
            return
        messages.append(
            {
                "role": "assistant",
                "content": completion.text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in completion.tool_calls
                ],
            }
        )
        for call in completion.tool_calls:
            yield event("status", {"text": tools.STATUS.get(call.name, "Lucrez…")})
            result = await tools.execute(ctx, call.name, call.parsed())
            for card in result.cards:
                turn.cards.append(card)
                yield event("card", card)
            turn.trace.append({"tool": call.name, "args": call.parsed(), "result": result.summary})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result.summary, ensure_ascii=False, default=str),
                }
            )


def interrupted_text(turn: Turn) -> str:
    separator = "\n\n" if turn.text else ""
    if any(card.get("type") == "report_draft" for card in turn.cards):
        return separator + "Am pregătit raportul: verifică ciorna și apasă Publică."
    if turn.cards:
        return separator + "Am adunat datele de mai sus, dar asistentul nu poate termina răspunsul acum."
    return separator + "(Răspunsul a fost întrerupt. Încearcă din nou.)"


async def replay(message: Message, quota: Quota) -> AsyncIterator[dict]:
    yield event("message.start", {"message_id": str(message.id)})
    if message.text:
        yield event("text.delta", {"text": message.text})
    for card in message.cards:
        yield event("card", card)
    yield event(
        "message.end",
        {"message_id": str(message.id), "engine": message.engine, "quota": quota.model_dump(mode="json")},
    )


async def respond(
    session: AsyncSession,
    ctx: tools.AgentContext,
    client: LlmClient,
    payload: MessageIn,
) -> AsyncIterator[dict]:
    settings, device = ctx.settings, ctx.device
    existing = await session.scalars(
        select(Message).where(
            Message.conversation_id == ctx.conversation_id,
            Message.client_message_id == payload.client_message_id,
        )
    )
    by_role = {m.role: m for m in existing}
    if "assistant" in by_role:
        async for item in replay(by_role["assistant"], await device_quota(session, settings, device.id)):
            yield item
        return
    device_id = device.id  # read once: the ORM object may be expired if the turn fails
    if device_id in ACTIVE_DEVICES:
        yield event("error", {"code": "busy", "message": "Aștept încă răspunsul la mesajul anterior."})
        return
    ACTIVE_DEVICES.add(device_id)
    try:
        async for item in _respond(session, ctx, client, payload, by_role.get("user")):
            yield item
    finally:
        ACTIVE_DEVICES.discard(device_id)


async def _respond(
    session: AsyncSession,
    ctx: tools.AgentContext,
    client: LlmClient,
    payload: MessageIn,
    user_message: Message | None,
) -> AsyncIterator[dict]:
    settings, device = ctx.settings, ctx.device
    now = datetime.now(UTC)
    if user_message is None:
        user_message = Message(
            conversation_id=ctx.conversation_id,
            client_message_id=payload.client_message_id,
            role="user",
            text=payload.text,
            created_at=now,
        )
        session.add(user_message)
        conversation = await session.get(Conversation, ctx.conversation_id)
        conversation.title = conversation.title or payload.text[:60]
        conversation.updated_at = now
        await session.commit()
    assistant_id = uuid7()
    yield event("message.start", {"message_id": str(assistant_id)})

    quota = await device_quota(session, settings, device.id)
    reason = None
    if not client.available:
        reason = None
    elif quota.used >= quota.limit:
        reason = f"Ai folosit cele {quota.limit} mesaje cu asistentul pentru azi."
    elif await global_tokens_today(session, settings) >= settings.agent_daily_token_budget:
        reason = "Asistentul a atins limita de azi."
    use_llm = client.available and reason is None

    turn = Turn()
    if use_llm:
        local_now = now.astimezone(settings.tz)
        system = (
            prompts.SYSTEM
            + "\n\n"
            + prompts.context_block(local_now, ctx.location, ctx.map_center, ctx.selected_point)
        )
        messages = [
            {"role": "system", "content": system},
            *await history(session, ctx.conversation_id, user_message.created_at),
            {"role": "user", "content": payload.text},
        ]
        try:
            async for item in llm_turn(ctx, client, messages, turn):
                yield item
        except LlmUnavailable as error:
            log.warning("Agent falling back to rules: %s", error)
            if turn.text or turn.cards:
                # Tools already ran (e.g. a draft exists): keep their cards, do not redo them with rules.
                ending = interrupted_text(turn)
                turn.text += ending
                turn.engine = "llm"
                yield event("text.delta", {"text": ending})
            else:
                use_llm = False
                reason = "Asistentul complet nu e disponibil acum."
    if not use_llm:
        turn = await rules_turn(ctx, reason)
        yield event("text.delta", {"text": turn.text})
        for card in turn.cards:
            yield event("card", card)

    session.add(
        Message(
            id=assistant_id,
            conversation_id=ctx.conversation_id,
            client_message_id=payload.client_message_id,
            role="assistant",
            text=turn.text,
            cards=turn.cards,
            tool_trace=turn.trace,
            engine=turn.engine,
            provider=turn.provider,
            model=turn.model,
            tokens_in=turn.tokens_in,
            tokens_out=turn.tokens_out,
            created_at=datetime.now(UTC),
        )
    )
    await record_usage(session, settings, device, turn)
    await session.commit()
    quota = await device_quota(session, settings, device.id)
    yield event(
        "message.end",
        {"message_id": str(assistant_id), "engine": turn.engine, "quota": quota.model_dump(mode="json")},
    )


async def refresh_draft_cards(session: AsyncSession, messages: list[Message]) -> list[dict[str, Any]]:
    """Conversation history with the current status of each draft card."""
    ids = [UUID(card["draft_id"]) for m in messages for card in m.cards if card.get("type") == "report_draft"]
    statuses = {}
    if ids:
        rows = await session.execute(
            select(ReportDraft.id, ReportDraft.status).where(ReportDraft.id.in_(ids))
        )
        statuses = {str(draft_id): status for draft_id, status in rows}
    output = []
    for m in messages:
        cards = [
            card | {"status": statuses.get(card["draft_id"], card.get("status"))}
            if card.get("type") == "report_draft"
            else card
            for card in m.cards
        ]
        output.append(
            {"id": m.id, "role": m.role, "text": m.text, "cards": cards, "created_at": m.created_at}
        )
    return output
