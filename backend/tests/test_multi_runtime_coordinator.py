"""Actual finite Rust Coordinator/PG control acceptance; reports are synthetic, no execution."""

import asyncio
import copy
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from test_coordinator import claim, fixture, manifest, notice, synthetic_report, worker_headers
from test_multi_runtime_versions import multi_import

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts import judging_versions as versions


def report_v2(grant, task):
    result = synthetic_report(grant, task)
    result["schema_version"] = 2
    language = v2.Language(task["language"])
    kind = v2.runtime_contract(language)[3]
    if kind == v2.ProgramKind.PYTHON:
        files = [dict(path="main.py", artifact=task["source"])]
    elif kind == v2.ProgramKind.JAVA:
        files = [
            dict(path=name, artifact=dict(digest="d" * 64, bytes=256))
            for name in ("Helper.class", "Main$Inner.class", "Main.class")
        ]
    else:
        files = [dict(path="main", artifact=dict(digest="d" * 64, bytes=4096))]
    result["compilation"]["program"] = dict(
        kind=kind.value,
        binding=grant["binding"],
        source=task["source"],
        toolchain=task["toolchain"],
        files=files,
    )
    return result


def stage_v2(grant, language, sequence, stage="compiling", index=None):
    return dict(
        schema_version=2,
        kind="judge.stage",
        binding=grant["binding"],
        sequence=sequence,
        stage=stage,
        case_index=index,
        preparation_kind=v2.runtime_contract(v2.Language(language))[4].value,
    )


def test_actual_versioned_claim_canonical_snapshot_stage_and_report(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch, import_builder=multi_import) as (
            client,
            c,
            workers,
            _,
            create,
        ):
            for language in v2.Language:
                created = await create(language.value)
                grant = await claim(client, created, workers[0])
                assert grant["schema_version"] == 2
                task = await manifest(c, grant)
                assert task["schema_version"] == 2 and task["language"] == language.value
                parsed = versions.decode_manifest(json.dumps(task).encode())
                canonical = versions.manifest_bytes(parsed)
                assert hashlib.sha256(canonical).hexdigest() == grant["manifest_digest"]
                async with client.fixture_app.state.sessions() as s:
                    version = await client.fixture_app.state.problems.for_submission(
                        s, created.problem_version_id
                    )
                    expected = version.manifest(
                        created.evaluation_id, created.submission_id, language, parsed.source
                    )
                    assert versions.manifest_bytes(expected) == canonical
                headers = worker_headers(workers[0], grant)
                bad = stage_v2(grant, language, 1)
                bad["preparation_kind"] = (
                    "compile" if language == v2.Language.PYTHON313 else "syntax_check"
                )
                assert (
                    await client.post("/internal/stage", json=bad, headers=headers)
                ).status_code == 422
                old = stage_v2(grant, language, 1)
                old.update(schema_version=1)
                old.pop("preparation_kind")
                assert (
                    await client.post("/internal/stage", json=old, headers=headers)
                ).status_code == 422
                for sequence, phase, index in (
                    (1, "compiling", None),
                    (2, "running", 0),
                    (3, "running", 1),
                ):
                    stage = stage_v2(grant, language, sequence, phase, index)
                    assert (
                        await client.post("/internal/stage", json=stage, headers=headers)
                    ).status_code == 204
                    assert (
                        await client.post("/internal/stage", json=stage, headers=headers)
                    ).status_code == 204
                result = report_v2(grant, task)
                for invalid in (
                    result | {"manifest_digest": "f" * 64},
                    result | {"binding": grant["binding"] | {"fencing_version": 2}},
                ):
                    assert (
                        await client.post("/internal/report", json=invalid, headers=headers)
                    ).status_code in (409, 422)
                changed = copy.deepcopy(result)
                changed["compilation"]["program"]["toolchain"]["image_digest"] = "e" * 64
                assert (
                    await client.post("/internal/report", json=changed, headers=headers)
                ).status_code == 422
                assert (
                    await client.post("/internal/report", json=result, headers=headers)
                ).status_code == 204
                assert (
                    await client.post("/internal/report", json=result, headers=headers)
                ).status_code == 204
                final = await c.fetchval(
                    "SELECT final_report FROM evaluations WHERE id=$1", created.evaluation_id
                )
                assert json.loads(final) == result
                assert (
                    await c.fetchval(
                        "SELECT state FROM evaluations WHERE id=$1", created.evaluation_id
                    )
                    == "finished"
                )
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM case_results WHERE evaluation_id=$1",
                        created.evaluation_id,
                    )
                    == 2
                )
                # No real program/Worker/Root executed; accepted is a synthetic protocol value only.

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["legacy", "identity", "budget"])
def test_actual_claim_refuses_missing_protocol_or_changed_capability_without_attempt(
    postgres_url,
    tmp_path,
    monkeypatch,
    kind,
):
    from pathlib import Path

    from kf_oj.contracts.judging import decode_wire

    fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"
    registry = decode_wire(
        v2.AdmissionRegistry, (fixtures / "judge-registry.valid.json").read_bytes()
    )
    raw = registry.model_dump(mode="json")
    if kind == "identity":
        raw["multi_runtime"]["capabilities"][2]["toolchain"]["image_digest"] = "e" * 64
    elif kind == "budget":
        raw["multi_runtime"]["capabilities"][2]["run_ceiling"]["memory_bytes"] = 32 * 1024 * 1024
    policy = registry.legacy if kind == "legacy" else v2.AdmissionRegistry.model_validate(raw)

    async def run():
        async with fixture(
            postgres_url,
            tmp_path,
            monkeypatch,
            import_builder=multi_import,
            node_policy=policy,
        ) as (client, c, workers, _, create):
            created = await create("python313")
            response = await client.post(
                "/internal/claim",
                json=notice(created.evaluation_id),
                headers=worker_headers(workers[0]),
            )
            assert response.status_code == 422
            assert await c.fetchval("SELECT count(*) FROM judge_attempts") == 0
            assert await c.fetchval("SELECT state FROM evaluations") == "queued"
            assert (await client.get("/health/ready")).status_code == 200

    asyncio.run(run())


def test_actual_v2_cancel_requires_matching_stop_protocol_and_fence(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch, import_builder=multi_import) as (
            client,
            c,
            workers,
            platform,
            create,
        ):
            created = await create("java21")
            grant = await claim(client, created, workers[0])
            assert (
                await client.post(
                    "/internal/cancel",
                    json=notice(created.evaluation_id),
                    headers={"Authorization": "Bearer " + platform},
                )
            ).status_code == 204
            stopped = dict(
                schema_version=2,
                kind="judge.stopped",
                binding=grant["binding"],
                cleanup_complete=True,
            )
            headers = worker_headers(workers[0])
            assert (
                await client.post(
                    "/internal/stopped", json=stopped | {"schema_version": 1}, headers=headers
                )
            ).status_code == 422
            bad = stopped | {"binding": grant["binding"] | {"attempt_id": str(uuid4())}}
            assert (
                await client.post("/internal/stopped", json=bad, headers=headers)
            ).status_code == 409
            assert (
                await client.post("/internal/stopped", json=stopped, headers=headers)
            ).status_code == 204
            assert await c.fetchval("SELECT state FROM evaluations") == "cancelled"
            assert (
                await c.fetchval(
                    "SELECT cleanup_complete FROM judge_attempts WHERE id=$1",
                    UUID(grant["binding"]["attempt_id"]),
                )
                is True
            )
            # This proves stop-report authority only; real JVM cleanup remains VM acceptance.

    asyncio.run(run())
