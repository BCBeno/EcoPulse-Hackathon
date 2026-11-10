"""Application settings, read from the environment and the repository's single `.env` file."""

from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, Literal, NamedTuple
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Bounds(NamedTuple):
    south: float
    west: float
    north: float
    east: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


def _comma_list(value):
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://ecopulse:ecopulse@localhost:5433/ecopulse"
    log_level: str = "INFO"
    log_json: bool = False

    # Comma-separated in the environment.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Proxies whose forwarding headers are trusted for the client IP (IPs or CIDRs).
    trusted_proxies: Annotated[list[str], NoDecode] = ["127.0.0.1/32", "::1/128"]
    # Internal endpoints are closed while this is empty.
    admin_api_key: str = ""
    user_agent: str = "EcoPulse/1.0 (+https://ecopulse.bcbeno.me)"
    http_timeout_s: float = Field(default=20, ge=1, le=120)

    city_name: str = "Iași"
    city_timezone: str = "Europe/Bucharest"
    # south,west,north,east
    city_bbox: Annotated[Bounds, NoDecode] = Bounds(47.05, 27.40, 47.26, 27.75)
    city_center_lat: float = 47.1585
    city_center_lon: float = 27.6014

    forecast_horizon_h: int = Field(default=72, ge=1, le=96)
    map_poll_interval_s: int = Field(default=60, ge=10)
    agent_daily_messages: int = Field(default=30, ge=1)
    # Stop calling LLMs for everyone after this many tokens per local day (stay under the free tiers).
    agent_daily_token_budget: int = Field(default=400_000, ge=1000)

    # LLM providers (OpenAI-compatible). Tried in order: Groq, then OpenRouter. Empty key = skipped;
    # with no provider at all the chat runs on rules only.
    groq_api_key: str = ""
    # Comma-separated: each Groq model has its own per-minute token limit, so several add capacity.
    groq_model: str = "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b"
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    llm_timeout_s: float = Field(default=30, ge=5, le=120)

    # Rate limits. Per-IP limits are generous: at a presentation every phone shares one Wi-Fi IP.
    # Per-device limits and the agent quota do the real protecting.
    devices_per_hour_per_ip: int = Field(default=300, ge=1)
    reads_per_minute_per_ip: int = Field(default=1500, ge=1)
    writes_per_minute_per_device: int = Field(default=20, ge=1)
    geocode_per_minute_per_ip: int = Field(default=300, ge=1)
    routes_per_minute_per_ip: int = Field(default=150, ge=1)

    graphhopper_url: str = "http://localhost:8989"

    # Data sources. OpenAQ is skipped (status "not_configured") while the key is empty.
    openaq_api_key: str = ""
    sync_enabled: bool = True
    sensor_community_every_min: int = Field(default=5, ge=5)
    openaq_every_min: int = Field(default=60, ge=30)
    cams_every_min: int = Field(default=180, ge=60)
    weather_every_min: int = Field(default=60, ge=30)
    osm_every_h: int = Field(default=24, ge=6)
    # HERE Traffic v7 (real-time flow). Empty key = no traffic layer.
    here_api_key: str = ""
    traffic_every_min: int = Field(default=10, ge=5)

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def split_lists(cls, value):
        return _comma_list(value)

    @field_validator("trusted_proxies")
    @classmethod
    def valid_networks(cls, value):
        for item in value:
            ip_network(item, strict=False)
        return value

    @field_validator("city_bbox", mode="before")
    @classmethod
    def parse_bbox(cls, value):
        if isinstance(value, str):
            value = [float(part) for part in _comma_list(value)]
        bounds = Bounds(*value)
        if not (-90 <= bounds.south < bounds.north <= 90 and -180 <= bounds.west < bounds.east <= 180):
            raise ValueError("CITY_BBOX must be south,west,north,east")
        return bounds

    @field_validator("city_timezone")
    @classmethod
    def valid_timezone(cls, value):
        ZoneInfo(value)
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.city_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
