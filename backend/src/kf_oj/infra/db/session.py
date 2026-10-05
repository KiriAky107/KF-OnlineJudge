from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from kf_oj.core.settings import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise ValueError("KF_OJ_DATABASE_URL is required for database-backed operations")
    return create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
