"""Only bound, byte-comparable public self-tests enter the aggregate."""

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from conftest import connect
from test_authority_operations import authority
from test_submission_create import setup_app

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic import DiagnosticObservation
from kf_oj.contracts.self_tests import CreateSelfTest
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.submissions.self_tests import self_test_manifest

_ORIGIN = "http://localhost:5173"
_ROOT = "/api/v1/admin/learning/practice"
_CODE = "int main(void) { return 0; }\n"
_FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"


async def observed_diagnostic(app, principal, version):
    submitted = await app.state.submissions.create(
        principal,
        CreateSubmission(
            idempotency_key=uuid4(),
            problem_version_id=version,
            language="c17",
            source=_CODE,
        ),
    )
    queue = app.state.agent_learning.diagnostics
    request_id = uuid4()
    await queue.enqueue(
        principal,
        request_id=request_id,
        submission_id=submitted.submission_id,
        problem_version_id=version,
        sample_ordinal=0,
        source_execution_confirmed=True,
    )
    claim = await queue.claim()
    assert claim.request_id == request_id
    await queue.acknowledge_stopped(
        claim,
        observation=DiagnosticObservation(o2=v1.Verdict.WRONG_ANSWER, o0=v1.Verdict.ACCEPTED),
    )
    return request_id


async def finish_self_test(app, operator, self_test_id, stdout, *, truncated=False, wrong_id=False):
    stored = await operator.fetchrow(
        "SELECT s.*, e.id AS evaluation_id FROM self_tests s "
        "JOIN evaluations e ON e.self_test_id=s.id WHERE s.id=$1",
        self_test_id,
    )
    async with app.state.sessions() as session:
        version = await app.state.problems.for_self_test(session, stored["problem_version_id"])
    manifest = self_test_manifest(
        version,
        stored["evaluation_id"],
        self_test_id,
        v2.Language.C17,
        v1.ArtifactReference(digest=stored["source_digest"], bytes=len(_CODE.encode())),
        v1.ArtifactReference(digest=stored["stdin_digest"], bytes=stored["stdin_bytes"]),
    )
    manifest_hash = hashlib.sha256(v1.manifest_bytes(manifest)).hexdigest()
    attempt, worker = uuid4(), uuid4()
    data = json.loads((_FIXTURE / "judge-result.valid-self-test.json").read_text())
    data["schema_version"] = manifest.schema_version
    data["binding"] = {
        "evaluation_id": str(stored["evaluation_id"]),
        "attempt_id": str(attempt),
        "worker_id": str(worker),
        "fencing_version": 1,
    }
    data["cases"][0]["case_id"] = str(self_test_id)
    data["manifest_digest"] = manifest_hash
    data["self_test_output"].update(stdout=stdout, stdout_truncated=truncated)
    program = data["compilation"]["program"]
    if manifest.schema_version == 1:
        data["compilation"]["program"] = program["files"][0]["artifact"]
    else:
        program.update(
            binding=data["binding"],
            source=manifest.source.model_dump(mode="json"),
            toolchain=manifest.toolchain.model_dump(mode="json"),
        )
    model = v1.JudgementReport if manifest.schema_version == 1 else v2.JudgementReport
    report = model.model_validate_json(json.dumps(data))
    report.validate_for_manifest(manifest, manifest_hash)
    if wrong_id:
        data["binding"]["attempt_id"] = str(uuid4())
    async with operator.transaction():
        await operator.execute(
            "INSERT INTO judge_attempts "
            "(id,evaluation_id,worker_id,number,fencing_version,lease_token_hash,expires_at,"
            "manifest,manifest_digest,cleanup_complete) "
            "VALUES ($1,$2,$3,1,1,$4,clock_timestamp()+interval '1 minute',$5::jsonb,$6,true)",
            attempt,
            stored["evaluation_id"],
            worker,
            secrets.token_hex(32),
            manifest.model_dump_json(),
            manifest_hash,
        )
        await operator.execute(
            "UPDATE evaluations SET state='finished',verdict='accepted',current_attempt_id=$2,"
            "fencing_version=1,attempts_count=1,updated_at=clock_timestamp(),"
            "final_report=$3::jsonb "
            "WHERE id=$1",
            stored["evaluation_id"],
            attempt,
            json.dumps(data),
        )


