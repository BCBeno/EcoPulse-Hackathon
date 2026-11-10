"""Typed shapes of the agent's cards and SSE events, published in openapi.json for the frontend."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, RootModel

from app.agent.quota import Quota
from app.air.router import AirForecast, AirPoint
from app.geo import LatLon
from app.places.service import Place
from app.reports.categories import CategoryId
from app.reports.schemas import ReportOut
from app.routing.service import RouteRequest, RoutesOut
from app.traffic.router import TrafficSegmentOut


class ReportDraftCard(BaseModel):
    type: Literal["report_draft"] = "report_draft"
    draft_id: UUID
    status: Literal["pending", "published", "cancelled"]
    category: CategoryId
    severity: int = Field(ge=1, le=3)
    description: str
    location: LatLon | None
    location_label: str | None
    location_source: Literal["place", "gps", "map", "none"]
    needs_location: bool
    extraction: Literal["llm", "rules"]


class RoutesCard(BaseModel):
    type: Literal["routes"] = "routes"
    request: RouteRequest
    result: RoutesOut


class AirCard(BaseModel):
    type: Literal["air"] = "air"
    point: AirPoint
    forecast: AirForecast | None


class ReportsCard(BaseModel):
    type: Literal["reports"] = "reports"
    center: LatLon
    items: list[ReportOut]


class PlacesCard(BaseModel):
    type: Literal["places"] = "places"
    query: str
    items: list[Place]


class TrafficCard(BaseModel):
    type: Literal["traffic"] = "traffic"
    center: LatLon | None
    segments: list[TrafficSegmentOut]


Card = Annotated[
    ReportDraftCard | RoutesCard | AirCard | ReportsCard | PlacesCard | TrafficCard,
    Field(discriminator="type"),
]


# --- SSE events: `event:` is the name, `data:` is the JSON body ------------------------------------


class MessageStartData(BaseModel):
    message_id: UUID


class TextData(BaseModel):
    text: str


class MessageEndData(BaseModel):
    message_id: UUID
    engine: Literal["llm", "rules"]
    quota: Quota


class ErrorData(BaseModel):
    code: Literal["busy", "internal_error"]
    message: str


class MessageStartEvent(BaseModel):
    event: Literal["message.start"]
    data: MessageStartData


class StatusEvent(BaseModel):
    event: Literal["status"]
    data: TextData


class TextDeltaEvent(BaseModel):
    event: Literal["text.delta"]
    data: TextData


class CardEvent(BaseModel):
    event: Literal["card"]
    data: Card


class MessageEndEvent(BaseModel):
    event: Literal["message.end"]
    data: MessageEndData


class ErrorEvent(BaseModel):
    event: Literal["error"]
    data: ErrorData


class AgentEvent(
    RootModel[
        Annotated[
            MessageStartEvent | StatusEvent | TextDeltaEvent | CardEvent | MessageEndEvent | ErrorEvent,
            Field(discriminator="event"),
        ]
    ]
):
    """One server-sent event of POST /agent/conversations/{id}/messages: `event` name + parsed `data`."""
