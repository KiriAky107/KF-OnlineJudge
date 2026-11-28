import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts import models as artifact_models  # noqa: F401
from kf_oj.infra.db.base import Base
from kf_oj.infra.db.session import build_engine
from kf_oj.infra.messaging import models as messaging_models  # noqa: F401
from kf_oj.modules.contests import models as contest_models  # noqa: F401
from kf_oj.modules.identity import models as identity_models  # noqa: F401
from kf_oj.modules.learning import (  # noqa: F401
    agent_models,
    agent_tool_models,
    diagnostic_models,
    mistake_models,
)
from kf_oj.modules.learning import models as training_models  # noqa: F401
from kf_oj.modules.operations import models as operations_models  # noqa: F401
from kf_oj.modules.problems import import_models, learning_models, maintenance_models  # noqa: F401
from kf_oj.modules.problems import models as problem_models  # noqa: F401
from kf_oj.modules.skills import (  # noqa: F401
    binding_models,
    draft_models,
    publication_models,
    report_models,
)
from kf_oj.modules.submissions import models as submission_models  # noqa: F401
from kf_oj.modules.teams import models as team_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Explicit composition root. Infra and domains do not import foreign ORM models.
target_metadata = Base.metadata


def run_offline() -> None:
    settings = Settings()
    if settings.database_url is None:
        raise ValueError("KF_OJ_DATABASE_URL is required for migrations")
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = build_engine(Settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
