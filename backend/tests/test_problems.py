import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kf_oj.api.app import create_app
from kf_oj.contracts.judging import AdmissionPolicy
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore
from kf_oj.modules.problems.models import Problem, ProblemVersion
from kf_oj.modules.problems.service import ProblemService

ROOT = Path(__file__).resolve().parents[2]


def import_fixture(root, *, slug="fixture-problem"):
    root.mkdir(mode=0o700)
    policy = AdmissionPolicy.model_validate_json(
        (ROOT / "tests/fixtures/contracts/judge-admission.valid.json").read_bytes()
    )
    manifest = {
        "schema_version": 1,
        "slug": slug,
        "title": "Synthetic problem",
        "statement_file": "statement.md",
        "languages": ["c17", "cpp20"],
        "toolchain": policy.toolchain.model_dump(),
        "comparison": "exact_bytes",
        "compile_limits": policy.compile_ceiling.model_dump(),
        "run_limits": policy.run_ceiling.model_dump(),
        "job_wall_time_us": 60000000,
        "cases": [
            {"key": "empty", "input_file": "input-1", "expected_file": "expected-1"},
            {"key": "binary", "input_file": "input-2", "expected_file": "expected-2"},
        ],
        "samples": [{"input_file": "sample-input", "output_file": "sample-output"}],
    }
    for filename, data in {
        "statement.md": b"Public statement only",
        "input-1": b"",
        "expected-1": b"hidden-expected\x00\xff",
        "input-2": b"hidden-input\x00\xfe",
        "expected-2": b"hidden-expected-2\xff",
        "sample-input": b"1\n",
        "sample-output": b"2\n",
    }.items():
        (root / filename).write_bytes(data)
    (root / "problem.json").write_text(json.dumps(manifest))
    return manifest, policy


def platform_role(engine):
    @event.listens_for(engine.sync_engine, "connect")
    def apply_role(connection, _):
        cursor = connection.cursor()
        cursor.execute("SET ROLE kf_oj_platform")
        cursor.close()


