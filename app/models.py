"""Imports every table so `Base.metadata` is complete (used by Alembic and tests)."""

from app.agent.models import LlmUsage
from app.db import Base
from app.devices.models import Device
from app.reports.models import Report, ReportDraft, ReportFlag, ReportVote

__all__ = ["Base", "Device", "LlmUsage", "Report", "ReportDraft", "ReportFlag", "ReportVote"]
