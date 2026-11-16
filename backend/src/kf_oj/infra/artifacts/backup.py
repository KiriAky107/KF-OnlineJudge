"""Read-only private backup inventory, separate from garbage collection and grading."""

import argparse
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from kf_oj.contracts.judging import ArtifactReference
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.db.session import build_engine, build_session_factory

MAX_REFERENCES = 65536


def verify_existing(root: Path, reference: ArtifactReference) -> None:
    """No mkdir, lock creation or writable descriptor, even for a missing store."""
    root = root.absolute()
    if root.resolve(strict=True) != root:
        raise ValueError("backup storage path contains links")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("backup storage root identity differs")
        descriptor = os.open(
            reference.digest,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size != reference.bytes
            ):
                raise ValueError("backup artifact identity or length differs")
            digest = hashlib.sha256()
            remaining = reference.bytes
            while remaining:
                block = source.read(min(65536, remaining))
                if not block:
                    raise ValueError("backup artifact truncated")
                digest.update(block)
                remaining -= len(block)
            after = os.fstat(source.fileno())
            if (
                source.read(1)
                or digest.hexdigest() != reference.digest
                or any(
                    getattr(before, key) != getattr(after, key)
                    for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                )
            ):
                raise ValueError("backup artifact changed or digest differs")
    finally:
        os.close(directory)


async def inventory() -> dict:
    """Every ready registry object covers all enforced ready-state foreign keys.

    The operator separately proves a stopped-writing cut. This CLI neither
    claims that cut nor reads domain ORM models or changes a lifecycle state.
    """
    settings = Settings()
    if settings.artifact_root is None:
        raise ValueError("artifact root required")
    engine = build_engine(settings)
    try:
        sessions = build_session_factory(engine)
        async with sessions.begin() as session:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            rows = await session.execute(
                select(Artifact.digest, Artifact.bytes)
                .where(Artifact.state == "ready")
                .order_by(Artifact.digest)
                .limit(MAX_REFERENCES + 1)
            )
            objects = list(rows)
            if len(objects) > MAX_REFERENCES:
                raise ValueError("backup registry count exceeds bound")
            references = []
            for digest, size in objects:
                reference = ArtifactReference(digest=digest, bytes=size)
                # A 64-KiB buffer checks actual bytes without source/case output.
                await asyncio.to_thread(verify_existing, settings.artifact_root, reference)
                references.append(reference.model_dump(mode="json"))
        return {
            "schema_version": 1,
            "kind": "kf.private.artifact-backup-inventory",
            "read_only": True,
            "quiescence_verified": False,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "storage_identity": {
                "device": settings.artifact_root.stat().st_dev,
                "inode": settings.artifact_root.stat().st_ino,
                "uid": settings.artifact_root.stat().st_uid,
            },
            "objects": references,
        }
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Private read-only artifact backup inventory")
    parser.parse_args()
    try:
        result = asyncio.run(asyncio.wait_for(inventory(), timeout=300))
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    except (ValueError, OSError, SQLAlchemyError, TimeoutError):
        parser.exit(1, "Backup artifact inventory refused; no metadata or grade was modified.\n")


if __name__ == "__main__":
    main()
