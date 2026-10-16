"""Real temporary PostgreSQL data/authority acceptance; no submitted code is executed."""

import asyncio
import copy
import hashlib
import json
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
from conftest import connect, migrate
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_database_foundation import fixture_rows
from test_problems import import_fixture, platform_role
from test_worker_artifacts import install_attempt

from kf_oj.api.app import create_app
from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.judging_versions import manifest_bytes
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.messaging.models import Outbox
from kf_oj.modules.problems.models import ProblemVersion
from kf_oj.modules.problems.service import ProblemService
from kf_oj.modules.submissions.models import Evaluation, Submission
from kf_oj.modules.submissions.public import IdempotencyConflict, SubmissionUnavailable

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/contracts-v2"


def configurations():
    return [
        {
            key: json.loads((FIXTURES / f"judge-manifest.valid-{language}.json").read_text())[key]
            for key in ("language", "toolchain", "compile_limits", "run_limits", "job_wall_time_us")
        }
        for language in v2.Language
    ]


def multi_import(root, *, slug="multi-runtime"):
    legacy, _ = import_fixture(root, slug=slug)
    manifest = {
        key: legacy[key]
        for key in ("slug", "title", "statement_file", "comparison", "cases", "samples")
    }
    manifest.update(schema_version=2, language_configurations=configurations())
    (root / "problem.json").write_text(json.dumps(manifest))
    registry = v1.decode_wire(
        v2.AdmissionRegistry, (FIXTURES / "judge-registry.valid.json").read_bytes()
    )
    return manifest, registry


def test_upgrade_preserves_published_rows_and_legacy_attempt_bytes(postgres_url):
    migrate(postgres_url, "downgrade", "0001_judging_foundation")

    async def seed():
        c = await connect(postgres_url)
        try:
            ids = await fixture_rows(c)
            manifest = v1.decode_wire(
                v1.JobManifest,
                (ROOT / "tests/fixtures/contracts/judge-manifest.valid.json").read_bytes(),
            )
            canonical = v1.manifest_bytes(manifest)
            await c.execute("SET ROLE kf_oj_coordinator")
            await c.execute(
                """INSERT INTO judge_attempts
                (id,evaluation_id,worker_id,number,fencing_version,lease_token_hash,
                 expires_at,manifest,manifest_digest)
                VALUES ($1,$2,$3,1,1,$4,now()+interval '30 seconds',$5,$6)""",
                uuid4(),
                ids[5],
                uuid4(),
                "d" * 64,
                canonical.decode(),
                hashlib.sha256(canonical).hexdigest(),
            )
            await c.execute("RESET ROLE")
            return await snapshot(c)
        finally:
            await c.close()

    async def snapshot(c):
        version = await c.fetchval(
            "SELECT (to_jsonb(p)-'judging_schema_version'-'language_configurations')::text "
            "FROM problem_versions p"
        )
        submission = await c.fetchval("SELECT to_jsonb(s)::text FROM submissions s")
        attempt = await c.fetchrow("SELECT manifest,manifest_digest FROM judge_attempts")
        decoded = v1.JobManifest.model_validate_json(attempt["manifest"])
        assert hashlib.sha256(v1.manifest_bytes(decoded)).hexdigest() == attempt["manifest_digest"]
        return version, submission, dict(attempt)

    before = asyncio.run(seed())
    migrate(postgres_url, "upgrade", "head")

    async def read():
        c = await connect(postgres_url)
        try:
            assert await c.fetchval("SELECT judging_schema_version FROM problem_versions") == 1
            assert await c.fetchval("SELECT language_configurations FROM problem_versions") is None
            return await snapshot(c)
        finally:
            await c.close()

    assert asyncio.run(read()) == before
    migrate(postgres_url, "downgrade", "0001_judging_foundation")
    migrate(postgres_url, "upgrade", "head")
    assert asyncio.run(read()) == before
    migrate(postgres_url, "check")


async def insert_version(c, problem, cfgs, *, languages=None, revision=1):
    version = uuid4()
    await c.execute(
        """INSERT INTO problem_versions
        (id,problem_id,revision,title,statement,languages,judging_schema_version,
         language_configurations,public_samples)
        VALUES ($1,$2,$3,'Multi runtime','Public',$4,2,$5,'[]')""",
        version,
        problem,
        revision,
        json.dumps(languages if languages is not None else [c["language"] for c in cfgs]),
        json.dumps(cfgs),
    )
    return version