async def seed_comparisons(app, operator, principal, version, reference):
    for output, diagnostic, truncated in (
        ("2\n", None, False),
        ("3\n", reference, False),
        ("", reference, True),
    ):
        receipt = await app.state.self_tests.create(
            principal,
            CreateSelfTest(
                problem_version_id=version,
                idempotency_key=uuid4(),
                language="c17",
                source=_CODE,
                stdin="1\n",
                public_sample_ordinal=0,
                diagnostic_request_id=diagnostic,
            ),
        )
        await finish_self_test(app, operator, receipt.self_test_id, output, truncated=truncated)


def test_self_test_observation_sources_and_lossy_outputs(postgres_url, tmp_path, monkeypatch):
    async def exercise():
        monkeypatch.setenv("KF_OJ_LEARNING_DIAGNOSTICS_ENABLED", "true")
        app, engine, module = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(module, "build_engine", lambda _: engine)
        password = secrets.token_urlsafe(24)

        def browser():
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=_ORIGIN,
                headers={"origin": _ORIGIN},
            )

        async with (
            app.router.lifespan_context(app),
            browser() as admin,
            browser() as learner,
            browser() as foreign,
            browser() as second_session,
        ):
            db = await connect(postgres_url)
            try:
                for name in ("compare_admin", "compare_member", "compare_other"):
                    await app.state.identity.create_account(name, password)
                assert (await authority(db, "compare_admin", "bootstrap"))["changed"]
                accounts = (
                    (admin, "compare_admin"),
                    (learner, "compare_member"),
                    (foreign, "compare_other"),
                    (second_session, "compare_member"),
                )
                for client, username in accounts:
                    login = await client.post(
                        "/api/v1/auth/login", json={"username": username, "password": password}
                    )
                    assert login.status_code == 200, login.text
                    client.headers["x-csrf-token"] = client.cookies["kf_csrf"]
                version = await app.state.problems.import_directory(tmp_path / "import")
                actor = await app.state.identity.authenticate(learner.cookies["kf_session"])
                reference = await observed_diagnostic(app, actor, version)

                async def create(
                    client,
                    *,
                    stdout,
                    stdin="1\n",
                    diagnostic=reference,
                    truncated=False,
                    wrong_id=False,
                ):
                    body = {
                        "problem_version_id": str(version),
                        "language": "c17",
                        "source": _CODE,
                        "stdin": stdin,
                        "public_sample_ordinal": 0,
                        "idempotency_key": str(uuid4()),
                        "diagnostic_request_id": str(diagnostic) if diagnostic else None,
                    }
                    result = await client.post("/api/v1/self-tests", json=body)
                    assert result.status_code == 201, result.text
                    self_test_id = UUID(result.json()["self_test_id"])
                    assert (
                        await client.post("/api/v1/self-tests", json=body)
                    ).json() == result.json()
                    await finish_self_test(
                        app,
                        db,
                        self_test_id,
                        stdout,
                        truncated=truncated,
                        wrong_id=wrong_id,
                    )
                    return self_test_id

                direct = await create(learner, stdout="2\n", diagnostic=None)
                linked = await create(learner, stdout="3\n")
                foreign_id = await create(foreign, stdout="2\n")
                stale = await create(second_session, stdout="2\n")
                custom = await create(learner, stdin="2\n", stdout="2\n")
                await create(learner, stdout="partial", truncated=True)
                await create(learner, stdout="\ufffd")
                await create(learner, stdout="3\n", wrong_id=True)
                context_query = (
                    "SELECT diagnostic_request_id FROM self_test_public_comparisons "
                    "WHERE self_test_id=$1"
                )
                assert (
                    await db.fetchval(
                        context_query,
                        linked,
                    )
                    == reference
                )
                for other in (direct, foreign_id, stale):
                    assert (
                        await db.fetchval(
                            context_query,
                            other,
                        )
                        is None
                    )
                assert not await db.fetchval(
                    "SELECT EXISTS(SELECT FROM self_test_public_comparisons WHERE self_test_id=$1)",
                    custom,
                )
                assert (await learner.get(_ROOT)).status_code == 403
                report = await admin.get(_ROOT)
                assert report.status_code == 200, report.text
                assert report.json()["self_tests"] == [
                    {
                        "cohort": "direct_sample",
                        "completed_count": 3,
                        "compared_count": 3,
                        "mismatch_count": 0,
                    },
                    {
                        "cohort": "diagnostic_sample",
                        "completed_count": 4,
                        "compared_count": 1,
                        "mismatch_count": 1,
                    },
                ]
                assert str(reference) not in report.text
            finally:
                await db.close()

    asyncio.run(exercise())
