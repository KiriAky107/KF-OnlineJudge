"""Read-only operator entry for checking locally reviewed diagnostic batches."""

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic_batch import MAX_BATCH_MANIFEST_BYTES
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.learning.diagnostic_batch import (
    DiagnosticBatch,
    draft_counted_list_reduction,
    resolve_diagnostic_batch,
)
from kf_oj.modules.problems.local_files import LocalImportFiles
from kf_oj.modules.problems.public import ProblemPort
from kf_oj.modules.problems.service import ProblemService


async def preflight_registered_batch(
    session: AsyncSession,
    problems: ProblemPort,
    artifacts: ArtifactRepository,
    admission: v2.NodeAdmission,
    registry: Path,
    manifest_sha256: str,
) -> DiagnosticBatch:
    """Recheck the exact registered package within the queue's transaction."""
    files = LocalImportFiles(registered_batch_directory(registry, manifest_sha256))
    try:
        return await _preflight(session, problems, artifacts, admission, files, manifest_sha256)
    finally:
        files.close()


async def _preflight(
    session: AsyncSession,
    problems: ProblemPort,
    artifacts: ArtifactRepository,
    admission: v2.NodeAdmission,
    files: LocalImportFiles,
    digest: str,
) -> DiagnosticBatch:
    return await resolve_diagnostic_batch(
        session,
        problems,
        artifacts,
        admission,
        files.read("diagnostic-batch.json", MAX_BATCH_MANIFEST_BYTES),
        digest,
        files.read,
    )


async def load_operator_batch(
    files: LocalImportFiles,
    manifest_sha256: str,
    settings: Settings,
    admission: v2.NodeAdmission | None,
) -> DiagnosticBatch:
    if settings.artifact_root is None or admission is None:
        raise ValueError("artifact root and judge admission required")
    store = ArtifactStore(
        settings.artifact_root,
        max_bytes=settings.artifact_max_bytes,
        max_files=settings.artifact_max_files,
    )
    try:
        engine = build_engine(settings)
        try:
            sessions = build_session_factory(engine)
            artifacts = ArtifactRepository(store)
            problems = ProblemService(sessions, artifacts, admission)
            async with sessions.begin() as session:
                return await _preflight(
                    session, problems, artifacts, admission, files, manifest_sha256
                )
        finally:
            await engine.dispose()
    finally:
        store.close()


def registered_batch_directory(registry: Path, manifest_sha256: str) -> Path:
    """Translate a registered digest to the only allowed package directory."""
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ValueError("invalid diagnostic batch digest")
    return registry / manifest_sha256


async def load_registered_batch(
    registry: Path,
    manifest_sha256: str,
    settings: Settings,
    admission: v2.NodeAdmission | None,
) -> DiagnosticBatch:
    files = LocalImportFiles(registered_batch_directory(registry, manifest_sha256))
    try:
        return await load_operator_batch(files, manifest_sha256, settings, admission)
    finally:
        files.close()


async def run(
    directory: Path,
    manifest_sha256: str,
    *,
    registry: bool = False,
    propose_reduction: bool = False,
) -> dict:
    if propose_reduction and not registry:
        raise ValueError("reduction proposals require the registered package")
    settings = Settings()
    if registry:
        batch = await load_registered_batch(
            directory, manifest_sha256, settings, settings.judge_admission
        )
    else:
        files = LocalImportFiles(directory)
        try:
            batch = await load_operator_batch(
                files, manifest_sha256, settings, settings.judge_admission
            )
        finally:
            files.close()
    return draft_counted_list_reduction(batch) if propose_reduction else batch.receipt()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a sealed diagnostic batch before replay")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument(
        "--registry", action="store_true", help="resolve the digest inside an operator registry"
    )
    parser.add_argument(
        "--propose-reduction",
        action="store_true",
        help="draft smaller inputs for a registered counted integer list",
    )
    args = parser.parse_args()
    try:
        receipt = asyncio.run(
            run(
                args.directory,
                args.manifest_sha256,
                registry=args.registry,
                propose_reduction=args.propose_reduction,
            )
        )
    except (ValueError, OSError, SQLAlchemyError):
        parser.exit(
            1, "Diagnostic batch evidence rejected. Check local pins, scope and admission.\n"
        )
    print(json.dumps(receipt, separators=(",", ":")))


if __name__ == "__main__":
    main()
