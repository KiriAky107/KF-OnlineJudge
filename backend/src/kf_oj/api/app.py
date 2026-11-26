import logging
from asyncio import Semaphore
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
from kf_oj.api.v1.agent_learning import router as agent_learning_router
from kf_oj.api.v1.contest_ratings import router as contest_rating_router
from kf_oj.api.v1.contest_schedule import router as contest_schedule_router
from kf_oj.api.v1.editor import router as editor_router
from kf_oj.api.v1.identity import router as identity_router
from kf_oj.api.v1.learning import router as learning_router
from kf_oj.api.v1.problems import router as problems_router
from kf_oj.api.v1.router import router
from kf_oj.api.v1.self_tests import router as self_test_router
from kf_oj.api.v1.skill_drafts import router as skill_draft_router
from kf_oj.api.v1.skill_publications import router as skill_publication_router
from kf_oj.api.v1.submissions import router as submission_router
from kf_oj.api.v1.worker_artifacts import router as worker_router
from kf_oj.core.logging import configure_logging
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.clangd_completion import ClangdCompletion
from kf_oj.infra.clangd_socket import SocketClangdCompletion
from kf_oj.infra.coordinator import CoordinatorControl
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.contests.schedule import ContestSchedules
from kf_oj.modules.contests.service import ContestRatings
from kf_oj.modules.identity.service import IdentityService
from kf_oj.modules.learning.agent_application import AgentLearning
from kf_oj.modules.learning.hint_policy import HintPolicy
from kf_oj.modules.learning.public import AGENT_TOOL_SCOPES
from kf_oj.modules.learning.service import LearningService
from kf_oj.modules.operations.queries import MaintenanceAuditQueries
from kf_oj.modules.operations.service import MaintenanceAudit
from kf_oj.modules.problems.imports import ProblemImports
from kf_oj.modules.problems.maintenance import ProblemMaintenance
from kf_oj.modules.problems.service import ProblemService
from kf_oj.modules.skills.public import SkillDrafts, SkillPublications, SkillReports
from kf_oj.modules.submissions.performance import SubmissionPerformanceQueries
from kf_oj.modules.submissions.queries import SubmissionQueries
from kf_oj.modules.submissions.self_tests import SelfTestService
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
        editor = None
        try:
            if settings.database_url is not None:
                engine = build_engine(settings)
                app.state.sessions = build_session_factory(engine)
                app.state.identity = IdentityService(
                    app.state.sessions, lifetime_seconds=settings.session_lifetime_seconds
                )
                app.state.submission_performance = SubmissionPerformanceQueries(app.state.sessions)
                app.state.contest_ratings = ContestRatings(app.state.sessions, app.state.identity)
                app.state.contest_schedules = ContestSchedules(
                    app.state.sessions, app.state.identity
                )
                if settings.editor_completion_socket is not None:
                    editor = SocketClangdCompletion(settings.editor_completion_socket)
                elif settings.editor_toolchain_usr is not None:
                    editor = ClangdCompletion(settings.editor_toolchain_usr)
                if editor is not None:
                    if await editor.available():
                        app.state.editor_completion = editor
                        app.state.editor_completion_slots = Semaphore(2)
                        app.state.editor_completion_busy = set()
                    else:
                        logger.warning(
                            "Isolated language analysis unavailable; keeping basic completion"
                        )
                app.state.learning = LearningService(app.state.sessions)
                source_reader = None
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
                    app.state.self_tests = SelfTestService(
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
                    source_reader = SubmissionQueries(app.state.sessions, artifacts, cancellation)
                    app.state.submission_queries = source_reader
                app.state.agent_learning = AgentLearning(
                    app.state.sessions,
                    app.state.identity,
                    source_reader=source_reader,
                    hint_policy=HintPolicy(
                        settings.learning_chat_max_hint_level,
                        settings.learning_member_max_hint_level,
                        settings.learning_admin_max_hint_level,
                    ),
                )
                app.state.skill_drafts = SkillDrafts(
                    app.state.sessions,
                    app.state.identity,
                    app.state.agent_learning.catalog,
                    AGENT_TOOL_SCOPES,
                )
                app.state.skill_publications = SkillPublications(
                    app.state.sessions, app.state.identity
                )
                app.state.skill_reports = SkillReports(app.state.sessions, app.state.identity)
            yield
        finally:
            if isinstance(editor, ClangdCompletion):
                await editor.close()
            app.state.skill_reports = None
            app.state.skill_publications = None
            app.state.skill_drafts = None
            app.state.agent_learning = None
            app.state.learning = None
            app.state.identity = None
            app.state.problems = None
            app.state.worker_access = None
            app.state.self_tests = None
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
            (
                "/api/v1/auth",
                "/api/v1/submissions",
                "/api/v1/self-tests",
                "/api/v1/internal",
                "/api/v1/admin",
                "/api/v1/learning",
                "/api/v1/trainings",
            )
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    install_handlers(app)

    app.include_router(router, prefix="/api/v1")
    app.include_router(contest_rating_router, prefix="/api/v1")
    app.include_router(contest_schedule_router, prefix="/api/v1")
    app.include_router(editor_router, prefix="/api/v1")
    app.include_router(identity_router, prefix="/api/v1")
    app.include_router(learning_router, prefix="/api/v1")
    app.include_router(agent_learning_router, prefix="/api/v1")
    app.include_router(skill_draft_router, prefix="/api/v1")
    app.include_router(skill_publication_router, prefix="/api/v1")
    app.include_router(problems_router, prefix="/api/v1")
    app.include_router(worker_router, prefix="/api/v1")
    app.include_router(submission_router, prefix="/api/v1")
    app.include_router(self_test_router, prefix="/api/v1")
    app.include_router(import_router, prefix="/api/v1")
    app.include_router(maintenance_router, prefix="/api/v1")
    return app
