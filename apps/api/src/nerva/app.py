"""Application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nerva import __version__
from nerva.api.router import api_router
from nerva.core.config import Settings, get_settings
from nerva.core.errors import register_exception_handlers
from nerva.routes import health


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Nerva API",
        version=__version__,
        description="The operating system for AI-powered companies.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,  # the session cookie must be readable across origins
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)
    return app
