from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.devices.router import router as devices_router
from app.errors import install_error_handlers
from app.logs import configure_logging
from app.reports.router import router as reports_router
from app.security import RateLimiter
from app.system.router import router as system_router

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.limiter = RateLimiter()
        yield
        await engine.dispose()

    app = FastAPI(
        title="EcoPulse API",
        version="1.0.0",
        description=(
            "Calitatea aerului, trasee curate și rapoarte comunitare pentru Iași. "
            "Contractul complet: docs/api-contract.md."
        ),
        lifespan=lifespan,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    install_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "If-None-Match"],
            expose_headers=["ETag", "Retry-After"],
        )
    # The map grid is large JSON; compress anything non-trivial.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    api = APIRouter(prefix=API_PREFIX)
    for router in (system_router, devices_router, reports_router):
        api.include_router(router)
    app.include_router(api)
    return app


app = create_app()
