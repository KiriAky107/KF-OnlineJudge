"""Self-test API/storage checks; controlled DB fixtures are not judge executions."""

import asyncio
import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_submission_create import setup_app

from kf_oj.contracts.judging import (
    ArtifactReference,
    JobManifest,
    LeaseBinding,
    SelfTestOutput,
    manifest_bytes,
)
from kf_oj.contracts.self_tests import CreateSelfTest
from kf_oj.modules.submissions.models import Evaluation, JudgeAttempt, SelfTest, Submission
from kf_oj.modules.submissions.public import (
    IdempotencyConflict,
    SubmissionUnavailable,
    WorkerAccessDenied,
)
from kf_oj.modules.submissions.self_tests import self_test_manifest


def test_input_output_utf8_limits_and_old_canonical():
    base = dict(problem_version_id=uuid4(), language="c17", idempotency_key=uuid4(), source="x")
    CreateSelfTest(**base, stdin="é" * 32768)
    for value in ("é" * 32769, "\ud800"):
        with pytest.raises(ValueError):
            CreateSelfTest(**base, stdin=value)
    with pytest.raises(ValueError):
        SelfTestOutput(
            stdout="é" * 32769, stderr="", stdout_truncated=False, stderr_truncated=False
        )
    root = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts"
    m = JobManifest.model_validate_json((root / "judge-manifest.valid.json").read_bytes())
    assert manifest_bytes(m) == (root / "canonical-manifest.v1.txt").read_bytes()
    invalid = m.model_dump(mode="json") | {"self_test": True}
    with pytest.raises(ValueError):
        JobManifest.model_validate(invalid)


def test_real_self_test_isolation_idempotency_and_worker_artifacts(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        admin = create_async_engine(postgres_url)
        sessions = async_sessionmaker(admin, expire_on_commit=False)
        try:
            async with app.router.lifespan_context(app):
                a = await app.state.identity.create_account("selftest_a", secrets.token_urlsafe(24))
                b = await app.state.identity.create_account("selftest_b", secrets.token_urlsafe(24))
                version = await app.state.problems.import_directory(tmp_path / "import")
                request = CreateSelfTest(
                    problem_version_id=version,
                    language="c17",
                    source="synthetic, not executed",
                    stdin="user input",
                    idempotency_key=uuid4(),
                )
                views = await asyncio.gather(
                    *(app.state.self_tests.create(a, request) for _ in range(3))
                )
                sid = views[0].self_test_id
                assert len({v.self_test_id for v in views}) == 1
                with pytest.raises(IdempotencyConflict):
                    await app.state.self_tests.create(
                        a, request.model_copy(update={"stdin": "changed"})
                    )
                with pytest.raises(SubmissionUnavailable):
                    await app.state.self_tests.detail(b, sid)
                assert (await app.state.submission_queries.page(a, None, 20)).items == ()
                assert (await app.state.learning.overview(a)).submission_count == 0
                async with sessions() as s:
                    assert await s.scalar(select(func.count()).select_from(Submission)) == 0
                    st = await s.get(SelfTest, sid)
                    ev = await s.scalar(select(Evaluation).where(Evaluation.self_test_id == sid))
                    projected = await app.state.problems.for_self_test(s, version)
                    assert projected.cases == ()
                    source = ArtifactReference(
                        digest=st.source_digest, bytes=len(request.source.encode())
                    )
                    stdin = ArtifactReference(digest=st.stdin_digest, bytes=st.stdin_bytes)
                    manifest = self_test_manifest(
                        projected, ev.id, sid, request.language, source, stdin
                    )
                    hidden = await s.scalar(
                        text(
                            "SELECT expected_digest FROM problem_cases WHERE version_id=:id LIMIT 1"
                        ),
                        {"id": version},
                    )
                attempt, worker = uuid4(), uuid4()
                token = secrets.token_hex(32)
                async with sessions.begin() as s:
                    await s.execute(text("SET LOCAL ROLE kf_oj_coordinator"))
                    now = await s.scalar(select(func.now()))
                    s.add(
                        JudgeAttempt(
                            id=attempt,
                            evaluation_id=ev.id,
                            worker_id=worker,
                            number=1,
                            fencing_version=1,
                            lease_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            expires_at=now + timedelta(minutes=1),
                            manifest=manifest.model_dump(mode="json"),
                            manifest_digest=hashlib.sha256(manifest_bytes(manifest)).hexdigest(),
                        )
                    )
                    await s.flush()
                    await s.execute(
                        update(Evaluation)
                        .where(Evaluation.id == ev.id)
                        .values(
                            current_attempt_id=attempt,
                            state="claimed",
                            attempts_count=1,
                            fencing_version=1,
                        )
                    )
                binding = LeaseBinding(
                    evaluation_id=ev.id, attempt_id=attempt, worker_id=worker, fencing_version=1
                )
                raw = await app.state.worker_access.manifest(binding, token)
                assert json.loads(raw)["self_test"] is True
                assert (
                    await app.state.worker_access.artifact(binding, token, stdin.digest)
                    == b"user input"
                )
                with pytest.raises(WorkerAccessDenied):
                    await app.state.worker_access.artifact(binding, token, hidden)
                with pytest.raises(WorkerAccessDenied):
                    await app.state.worker_access.manifest(binding, "0" * 64)
                async with sessions.begin() as s:
                    await s.execute(
                        update(JudgeAttempt)
                        .where(JudgeAttempt.id == attempt)
                        .values(revoked_at=func.now())
                    )
                with pytest.raises(WorkerAccessDenied):
                    await app.state.worker_access.manifest(binding, token)
        finally:
            await admin.dispose()

    asyncio.run(run())


def test_private_output_projection_checks_execution_binding():
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import UUID

    from kf_oj.modules.submissions.self_tests import SelfTestService

    root = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts"
    report = json.loads((root / "judge-result.valid-ce.json").read_text())
    st = SimpleNamespace(
        id=uuid4(), problem_version_id=uuid4(), language="c17", created_at=datetime.now(UTC)
    )
    ev = SimpleNamespace(
        id=UUID(report["binding"]["evaluation_id"]),
        current_attempt_id=UUID(report["binding"]["attempt_id"]),
        fencing_version=report["binding"]["fencing_version"],
        state="finished",
        verdict="compilation_error",
        failure_code=None,
        final_report=report,
    )
    assert SelfTestService._response(st, ev).compile_diagnostics is not None
    ev.current_attempt_id = uuid4()
    with pytest.raises(ValueError, match="binding"):
        SelfTestService._response(st, ev)


def test_shared_self_test_canonical_and_report_binding():
    from kf_oj.contracts.judging_v2 import JobManifest as V2Manifest
    from kf_oj.contracts.judging_v2 import JudgementReport

    root = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
    manifest = V2Manifest.model_validate_json(
        (root / "judge-manifest.valid-self-test.json").read_bytes()
    )
    canonical = (root / "canonical-manifest.self-test.txt").read_bytes()
    assert manifest_bytes(manifest) == canonical
    report = JudgementReport.model_validate_json(
        (root / "judge-result.valid-self-test.json").read_bytes()
    )
    report.validate_for_manifest(manifest, hashlib.sha256(canonical).hexdigest())
    assert report.self_test_output.stdout == "42\n"
