"""Entry point for the independently credentialed learning worker."""

import argparse
import asyncio
import json
import signal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.infra.responses_model import ResponsesLearningHint
from kf_oj.modules.learning.agent_model_budget import AgentModelLimits
from kf_oj.modules.learning.agent_worker import LearningWorker
from kf_oj.modules.learning.hint_policy import HintPolicy


async def verify_role(engine: AsyncEngine) -> None:
    async with engine.connect() as db:
        allowed = await db.scalar(
            text("""
            SELECT pg_has_role(current_user, 'kf_oj_learning_worker', 'USAGE')
               AND NOT pg_has_role(current_user, 'kf_oj_platform', 'MEMBER')
               AND NOT pg_has_role(current_user, 'kf_oj_coordinator', 'MEMBER')
               AND NOT has_column_privilege('accounts', 'password_hash', 'SELECT')
               AND NOT has_column_privilege('sessions', 'token_hash', 'SELECT')
               AND NOT has_table_privilege('problem_cases', 'SELECT')
               AND NOT has_column_privilege('evaluations', 'state', 'UPDATE')
        """)
        )
    if not allowed:
        raise ValueError("learning worker requires its dedicated database role")


async def serve(settings: Settings, *, check: bool, once: bool) -> None:
    if settings.learning_database_url is None:
        raise ValueError("KF_OJ_LEARNING_DATABASE_URL is required")
    engine = build_engine(
        settings.model_copy(update={"database_url": settings.learning_database_url})
    )
    try:
        await verify_role(engine)
        model = None
        if settings.learning_model_base_url and settings.learning_model_name:
            model = ResponsesLearningHint(
                settings.learning_model_base_url,
                settings.learning_model_name,
                settings.learning_model_api_key,
                reasoning_effort=settings.learning_model_reasoning_effort,
                chat_reasoning_effort=settings.learning_chat_reasoning_effort,
                review_mcp=settings.learning_review_mcp_enabled,
                context_bytes=settings.learning_model_context_bytes,
                record_usage=settings.learning_model_record_usage,
            )
        worker = LearningWorker(
            build_session_factory(engine),
            model=model,
            model_limits=AgentModelLimits(
                account_per_hour=settings.learning_model_account_calls_per_hour,
                service_per_hour=settings.learning_model_service_calls_per_hour,
            ),
            review_batch_offers=(
                settings.learning_diagnostic_batch_offers
                if settings.learning_diagnostics_enabled
                else ()
            ),
            hint_policy=HintPolicy(
                settings.learning_chat_max_hint_level,
                settings.learning_member_max_hint_level,
                settings.learning_admin_max_hint_level,
            ),
        )
        if check:
            print(
                json.dumps(
                    {"service": "learning-worker", "status": "ready", "consuming_tasks": False}
                )
            )
            return
        if once:
            await worker.run_once()
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signo in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signo, stop.set)
        try:
            while not stop.is_set():
                if not await worker.run_once():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except TimeoutError:
                        continue
        finally:
            for signo in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signo)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process jobs from the learning queue")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--check", action="store_true", help="Verify the worker's database role")
    options.add_argument(
        "--once", action="store_true", help="Process at most one ready learning job"
    )
    args = parser.parse_args()
    try:
        asyncio.run(serve(Settings(), check=args.check, once=args.once))
    except ValueError as error:
        parser.exit(2, f"learning worker configuration failed ({type(error).__name__})\n")
    except SQLAlchemyError as error:
        parser.exit(2, f"learning worker database operation failed ({type(error).__name__})\n")


if __name__ == "__main__":
    main()
