"""Local, controlled import of ordinary files; no archive extraction or program execution."""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.problems.service import ProblemService


async def run(root: Path, manifest: str) -> None:
    settings = Settings()
    if settings.artifact_root is None or settings.judge_admission is None:
        raise ValueError("artifact root and trusted judge admission required")
    store = ArtifactStore(
        settings.artifact_root,
        max_bytes=settings.artifact_max_bytes,
        max_files=settings.artifact_max_files,
    )
    engine = build_engine(settings)
    try:
        service = ProblemService(
            build_session_factory(engine), ArtifactRepository(store), settings.judge_admission
        )
        version_id = await service.import_directory(root, manifest)
        print(f"Problem version published: {version_id}")
    finally:
        await engine.dispose()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import immutable problem version")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest", default="problem.json")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.directory, args.manifest))
    except (ValueError, OSError, SQLAlchemyError):
        parser.exit(1, "Problem import rejected; check controlled files, budgets and database.\n")


if __name__ == "__main__":
    main()
