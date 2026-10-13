from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from kf_oj.core.settings import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise ValueError("KF_OJ_DATABASE_URL is required for database-backed operations")
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
        pool_timeout=5,
        connect_args={
            "timeout": 5,
            "command_timeout": 10,
            "server_settings": {
                "statement_timeout": "10000",
                "lock_timeout": "5000",
                "idle_in_transaction_session_timeout": "30000",
            },
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
