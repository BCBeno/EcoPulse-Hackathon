from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette import EventSourceResponse

from app.agent import service
from app.agent.llm import LlmClient
from app.agent.models import Conversation, Message
from app.agent.schemas import Card
from app.agent.tools import AgentContext
from app.air.router import forecast_cache
from app.config import Settings, get_settings
from app.db import get_session
from app.devices.models import Device
from app.errors import ErrorResponse, not_found
from app.security import current_device, limit_device, read_limit

router = APIRouter(prefix="/agent", tags=["agent"], responses={404: {"model": ErrorResponse}})
Session = Annotated[AsyncSession, Depends(get_session)]
CurrentDevice = Annotated[Device, Depends(current_device)]
message_limit = limit_device("agent", "writes_per_minute_per_device", 60)


class ConversationCreated(BaseModel):
    id: UUID
    created_at: datetime


class ConversationItem(BaseModel):
    id: UUID
    title: str | None
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationItem]


class MessageOut(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    text: str
    cards: list[Card]
    created_at: datetime


class ConversationOut(BaseModel):
    id: UUID
    messages: list[MessageOut]


async def owned(session: AsyncSession, device: Device, conversation_id: UUID) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.device_id != device.id:
        raise not_found("Conversația nu există.")
    return conversation


def llm_client(request: Request, settings: Settings) -> LlmClient:
    if not hasattr(request.app.state, "llm"):
        request.app.state.llm = LlmClient(settings)
    return request.app.state.llm


@router.post(
    "/conversations",
    status_code=201,
    response_model=ConversationCreated,
    dependencies=[Depends(message_limit)],
)
async def create_conversation(session: Session, device: CurrentDevice) -> ConversationCreated:
    conversation = Conversation(device_id=device.id)
    session.add(conversation)
    await session.commit()
    return ConversationCreated(id=conversation.id, created_at=conversation.created_at)


@router.get("/conversations", response_model=ConversationList, dependencies=[Depends(read_limit)])
async def list_conversations(session: Session, device: CurrentDevice) -> ConversationList:
    rows = await session.scalars(
        select(Conversation)
        .where(Conversation.device_id == device.id)
        .order_by(Conversation.updated_at.desc())
        .limit(20)
    )
    return ConversationList(
        items=[ConversationItem(id=c.id, title=c.title, updated_at=c.updated_at) for c in rows]
    )


@router.get(
    "/conversations/{conversation_id}", response_model=ConversationOut, dependencies=[Depends(read_limit)]
)
async def get_conversation(conversation_id: UUID, session: Session, device: CurrentDevice) -> ConversationOut:
    await owned(session, device, conversation_id)
    messages = list(
        await session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
        )
    )
    return ConversationOut(
        id=conversation_id,
        messages=[MessageOut(**m) for m in await service.refresh_draft_cards(session, messages)],
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    dependencies=[Depends(message_limit)],
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events, see the contract"}
    },
)
async def send_message(
    conversation_id: UUID,
    payload: service.MessageIn,
    request: Request,
    session: Session,
    device: CurrentDevice,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EventSourceResponse:
    await owned(session, device, conversation_id)
    context = payload.context
    location = (
        context.location
        if context.location and context.location.accuracy_m <= service.MAX_ACCURACY_M
        else None
    )
    app = request.app
    client = llm_client(request, settings)
    cache = forecast_cache(request)

    async def events():
        # The stream outlives the request's dependencies, so it uses its own session.
        async with app.state.sessionmaker() as stream_session:
            ctx = AgentContext(
                session=stream_session,
                settings=settings,
                http=app.state.http,
                cache=cache,
                device=await stream_session.get(Device, device.id),
                conversation_id=conversation_id,
                user_text=payload.text,
                location=location,
                map_center=context.map_center,
                selected_point=context.selected_point,
            )
            try:
                async for item in service.respond(stream_session, ctx, client, payload):
                    yield item
            except Exception:
                service.log.exception("Agent turn failed")
                yield service.event(
                    "error", {"code": "internal_error", "message": "A apărut o eroare. Încearcă din nou."}
                )

    return EventSourceResponse(
        events(), ping=15, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    )
