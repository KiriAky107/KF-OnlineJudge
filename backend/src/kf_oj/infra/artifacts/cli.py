import argparse
import asyncio

from sqlalchemy.exc import SQLAlchemyError

from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.db.session import build_engine, build_session_factory


async def run(limit: int, after: str | None) -> None:
    settings = Settings()
    if settings.artifact_root is None:
        raise ValueError("artifact root required")
    store = ArtifactStore(
        settings.artifact_root,
        max_bytes=settings.artifact_max_bytes,
        max_files=settings.artifact_max_files,
    )
    engine = build_engine(settings)
    try:
        sessions = build_session_factory(engine)
        repository = ArtifactRepository(store)
        recovered = await repository.finish_deleting(sessions, limit=limit)
        marked, cursor = await repository.collect_metadata(sessions, limit=limit, after=after)
        removed = await repository.finish_deleting(sessions, limit=limit)
        orphans = await repository.collect_orphans(sessions, limit=limit)
        print(f"Recovered {recovered}; marked {marked}; removed {removed}; orphans {orphans}.")
        print(f"Next metadata scan cursor: {cursor or 'complete'}")
    finally:
        await engine.dispose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect bounded old unreferenced artifacts")
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 101))
    parser.add_argument("--after")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.limit, args.after))
    except (ValueError, OSError, SQLAlchemyError):
        parser.exit(1, "Artifact collection rejected; check storage and database configuration.\n")


if __name__ == "__main__":
    main()
