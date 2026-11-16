import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from kf_oj import __version__
from kf_oj.api.availability import AvailabilityMiddleware
from kf_oj.api.body_limits import BodyLimitMiddleware
from kf_oj.api.dependency_errors import install_handlers
from kf_oj.api.v1.admin_imports import router as import_router
from kf_oj.api.v1.admin_problems import router as maintenance_router
from kf_oj.api.v1.identity import router as identity_router
from kf_oj.api.v1.problems import router as problems_router
from kf_oj.api.v1.router import router
from kf_oj.api.v1.submissions import router as submission_router
from kf_oj.api.v1.worker_artifacts import router as worker_router
from kf_oj.core.logging import configure_logging
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.coordinator import CoordinatorControl
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.service import IdentityService
from kf_oj.modules.operations.queries import MaintenanceAuditQueries
from kf_oj.modules.operations.service import MaintenanceAudit
from kf_oj.modules.problems.imports import ProblemImports
from kf_oj.modules.problems.maintenance import ProblemMaintenance
from kf_oj.modules.problems.service import ProblemService
from kf_oj.modules.submissions.performance import SubmissionPerformanceQueries
from kf_oj.modules.submissions.queries import SubmissionQueries
from kf_oj.modules.submissions.service import SubmissionService
from kf_oj.modules.submissions.worker_access import WorkerAccess


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        configure_logging(settings.log_level, production=settings.environment == "production")
        logger = logging.getLogger("kf_oj.api")
        logger.info("Platform API started")
        engine = None
        store = None
        staging = None
        try:
            if settings.database_url is not None:
                engine = build_engine(settings)
                app.state.sessions = build_session_factory(engine)
                app.state.identity = IdentityService(
                    app.state.sessions, lifetime_seconds=settings.session_lifetime_seconds
                )
                app.state.submission_performance = SubmissionPerformanceQueries(app.state.sessions)
                artifacts = None
                if settings.artifact_root is not None:
                    store = ArtifactStore(
                        settings.artifact_root,
                        max_bytes=settings.artifact_max_bytes,
                        max_files=settings.artifact_max_files,
                    )
                    artifacts = ArtifactRepository(store)
                    if (
                        settings.import_staging_root is not None
                        and settings.judge_admission is not None
                    ):
                        staging = ImportStaging(settings.import_staging_root)
                        app.state.problem_imports = ProblemImports(
                            app.state.sessions,
                            staging,
                            app.state.identity,
                            MaintenanceAudit(),
                            settings.judge_admission,
                        )
                app.state.problem_maintenance = ProblemMaintenance(
                    app.state.sessions,
                    app.state.identity,
                    MaintenanceAudit(),
                    artifacts,
                    settings.judge_admission,
                )
                app.state.maintenance_audit = MaintenanceAuditQueries(
                    app.state.sessions, app.state.identity
                )
                app.state.problems = ProblemService(
                    app.state.sessions, artifacts, settings.judge_admission
                )
                if artifacts is not None:
                    app.state.submissions = SubmissionService(
                        app.state.sessions, app.state.problems, artifacts, settings.judge_admission
                    )
                    app.state.worker_access = WorkerAccess(
                        app.state.sessions, app.state.problems, artifacts
                    )
                    cancellation = (
                        CoordinatorControl(
                            settings.coordinator_url, settings.coordinator_token.get_secret_value()
                        )
                        if settings.coordinator_url and settings.coordinator_token
                        else None
                    )
                    app.state.submission_queries = SubmissionQueries(
                        app.state.sessions, artifacts, cancellation
                    )
            yield
        finally:
            app.state.identity = None
            app.state.problems = None
            app.state.worker_access = None
            app.state.submissions = None
            app.state.submission_queries = None
            app.state.submission_performance = None
            app.state.problem_imports = None
            app.state.problem_maintenance = None
            app.state.maintenance_audit = None
            if engine is not None:
                await engine.dispose()
            if store is not None:
                store.close()
            if staging is not None:
                staging.close()
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
    app.add_middleware(AvailabilityMiddleware, settings=settings)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(
            ("/api/v1/auth", "/api/v1/submissions", "/api/v1/internal", "/api/v1/admin")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    install_handlers(app)

    app.include_router(router, prefix="/api/v1")
    app.include_router(identity_router, prefix="/api/v1")
    app.include_router(problems_router, prefix="/api/v1")
    app.include_router(worker_router, prefix="/api/v1")
    app.include_router(submission_router, prefix="/api/v1")
    app.include_router(import_router, prefix="/api/v1")
    app.include_router(maintenance_router, prefix="/api/v1")
    return app