def test_sql_configuration_constraints_authority_and_downgrade_refusal(postgres_url):
    async def run():
        c = await connect(postgres_url)
        try:
            await c.execute("SET ROLE kf_oj_platform")
            problem = uuid4()
            await c.execute("INSERT INTO problems(id,slug) VALUES($1,'multi-sql')", problem)
            cfgs = configurations()
            bad_sets = []
            for field, value in (
                ("language", "shell"),
                ("toolchain", None),
                ("compile_limits", None),
                ("job_wall_time_us", True),
                ("job_wall_time_us", 1000),
            ):
                bad = copy.deepcopy(cfgs)
                bad[0][field] = value
                bad_sets.append(bad)
            for field, value in (
                ("image_digest", "invalid"),
                ("image_revision", True),
                ("runtime_version", "3.14.0"),
                ("image_kind", "linux_gnu_cxx_x86_64_v1"),
            ):
                bad = copy.deepcopy(cfgs)
                bad[2]["toolchain"][field] = value
                bad_sets.append(bad)
            for field, value in (
                ("processes", 8),
                ("memory_bytes", 64 * v1.MIB),
                ("cpu_time_us", "1"),
                ("stack_bytes", 1),
            ):
                bad = copy.deepcopy(cfgs)
                bad[3]["run_limits"][field] = value
                bad_sets.append(bad)
            bad = copy.deepcopy(cfgs)
            bad[0]["argv"] = ["/bin/sh"]
            bad_sets.append(bad)
            bad = copy.deepcopy(cfgs)
            bad[0]["toolchain"]["host_path"] = "/host/private"
            bad_sets.append(bad)
            bad_sets.extend([[], None, [cfgs[0], cfgs[0]]])
            for bad in bad_sets:
                with pytest.raises(asyncpg.CheckViolationError):
                    await insert_version(c, problem, bad, languages=[c["language"] for c in cfgs])
            with pytest.raises(asyncpg.CheckViolationError):
                await insert_version(
                    c, problem, cfgs, languages=["java21", "python313", "cpp20", "c17"]
                )
            version = await insert_version(c, problem, cfgs)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute("UPDATE problem_versions SET language_configurations='[]'")
            await c.execute("RESET ROLE")
            with pytest.raises(asyncpg.CheckViolationError):
                await c.execute("UPDATE problem_versions SET toolchain='{}'")
            await c.execute("SET ROLE kf_oj_coordinator")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute("INSERT INTO problems(id,slug) VALUES($1,'forbidden')", uuid4())
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await c.execute("UPDATE problem_versions SET language_configurations='[]'")
            return version
        finally:
            await c.close()

    version = asyncio.run(run())
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        migrate(postgres_url, "downgrade", "0001_judging_foundation")
    assert b"cannot downgrade while multi-runtime data exists" in rejected.value.stderr

    async def preserved():
        c = await connect(postgres_url)
        try:
            assert (
                await c.fetchval("SELECT count(*) FROM problem_versions WHERE id=$1", version) == 1
            )
            assert (
                await c.fetchval("SELECT version_num FROM alembic_version") == "0002_multi_runtime"
            )
        finally:
            await c.close()

    asyncio.run(preserved())


def test_real_four_language_import_public_projection_and_initial_submissions(
    postgres_url, tmp_path, monkeypatch
):
    _, registry = multi_import(tmp_path / "multi")
    import_fixture(tmp_path / "legacy", slug="legacy-runtime")

    async def run():
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        mod = __import__("kf_oj.api.app", fromlist=["build_engine"])
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "artifacts",
                judge_admission=registry,
            )
        )
        async with app.router.lifespan_context(app):
            owner = await app.state.identity.create_account(
                "multi_owner", secrets.token_urlsafe(24)
            )
            version = await app.state.problems.import_directory(tmp_path / "multi")
            old = await app.state.problems.import_directory(tmp_path / "legacy")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                public = await client.get(f"/api/v1/problems/versions/{version}")
                assert public.status_code == 200
                data = public.json()
                assert data["run_limits"] is None
                assert data["languages"] == [v.value for v in v2.Language]
                assert [c["source_name"] for c in data["language_configurations"]] == [
                    "main.c",
                    "main.cpp",
                    "main.py",
                    "Main.java",
                ]
                assert data["language_configurations"][2]["preparation_kind"] == "syntax_check"
                assert data["language_configurations"][3]["entrypoint"] == "Main"
                for private in (
                    "image_digest",
                    "image_kind",
                    "hidden-input",
                    "hidden-expected",
                    str(tmp_path),
                ):
                    assert private not in public.text
                for language in v2.Language:
                    request = CreateSubmission(
                        problem_version_id=version,
                        language=language,
                        idempotency_key=uuid4(),
                        source="Synthetic transport bytes; never executed",
                    )
                    results = await asyncio.gather(
                        *(app.state.submissions.create(owner, request) for _ in range(2))
                    )
                    assert results[0].submission_id == results[1].submission_id
                    async with app.state.sessions() as s:
                        projected = await app.state.problems.for_submission(s, version)
                        ref = app.state.problems.artifacts.store.reference(request.source.encode())
                        manifest = projected.manifest(
                            results[0].evaluation_id, results[0].submission_id, language, ref
                        )
                        assert manifest.schema_version == 2 and manifest.toolchain.runtime_version
                        v2.admit_manifest(registry, manifest, 1)
                        assert manifest_bytes(manifest)
                    with pytest.raises(IdempotencyConflict):
                        await app.state.submissions.create(
                            owner, request.model_copy(update={"source": "Different"})
                        )
                with pytest.raises(SubmissionUnavailable):
                    await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=old,
                            language="python313",
                            idempotency_key=uuid4(),
                            source="x",
                        ),
                    )
                async with app.state.sessions() as s:
                    for model in (Submission, Evaluation, Outbox):
                        assert await s.scalar(select(func.count()).select_from(model)) == 4
                    row = await s.get(ProblemVersion, version)
                    assert row.toolchain is None and row.run_limits is None
                    assert len(row.language_configurations) == 4
            # Actual SQL membership guard also rejects new-language rows on old versions.
            admin = await connect(postgres_url)
            try:
                source_digest = await admin.fetchval(
                    "SELECT source_digest FROM submissions LIMIT 1"
                )
                problem_id = await admin.fetchval(
                    "SELECT problem_id FROM problem_versions WHERE id=$1", old
                )
                await admin.execute("SET ROLE kf_oj_platform")
                with pytest.raises(asyncpg.CheckViolationError):
                    await admin.execute(
                        """INSERT INTO submissions
                        (id,account_id,problem_id,problem_version_id,language,source_digest,
                         idempotency_key,request_digest)
                        VALUES($1,$2,$3,$4,'java21',$5,$6,$7)""",
                        uuid4(),
                        owner.account_id,
                        problem_id,
                        old,
                        source_digest,
                        uuid4(),
                        "a" * 64,
                    )
                await admin.execute("RESET ROLE")
                with pytest.raises(asyncpg.CheckViolationError):
                    await admin.execute(
                        "UPDATE problem_versions SET language_configurations='[]' WHERE id=$1",
                        version,
                    )
            finally:
                await admin.close()

    asyncio.run(run())


