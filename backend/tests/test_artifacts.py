import asyncio
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kf_oj.contracts.judging import ArtifactReference
from kf_oj.infra.artifacts.models import Artifact
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore


def test_immutable_store_digest_capacity_and_recovery(tmp_path):
    root = tmp_path / "artifacts"
    store = ArtifactStore(root, max_bytes=6, max_files=2)
    try:
        first = store.put(b"abc")
        assert store.read(first) == b"abc"
        assert store.put(b"abc") == first
        with pytest.raises(ArtifactError, match="digest or length"):
            store.put(b"changed", first)
        second = store.put(b"def")
        with pytest.raises(ArtifactError, match="capacity"):
            store.put(b"g")
        assert store.read(first) == b"abc" and store.read(second) == b"def"
        (root / first.digest).write_bytes(b"xyz")
        with pytest.raises(ArtifactError, match="digest or length"):
            store.read(first)
        with pytest.raises(ArtifactError, match="digest or length"):
            store.put(b"abc")  # Corrupt files are not silently repaired or overwritten.
        assert (root / first.digest).read_bytes() == b"xyz"
    finally:
        store.close()


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "traversal"])
def test_links_and_non_regular_entries_refused(tmp_path, attack):
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    data = b"private bytes"
    ref = ArtifactReference(digest=hashlib.sha256(data).hexdigest(), bytes=len(data))
    private = tmp_path / "outside"
    private.write_bytes(data)
    private.chmod(0o600)
    try:
        if attack == "symlink":
            (root / ref.digest).symlink_to(private)
        elif attack == "hardlink":
            os.link(private, root / ref.digest)
        elif attack == "fifo":
            os.mkfifo(root / ref.digest, 0o600)
        else:
            ref = ref.model_copy(update={"digest": "../outside"})
        with pytest.raises(ArtifactError):
            store.read(ref)
    finally:
        store.close()


def test_private_root_and_bounded_temporary_cleanup(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    with pytest.raises(ArtifactError, match="private"):
        ArtifactStore(root)
    linked = tmp_path / "linked"
    linked.symlink_to(root)
    with pytest.raises(ArtifactError, match="links"):
        ArtifactStore(linked)
    root.chmod(0o700)
    store = ArtifactStore(root)
    try:
        old = root / (".tmp-" + uuid4().hex)
        recent = root / (".tmp-" + uuid4().hex)
        for path in (old, recent):
            path.write_bytes(b"temporary")
            path.chmod(0o600)
        os.utime(old, (0, 0))
        assert store.clean_temporary(limit=1) == 1
        assert not old.exists() and recent.exists()
        with pytest.raises(ArtifactError):
            store.clean_temporary(min_age_seconds=0)
    finally:
        store.close()


def test_physical_quota_serializes_parallel_publishers(tmp_path):
    root = tmp_path / "root"
    first = ArtifactStore(root, max_bytes=6, max_files=10)
    second = ArtifactStore(root, max_bytes=6, max_files=10)

    async def run():
        results = await asyncio.gather(
            asyncio.to_thread(first.put, b"abcd"),
            asyncio.to_thread(second.put, b"efgh"),
            return_exceptions=True,
        )
        assert sum(isinstance(r, ArtifactReference) for r in results) == 1
        assert sum(isinstance(r, ArtifactError) for r in results) == 1
        assert sum(p.stat().st_size for p in root.iterdir() if p.name != ".lock") == 4

    try:
        asyncio.run(run())
    finally:
        first.close()
        second.close()


def test_real_metadata_rollback_reference_fencing_and_collection(postgres_url, tmp_path):
    store = ArtifactStore(tmp_path / "bytes")
    artifacts = ArtifactRepository(store)

    async def run():
        engine = create_async_engine(postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        ref = store.reference(b"rolled back")
        try:
            with pytest.raises(RuntimeError):
                async with sessions.begin() as s:
                    await s.execute(text("SET LOCAL ROLE kf_oj_platform"))
                    await artifacts.put(s, b"rolled back")
                    raise RuntimeError("transaction aborted")
            async with sessions() as s:
                assert await s.get(Artifact, ref.digest) is None
            assert store.read(ref) == b"rolled back"  # Durable orphan, never dangling DB reference.
            os.utime(tmp_path / "bytes" / ref.digest, (0, 0))
            assert await artifacts.collect_orphans(sessions) == 1
            with pytest.raises(ArtifactError):
                store.read(ref)
            async with sessions.begin() as s:
                await s.execute(text("SET LOCAL ROLE kf_oj_platform"))
                ref = await artifacts.put(s, b"durable unreferenced")
            os.utime(tmp_path / "bytes" / ref.digest, (0, 0))
            assert await artifacts.collect_orphans(sessions) == 0
            async with sessions.begin() as s:
                await artifacts.mark_deleting(s, ref.digest)
            with pytest.raises(ArtifactError):
                async with sessions.begin() as s:
                    await artifacts.put(s, b"durable unreferenced")
            assert await artifacts.finish_deleting(sessions) == 1
            async with sessions() as s:
                assert list(await s.scalars(select(Artifact))) == []
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_bounded_metadata_collector_preserves_references_and_resumes(postgres_url, tmp_path):
    from datetime import timedelta

    from test_problems import import_fixture, platform_role

    from kf_oj.modules.problems.service import ProblemService

    root = tmp_path / "import"
    _, policy = import_fixture(root)
    store = ArtifactStore(tmp_path / "bytes")
    repository = ArtifactRepository(store)

    async def run():
        engine = create_async_engine(postgres_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = ProblemService(sessions, repository, policy)
            version = await service.import_directory(root)
            async with sessions.begin() as session:
                orphan = await repository.put(session, b"committed without business reference")
                await session.execute(
                    update(Artifact).values(created_at=func.now() - timedelta(days=2))
                )
            # After trusted fixture preparation, run collection with actual limited platform rights.
            await engine.dispose()
            platform_role(engine)
            marked_total, cursor = 0, None
            for _ in range(100):
                marked, cursor = await repository.collect_metadata(sessions, limit=1, after=cursor)
                marked_total += marked
                if cursor is None:
                    break
            else:
                pytest.fail("bounded metadata scan failed to finish")
            assert marked_total == 1
            assert await repository.finish_deleting(sessions, limit=1) == 1
            assert await repository.finish_deleting(sessions, limit=1) == 0
            async with sessions() as session:
                projected = await service.for_submission(session, version)
                assert (
                    await repository.read(session, projected.cases[0].expected)
                    == b"hidden-expected\x00\xff"
                )
                assert await session.get(Artifact, orphan.digest) is None
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        store.close()
