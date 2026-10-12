"""Ordinary import service composition. Provision its dedicated DB role and cgroup separately."""

import asyncio
import signal
import sys

from sqlalchemy.exc import DBAPIError

from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.identity.service import ReservedMaintenance
from kf_oj.modules.operations.service import MaintenanceAudit
from kf_oj.modules.problems.import_worker import ImportWorker, verify_worker_limits


async def run():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(event, stop.set)
    settings = Settings()
    if (
        settings.import_staging_root is None
        or settings.artifact_root is None
        or settings.judge_admission is None
        or settings.import_worker_cgroup is None
    ):
        raise ValueError("import service configuration required")
    verify_worker_limits(settings.import_worker_cgroup)
    engine = build_engine(settings)
    store = ArtifactStore(
        settings.artifact_root,
        max_bytes=settings.artifact_max_bytes,
        max_files=settings.artifact_max_files,
    )
    staging = ImportStaging(settings.import_staging_root)
    try:
        worker = ImportWorker(
            build_session_factory(engine),
            staging,
            ArtifactRepository(store),
            ReservedMaintenance(),
            MaintenanceAudit(),
            settings.judge_admission,
            settings.import_worker_cgroup,
        )
        while not stop.is_set():
            if not await worker.once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
    finally:
        staging.close()
        store.close()
        await engine.dispose()


def main():
    try:
        asyncio.run(run())
    except (ValueError, OSError, DBAPIError):
        print(
            "import service unavailable; check trusted configuration and resource limits",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