def test_import_refuses_missing_protocol_and_runtime_identity_without_writes(
    postgres_url, tmp_path
):
    manifest, registry = multi_import(tmp_path / "multi")
    store = ArtifactStore(tmp_path / "artifacts")

    async def run():
        engine = create_async_engine(postgres_url)
        platform_role(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            service = ProblemService(sessions, ArtifactRepository(store), registry.legacy)
            with pytest.raises(ValueError, match="protocol"):
                await service.import_directory(tmp_path / "multi")
            manifest["language_configurations"][2]["toolchain"]["image_digest"] = "f" * 64
            (tmp_path / "multi/problem.json").write_text(json.dumps(manifest))
            service = ProblemService(sessions, ArtifactRepository(store), registry)
            with pytest.raises(ValueError, match="capability"):
                await service.import_directory(tmp_path / "multi")
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(ProblemVersion)) == 0
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_v2_worker_reads_immutable_language_snapshot_with_live_lease(postgres_url, tmp_path):
    _, registry = multi_import(tmp_path / "multi")

    async def run():
        admin = create_async_engine(postgres_url)
        sessions = async_sessionmaker(admin, expire_on_commit=False)
        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=postgres_url,
                artifact_root=tmp_path / "artifacts",
                judge_admission=registry,
            )
        )
        try:
            async with app.router.lifespan_context(app):
                platform_role(app.state.sessions.kw["bind"])
                account = await app.state.identity.create_account(
                    "worker_owner", secrets.token_urlsafe(24)
                )
                version = await app.state.problems.import_directory(tmp_path / "multi")
                for language in ("python313", "java21"):
                    binding, token, manifest = await install_attempt(
                        sessions, app, account, version, language=language
                    )
                    # Controlled DB lease fixture, not an actual Coordinator claim or execution.
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as client:
                        path = (
                            f"/api/v1/internal/evaluations/{binding.evaluation_id}"
                            f"/attempts/{binding.attempt_id}"
                        )
                        headers = {
                            "Authorization": "Bearer " + token,
                            "X-Worker-ID": str(binding.worker_id),
                            "X-Fencing-Version": "1",
                        }
                        response = await client.get(path + "/manifest", headers=headers)
                        assert response.status_code == 200 and response.content == manifest_bytes(
                            manifest
                        )
                        assert manifest.schema_version == 2 and manifest.language == language
                        response = await client.get(
                            path + "/artifacts/" + manifest.source.digest, headers=headers
                        )
                        assert (
                            response.status_code == 200 and response.content == b"synthetic source"
                        )
                        assert (
                            await client.get(
                                path + "/manifest", headers=headers | {"X-Worker-ID": str(uuid4())}
                            )
                        ).status_code == 404
                        assert (
                            await client.get(
                                path + "/manifest", headers=headers | {"X-Fencing-Version": "2"}
                            )
                        ).status_code == 404
                        async with sessions.begin() as s:
                            await s.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                            await s.execute(
                                text("UPDATE evaluations SET state='cancelling' WHERE id=:id"),
                                {"id": binding.evaluation_id},
                            )
                        assert (
                            await client.get(path + "/manifest", headers=headers)
                        ).status_code == 404
        finally:
            await admin.dispose()

    asyncio.run(run())