def test_controlled_import_cli_and_unpublished_version_visibility(postgres_url, tmp_path):
    root = tmp_path / "import"
    _, policy = import_fixture(root)
    env = os.environ | {
        "KF_OJ_DATABASE_URL": postgres_url,
        "KF_OJ_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "KF_OJ_JUDGE_ADMISSION": policy.model_dump_json(),
        "KF_OJ_ENVIRONMENT": "test",
    }
    result = subprocess.run(
        [sys.executable, "-m", "kf_oj.modules.problems.cli", str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0 and "Problem version published:" in result.stdout
    assert "hidden-expected" not in result.stdout + result.stderr

    async def run():
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = ProblemService(sessions)
            page = await service.list()
            assert page.items[0].revision == 1
            version_id = page.items[0].version_id
            draft_id = uuid4()
            async with sessions.begin() as session:
                published = await session.get(ProblemVersion, version_id)
                session.add(
                    ProblemVersion(
                        id=draft_id,
                        problem_id=published.problem_id,
                        revision=2,
                        title=published.title,
                        statement=published.statement,
                        languages=published.languages,
                        toolchain=published.toolchain,
                        compile_limits=published.compile_limits,
                        run_limits=published.run_limits,
                        job_wall_time_us=published.job_wall_time_us,
                        public_samples=[],
                    )
                )
            from kf_oj.modules.problems.public import ProblemUnavailable

            with pytest.raises(ProblemUnavailable):
                await service.detail(draft_id)
            assert (await service.list()).items[0].version_id == version_id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_actual_import_two_immutable_versions_and_public_api(postgres_url, tmp_path):
    root = tmp_path / "import"
    manifest, policy = import_fixture(root)

    async def run():
        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "artifacts",
                judge_admission=policy,
            )
        )
        async with app.router.lifespan_context(app):
            platform_role(app.state.sessions.kw["bind"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/api/v1/problems")).json() == {
                    "items": [],
                    "next_after": None,
                }
                first = await app.state.problems.import_directory(root)
                async with app.state.sessions() as session:
                    projected = await app.state.problems.for_submission(session, first)
                    assert len(projected.cases) == 2 and projected.cases[0].input.bytes == 0
                    binary = await app.state.problems.artifacts.read(
                        session, projected.cases[0].expected
                    )
                    assert binary == b"hidden-expected\x00\xff"
                manifest["title"] = "Second immutable version"
                (root / "statement.md").write_text("New public statement")
                (root / "problem.json").write_text(json.dumps(manifest))
                second = await app.state.problems.import_directory(root)
                old = await client.get(f"/api/v1/problems/versions/{first}")
                new = await client.get(f"/api/v1/problems/versions/{second}")
                assert old.json()["revision"] == 1 and new.json()["revision"] == 2
                assert old.json()["statement"] == "Public statement only"
                assert new.json()["statement"] == "New public statement"
                assert new.json()["samples"][0] == {
                    "encoding": "base64",
                    "input": base64.b64encode(b"1\n").decode(),
                    "output": base64.b64encode(b"2\n").decode(),
                }
                page = (await client.get("/api/v1/problems")).json()
                assert page["items"][0]["version_id"] == str(second)
                assert (await client.get("/api/v1/problems?limit=0")).status_code == 422
                assert (await client.get(f"/api/v1/problems/versions/{uuid4()}")).status_code == 404
                public_text = old.text + new.text + json.dumps(page)
                assert "hidden-expected" not in public_text and "hidden-input" not in public_text
                assert str(root) not in public_text and "digest" not in public_text
                async with app.state.sessions.begin() as session:
                    await session.execute(update(Problem).values(enabled=False))
                assert (await client.get(f"/api/v1/problems/versions/{first}")).status_code == 404
                assert (await client.get("/api/v1/problems")).json()["items"] == []

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "deadline", "node_budget"])
def test_import_invalid_package_refused_without_published_rows(postgres_url, tmp_path, invalid):
    root = tmp_path / "import"
    manifest, policy = import_fixture(root)
    if invalid == "missing":
        (root / "expected-1").unlink()
    elif invalid == "duplicate":
        manifest["cases"].append(manifest["cases"][0])
    elif invalid == "deadline":
        manifest["job_wall_time_us"] = 1000
    else:
        manifest["run_limits"]["cpu_time_us"] = policy.run_ceiling.cpu_time_us + 1
    (root / "problem.json").write_text(json.dumps(manifest))
    store = ArtifactStore(tmp_path / "artifacts")

    async def run():
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = ProblemService(sessions, ArtifactRepository(store), policy)
            with pytest.raises(ValueError):
                await service.import_directory(root)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ProblemVersion)) == 0
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_concurrent_import_revisions_pagination_and_storage_failure_rollback(
    postgres_url, tmp_path
):
    root = tmp_path / "import"
    _, policy = import_fixture(root)
    store = ArtifactStore(tmp_path / "artifacts")

    async def run():
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = ProblemService(sessions, ArtifactRepository(store), policy)
            versions = await asyncio.gather(
                service.import_directory(root), service.import_directory(root)
            )
            assert versions[0] != versions[1]
            assert sorted([(await service.detail(v)).revision for v in versions]) == [1, 2]
            other = tmp_path / "other"
            import_fixture(other, slug="another-problem")
            await service.import_directory(other)
            page = await service.list(limit=1)
            assert page.next_after == "another-problem" and len(page.items) == 1
            next_page = await service.list(after=page.next_after, limit=1)
            assert next_page.next_after is None and next_page.items[0].slug == "fixture-problem"
            limited = ArtifactStore(tmp_path / "limited", max_bytes=1, max_files=5)
            try:
                with pytest.raises(ArtifactError):
                    await ProblemService(
                        sessions, ArtifactRepository(limited), policy
                    ).import_directory(root)
            finally:
                limited.close()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(ProblemVersion)) == 3
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        store.close()
