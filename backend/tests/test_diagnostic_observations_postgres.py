"""PostgreSQL-backed diagnostic evidence counts."""

import asyncio
import secrets
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy import func, select
from test_authority_operations import authority
from test_submission_create import setup_app

from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.learning.diagnostic_models import DiagnosticRequestRow

_ROOT = "/api/v1/admin/learning/diagnostics"
_ORIGIN = "http://localhost:5173"
_CODE = "int main(void) { return 0; }\n"


async def seed_observations(app, principal, version):
    submission = await app.state.submissions.create(
        principal,
        CreateSubmission(
            idempotency_key=uuid4(),
            problem_version_id=version,
            language="c17",
            source=_CODE,
        ),
    )
    profile = {
        "tool": "public-sample-v1",
        "configuration_sha256": "b" * 64,
        "admission_sha256": "c" * 64,
        "factory_config_sha256": "d" * 64,
        "sandbox_wall_budget_us": 25_000_000,
    }
    linked = {
        "run_id": str(uuid4()),
        "skill_version_id": str(uuid4()),
        "configuration_sha256": "e" * 64,
    }
    async with app.state.sessions.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        cases = [
            {"mode": "optimization", "o2": "accepted"},
            {
                "mode": "optimization",
                "o2": "wrong_answer",
                "o0": "accepted",
                "review_context": linked,
            },
            {"mode": "optimization", "o2": "runtime_error", "o0": "runtime_error", "age": 8},
            {"mode": "optimization", "o2": "accepted", "trace": False},
            {
                "mode": "ubsan",
                "ubsan": {
                    "verdict": "runtime_error",
                    "finding": {"category": "signed_integer_overflow", "line": 1, "column": 4},
                },
            },
            {"mode": "ubsan", "ubsan": {"verdict": "compilation_error", "finding": None}},
            {
                "mode": "batch",
                "batch_purpose": "differential",
                "batch_outcome": "wrong_answer",
                "review_context": linked,
            },
            {
                "mode": "batch",
                "batch_purpose": "differential",
                "batch_outcome": "reduction_unverified",
            },
            {
                "mode": "batch",
                "batch_purpose": "differential",
                "batch_outcome": "reference_pending",
                "trace": False,
            },
            {"mode": "batch", "batch_purpose": "boundary", "batch_outcome": "resource_limit"},
            {"mode": "batch", "batch_purpose": "boundary", "batch_outcome": "input_not_verified"},
            {"mode": "optimization", "o2": "accepted", "age": 100},
            {"mode": "optimization", "state": "failed", "failure_code": "sandbox_unavailable"},
        ]
        for case in cases:
            tracked = case.pop("trace", True)
            age = case.pop("age", 0)
            case.setdefault("state", "observed")
            inputs = (
                {"batch_manifest_digest": "f" * 64}
                if case["mode"] == "batch"
                else {
                    "sample_ordinal": 0,
                    "input_digest": sha256(b"1\n").hexdigest(),
                    "input_bytes": 2,
                    "expected_digest": sha256(b"2\n").hexdigest(),
                    "expected_bytes": 2,
                }
            )
            start = now - timedelta(days=age, seconds=2)
            saved_profile = {
                **profile,
                "tool": "registered-batch-v1" if case["mode"] == "batch" else profile["tool"],
            }
            session.add(
                DiagnosticRequestRow(
                    id=uuid4(),
                    owner_id=principal.account_id,
                    submission_id=submission.submission_id,
                    problem_version_id=version,
                    language="c17",
                    source_digest=sha256(_CODE.encode()).hexdigest(),
                    source_bytes=len(_CODE.encode()),
                    authorization_version=principal.authorization_version,
                    session_proof="a" * 64,
                    created_at=start - timedelta(seconds=1),
                    queue_deadline=start + timedelta(minutes=2),
                    execution_id=uuid4(),
                    execution_started_at=start if tracked else None,
                    execution_deadline=start + timedelta(minutes=1),
                    execution_profile=saved_profile if tracked else None,
                    finished_at=start + timedelta(milliseconds=125),
                    **inputs,
                    **case,
                )
            )


def test_admin_observations_define_denominators_and_recheck_authority(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
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
            browser() as guest,
        ):
            db = await connect(postgres_url)
            try:
                await app.state.identity.create_account("observation_admin", password)
                await app.state.identity.create_account("observation_member", password)
                assert (await authority(db, "observation_admin", "bootstrap"))["changed"]
            finally:
                await db.close()
            assert (await guest.get(_ROOT)).status_code == 401
            for client, username in ((admin, "observation_admin"), (learner, "observation_member")):
                response = await client.post(
                    "/api/v1/auth/login", json={"username": username, "password": password}
                )
                assert response.status_code == 200, response.text
            assert (await learner.get(_ROOT)).status_code == 403
            empty = await admin.get(_ROOT)
            assert empty.status_code == 200 and empty.headers["cache-control"] == "no-store"
            assert all(row["eligible_count"] == 0 for row in empty.json()["items"])
            version = await app.state.problems.import_directory(tmp_path / "import")
            principal = await app.state.identity.authenticate(learner.cookies["kf_session"])
            await seed_observations(app, principal, version)

            response = await admin.get(_ROOT, params={"days": 7})
            assert response.status_code == 200, response.text
            data = response.json()
            assert set(data) == {"start_at", "end_at", "cohort", "items"}
            items = {row["method"]: row for row in data["items"]}
            expected = {
                "optimization": (3, 1, 2, 1),
                "ubsan": (2, 0, 1, 1),
                "differential": (3, 1, 2, 1),
                "boundary": (2, 0, 1, 1),
            }
            for method, counts in expected.items():
                assert items[method] == {
                    "method": method,
                    **dict(
                        zip(
                            (
                                "observed_count",
                                "untracked_count",
                                "eligible_count",
                                "finding_count",
                            ),
                            counts,
                            strict=True,
                        )
                    ),
                    "mean_claim_to_finish_ms": 125,
                    "max_sandbox_budget_us": 25_000_000,
                }

            longer = await admin.get(_ROOT, params={"days": 30})
            assert longer.json()["items"][0]["eligible_count"] == 3
            linked = await admin.get(_ROOT, params={"cohort": "linked_review"})
            assert [row["eligible_count"] for row in linked.json()["items"]] == [1, 0, 1, 0]
            unlinked = await admin.get(_ROOT, params={"cohort": "unlinked"})
            assert [row["finding_count"] for row in unlinked.json()["items"]] == [0, 1, 0, 1]
            assert (await admin.get(_ROOT, params={"days": 91})).status_code == 422

    asyncio.run(exercise())
