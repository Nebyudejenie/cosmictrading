"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cosmic import __version__
from cosmic.core.clock import Clock, SystemClock
from cosmic.core.config import DEV_ENVIRONMENTS, Settings
from cosmic.core.redaction import SECRETS
from cosmic.db.engine import Database
from cosmic.trading import queries
from cosmic.trading.services import TradingServices
from cosmic.web.errors import install_error_handlers
from cosmic.web.filters import register_filters
from cosmic.web.routes import api, auth, health, market, portal, trading
from cosmic.web.security import security_headers_middleware
from cosmic.web.state import STATIC_DIR, TEMPLATES_DIR, AppState


def create_app(
    settings: Settings, *, db: Database | None = None, clock: Clock | None = None
) -> FastAPI:
    SECRETS.register(settings.secret_values())
    database = db or Database.from_settings(settings)
    owns_db = db is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_db:
                await database.dispose()

    dev = settings.env in DEV_ENVIRONMENTS
    app = FastAPI(
        title="COSMICTRADING QUANT",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if dev else None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True
    register_filters(templates.env)
    app.state.cosmic = AppState(
        settings=settings, db=database, clock=clock or SystemClock(), templates=templates
    )
    app.state.trading = TradingServices.build(database, app.state.cosmic.clock, settings)

    async def authority_banner(request: Request) -> dict[str, object]:
        async with database.connect() as conn:
            return await queries.authority_banner(conn)

    app.state.authority_banner = authority_banner
    app.middleware("http")(security_headers_middleware(hsts=settings.cookie_secure))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    install_error_handlers(app)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(portal.router)
    app.include_router(market.router)
    app.include_router(trading.router)
    app.include_router(api.router)
    return app
