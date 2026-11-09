from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class PlaceCache(Base):
    """Answers from Photon / Nominatim, kept 30 days to respect their usage policies."""

    __tablename__ = "places_cache"

    query: Mapped[str] = mapped_column(String(200), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    results: Mapped[list | dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
