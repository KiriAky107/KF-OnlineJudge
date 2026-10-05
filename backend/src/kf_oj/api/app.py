import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from kf_oj import __version__
from kf_oj.api.v1.router import router
from kf_oj.core.logging import configure_logging
from kf_oj.core.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_logging(settings.log_level)
        logger = logging.getLogger("kf_oj.api")
        logger.info("Platform API started")
        try:
            yield
        finally:
            logger.info("Platform API stopped")

    app = FastAPI(
        title="KF-OnlineJudge Platform API",
        version=__version__,
        description="工程骨架。当前只提供进程存活检查，业务接口逐项实现。",
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router, prefix="/api/v1")
    return app
