import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.geo import LatLon
from app.reports.categories import DESCRIPTION_MAX, CategoryId

VoteKind = Literal["still_there", "gone"]
FlagReason = Literal["spam", "offensive", "personal_data", "wrong"]
PublicStatus = Literal["active", "expired", "resolved"]


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip()


class VoteCounts(BaseModel):
    still_there: int = 0
    gone: int = 0


class Viewer(BaseModel):
    is_author: bool
    my_vote: VoteKind | None
    can_vote: bool
    can_resolve: bool


class ReportOut(BaseModel):
    id: UUID
    category: CategoryId
    severity: int
    description: str
    location: LatLon
    location_label: str | None
    radius_m: int
    observed_at: datetime
    expires_at: datetime
    status: PublicStatus
    votes: VoteCounts
    created_at: datetime
    viewer: Viewer | None = Field(default=None, description="Only present when the request has a token.")


class ReportPage(BaseModel):
    items: list[ReportOut]
    next_cursor: str | None


class ReportCreate(BaseModel):
    category: CategoryId
    severity: int = Field(default=2, ge=1, le=3)
    # Optional: a report can be made in three taps; the category label is used as the description.
    description: str | None = Field(default=None, min_length=5, max_length=DESCRIPTION_MAX)
    location: LatLon
    location_label: str | None = Field(default=None, max_length=120)
    observed_at: AwareDatetime | None = None

    _clean = field_validator("description", "location_label")(clean_text)


class DraftPublish(BaseModel):
    """Optional corrections made by the user on the draft card."""

    location: LatLon | None = None
    category: CategoryId | None = None
    severity: int | None = Field(default=None, ge=1, le=3)
    description: str | None = Field(default=None, min_length=5, max_length=DESCRIPTION_MAX)

    _clean = field_validator("description")(clean_text)


class VoteIn(BaseModel):
    kind: VoteKind


class FlagIn(BaseModel):
    reason: FlagReason
