import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import get_settings
from app.db.session import Base, engine
from app.integrations.http import ProviderError, ProviderHTTP
from app.integrations.routing import NominatimGeocodingProvider
from app.services.sources import SourceService

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app):
    if engine.dialect.name == "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    app.state.http = ProviderHTTP(settings)
    app.state.geocoder = NominatimGeocodingProvider(settings, app.state.http)
    app.state.sources = SourceService(settings, app.state.http)
    await app.state.sources.initialize()
    yield
    await app.state.http.close()
    await engine.dispose()


app = FastAPI(
    title="EcoPulse API",
    version="0.1.0",
    description="Environmental navigation for Iași. All coordinates are WGS84; GeoJSON uses longitude,latitude. Environmental scores are relative heuristics, not personal exposure measurements.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
# Traffic geometry is several hundred KB of JSON; compress large responses.
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.include_router(router)


@app.exception_handler(ProviderError)
async def provider_error(request: Request, exc: ProviderError):
    return JSONResponse(
        status_code=503, content={"detail": "External provider unavailable or unconfigured", "code": exc.code}
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logging.getLogger("ecopulse").error("Request failed: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "EcoPulse"}
