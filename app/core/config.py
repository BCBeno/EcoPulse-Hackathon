from functools import lru_cache
from pathlib import Path
from secrets import token_urlsafe

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ROOT / "backend/.env"), extra="ignore")
    app_env: str = "development"
    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'ecopulse.db'}"
    jwt_secret_key: str = Field(default_factory=lambda: token_urlsafe(48))
    jwt_access_token_expire_minutes: int = Field(default=30, ge=5, le=1440)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    city_name: str = "Iasi"
    city_timezone: str = "Europe/Bucharest"
    # South, west, north, east, as used by Overpass and Sensor.Community.
    city_bbox: str = "47.05,27.40,47.26,27.75"
    city_latitude: float = 47.1585
    city_longitude: float = 27.6014
    user_agent: str = "EcoPulse/0.1 (local environmental navigation research)"
    http_timeout_seconds: float = Field(default=25, ge=1, le=120)
    openaq_api_key: str = ""
    openaq_location_ids: str = ""
    source_sensor_aliases: dict[str, str] = Field(default_factory=dict)
    openaq_enabled: bool = True
    sensor_community_enabled: bool = True
    openmeteo_weather_enabled: bool = True
    openmeteo_air_quality_enabled: bool = True
    osm_features_enabled: bool = True
    official_works_enabled: bool = False
    official_works_feed_url: str = ""
    official_works_allowed_host: str = "politialocala-iasi.ro"
    valhalla_base_url: str = ""
    motorcycle_enabled: bool = False
    route_avoidance_radius_m: float = Field(default=150, ge=50, le=300)
    route_max_avoidance_zones: int = Field(default=8, ge=1, le=12)
    route_max_detour_attempts: int = Field(default=3, ge=1, le=5)
    route_exclusion_vertex_budget: int = Field(default=100, ge=60, le=1000)
    route_detour_timeout_seconds: float = Field(default=20, ge=2, le=30)
    route_max_detour_ratio: float = Field(default=2, ge=1.1, le=4)
    geocoding_base_url: str = "https://nominatim.openstreetmap.org"
    forecast_days: int = Field(default=3, ge=1, le=3)
    sensor_community_sync_interval_minutes: int = Field(default=10, ge=5)
    openaq_sync_interval_minutes: int = Field(default=60, ge=30)
    weather_sync_interval_minutes: int = Field(default=60, ge=30)
    cams_sync_interval_minutes: int = Field(default=180, ge=60)
    osm_sync_interval_hours: int = Field(default=24, ge=12)
    official_works_sync_interval_hours: int = Field(default=12, ge=6)
    source_sync_enabled: bool = True
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    @model_validator(mode="after")
    def validate_settings(self):
        from zoneinfo import ZoneInfo

        self.llm_provider = self.llm_provider.strip().lower().rstrip("/")
        if self.llm_provider in {
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/chat/completions",
        }:
            self.llm_provider = "openrouter"
        if self.llm_provider not in {"", "openai", "openrouter"}:
            raise ValueError("LLM_PROVIDER must be empty, openai, or openrouter")
        ZoneInfo(self.city_timezone)
        s, w, n, e = self.bounds
        if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
            raise ValueError("Invalid CITY_BBOX: expected south,west,north,east")
        if self.app_env == "production":
            if "jwt_secret_key" not in self.model_fields_set or len(self.jwt_secret_key) < 40:
                raise ValueError("Production requires a stable JWT_SECRET_KEY with at least 40 characters")
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production requires PostgreSQL/PostGIS")
        return self

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        parts = tuple(float(x) for x in self.city_bbox.split(","))
        if len(parts) != 4:
            raise ValueError("CITY_BBOX must have four values")
        return parts


@lru_cache
def get_settings() -> Settings:
    return Settings()
