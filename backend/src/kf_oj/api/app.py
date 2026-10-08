import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from kf_oj import __version__
from kf_oj.api.body_limits import BodyLimitMiddleware
from kf_oj.api.v1.identity import router as identity_router
from kf_oj.api.v1.router import router
from kf_oj.core.logging import configure_logging
from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.service import IdentityService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_logging(settings.log_level)
        logger = logging.getLogger("kf_oj.api")
        logger.info("Platform API started")
        engine = None
        try:
            if settings.database_url is not None:
                engine = build_engine(settings)
                app.state.sessions = build_session_factory(engine)
                app.state.identity = IdentityService(
                    app.state.sessions, lifetime_seconds=settings.session_lifetime_seconds
                )
            yield
        finally:
            app.state.identity = None
            if engine is not None:
                await engine.dispose()
            logger.info("Platform API stopped")

    app = FastAPI(
        title="KF-OnlineJudge Platform API",
        version=__version__,
        description="平台 API。身份使用真实可撤销会话，业务接口按模块逐项接入。",
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    app.include_router(router, prefix="/api/v1")
    app.include_router(identity_router, prefix="/api/v1")
    return app
