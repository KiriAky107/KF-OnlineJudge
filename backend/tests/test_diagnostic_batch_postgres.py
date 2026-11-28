"""Bind private diagnostic data through the real Coordinator and operator CLI."""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
from base64 import b64decode
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from test_coordinator import (
    ROOT,
    claim,
    fixture,
    manifest,
    stages,
    synthetic_report,
    worker_headers,
)

from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.core.settings import Settings
from kf_oj.workers.diagnostic_batch import load_registered_batch, registered_batch_directory


def test_registered_paths_are_canonical_digests(tmp_path):
    digest = "a" * 64
    package = registered_batch_directory(tmp_path, digest)
    assert package == tmp_path / digest
    for path in ("../package", "/tmp/package", "A" * 64, "a" * 63):
        with pytest.raises(ValueError, match="invalid diagnostic batch digest"):
            registered_batch_directory(tmp_path, path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o700)
    package.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(ValueError, match="directory must not contain links"):
        asyncio.run(
            load_registered_batch(
                tmp_path, digest, Settings(_env_file=None, environment="test"), None
            )
        )


def test_operator_batch_preflight_binds_reference_and_generated_cases(
    postgres_url, tmp_path, monkeypatch
):
    async def exercise():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (client, db, workers, _, create):
            created = await create()
            worker = workers[0]
            grant = await claim(client, created, worker)
            await stages(client, worker, grant)
            admitted = await manifest(db, grant)
            reply = await client.post(
                "/internal/report",
                json=synthetic_report(grant, admitted),
                headers=worker_headers(worker, grant),
            )
            assert reply.status_code == 204, reply.text
            report_digest = await db.fetchval(
                "SELECT final_report_digest FROM evaluations WHERE id=$1",
                created.evaluation_id,
            )
            assert report_digest
            assert await db.fetchval("SELECT count(*) FROM evaluation_performance") == 0

            root = tmp_path / "batch"
            root.mkdir(mode=0o700)
            batch_path = root / "diagnostic-batch.json"

            def file(name, data):
                (root / name).write_bytes(data)
                return {
                    "name": name,
                    "artifact": {"digest": sha256(data).hexdigest(), "bytes": len(data)},
                }

            batch = {
                "schema_version": 1,
                "kind": "diagnostic.batch-inputs",
                "problem_version_id": str(client.fixture_version),
                "language": "c17",
                "purpose": "differential",
                "generation": {
                    "recipe": "integer-pairs",
                    "revision": file("generator-revision", b"generate fixture pairs using seed 19"),
                    "seed": 19,
                },
                "oracle": {
                    "method": "manual-arithmetic",
                    "revision": file("oracle-revision", b"independently checked integer sums"),
                },
                "reference": {
                    "origin": "operator_reviewed",
                    "submission_id": str(created.submission_id),
                    "evaluation_id": str(created.evaluation_id),
                    "accepted_report_sha256": report_digest,
                    "source": admitted["source"],
                },
                "cases": [
                    {
                        "input": file("input-a", b"-3 10\n"),
                        "independent_expected": file("expected-a", b"7\n"),
                    },
                    {
                        "input": file("input-b", b"8 5\n"),
                        "independent_expected": file("expected-b", b"13\n"),
                    },
                ],
            }
            registry = AdmissionRegistry.model_validate_json(
                (ROOT / "tests/fixtures/contracts-v2/judge-registry.valid.json").read_bytes()
            )
            role = "diag_batch_" + uuid4().hex
            password = secrets.token_hex(32)
            await db.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
            await db.execute(f"GRANT kf_oj_platform TO {role}")
            url = make_url(postgres_url).set(username=role, password=password)
            env = os.environ | {
                "KF_OJ_DATABASE_URL": url.render_as_string(hide_password=False),
                "KF_OJ_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
                "KF_OJ_JUDGE_ADMISSION": registry.model_dump_json(),
                "KF_OJ_ENVIRONMENT": "test",
            }

            async def check(value, *, pin=None, code=0, registered=False, propose=False):
                payload = json.dumps(value).encode()
                digest = sha256(payload).hexdigest()
                batch_path.write_bytes(payload)
                directory = root
                flags = []
                if registered:
                    directory = tmp_path / "registered"
                    directory.mkdir(mode=0o700, exist_ok=True)
                    shutil.copytree(root, directory / digest)
                    flags.append("--registry")
                if propose:
                    flags.append("--propose-reduction")
                result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-m",
                        "kf_oj.workers.diagnostic_batch",
                        str(directory),
                        "--manifest-sha256",
                        digest if pin is None else pin,
                        *flags,
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                assert result.returncode == code, result.stderr
                if code:
                    assert not result.stdout and "evidence rejected" in result.stderr
                    assert "expected-a" not in result.stderr
                    return None
                receipt = json.loads(result.stdout)
                if propose:
                    assert receipt["state"] == "verification_pending"
                    return receipt
                assert set(receipt) == {
                    "schema_version",
                    "problem_version_id",
                    "purpose",
                    "manifest_sha256",
                    "runtime_sha256",
                    "state",
                }
                assert receipt["manifest_sha256"] == digest
                assert receipt["problem_version_id"] == value["problem_version_id"]
                return receipt

            try:
                assert (await check(batch))["state"] == "ready_for_replay"
                assert (await check(batch, registered=True))["state"] == "ready_for_replay"
                checked = deepcopy(batch)
                checked["input_checker"] = {
                    "constraints_revision": file("constraints", b"second integer is positive"),
                    "source": file("checker-source", b"independent input checker source"),
                }
                assert (await check(checked))["state"] == "ready_for_replay"
                counted = deepcopy(checked)
                counted["generation"]["recipe"] = "counted-i64-list-v1"
                original = b"4\n10 20 30 40\n"
                counted["cases"] = [
                    {
                        "input": file("counted-original", original),
                        "independent_expected": file("counted-original-expected", b"100\n"),
                    },
                    {
                        "input": file("counted-other", b"1\n2\n"),
                        "independent_expected": file("counted-other-expected", b"2\n"),
                    },
                ]
                proposals = await check(counted, registered=True, propose=True)
                assert proposals["original_input_sha256"] == sha256(original).hexdigest()
                assert {b64decode(item["input_base64"]) for item in proposals["candidates"]} == {
                    b"2\n10 20\n",
                    b"2\n20 30\n",
                    b"2\n30 40\n",
                }
                reduction = deepcopy(checked)
                reduction["reduction"] = {
                    "original_input_sha256": batch["cases"][0]["input"]["artifact"]["digest"],
                    "smaller_input_sha256": batch["cases"][1]["input"]["artifact"]["digest"],
                }
                assert (await check(reduction))["state"] == "ready_for_replay"
                reduction.pop("input_checker")
                await check(reduction, code=1)
                borrowed = deepcopy(checked)
                borrowed["input_checker"]["source"] = checked["oracle"]["revision"]
                await check(borrowed, code=1)
                (root / "constraints").write_bytes(b"second integer is unrestricted")
                await check(checked, code=1)
                await check(batch, pin="0" * 64, code=1)
                (root / "expected-a").write_bytes(b"8\n")
                await check(batch, code=1)
                (root / "expected-a").write_bytes(b"7\n")
                (root / "generator-revision").write_bytes(b"generate fixture pairs using seed 20")
                await check(batch, code=1)
                (root / "generator-revision").write_bytes(b"generate fixture pairs using seed 19")

                for field, invalid in (
                    ("accepted_report_sha256", "0" * 64),
                    ("source", admitted["source"] | {"digest": "0" * 64}),
                    ("evaluation_id", str(uuid4())),
                ):
                    different = deepcopy(batch)
                    different["reference"][field] = invalid
                    await check(different, code=1)
                other_version = await client.fixture_app.state.problems.import_directory(
                    tmp_path / "import"
                )
                await check(batch | {"problem_version_id": str(other_version)}, code=1)
                await check(batch | {"language": "cpp20"}, code=1)

                pending = deepcopy(batch)
                pending["reference"] = {"origin": "model_candidate", "source": admitted["source"]}
                assert (await check(pending))["state"] == "reference_pending"
                pending.pop("oracle")
                for item in pending["cases"]:
                    item.pop("independent_expected")
                assert (await check(pending))["state"] == "reference_pending"

                boundary = deepcopy(pending)
                boundary["purpose"] = "boundary"
                boundary.pop("reference")
                assert (await check(boundary))["state"] == "ready_for_replay"
                boundary["cases"][0]["independent_expected"] = batch["cases"][0][
                    "independent_expected"
                ]
                await check(boundary, code=1)

                bigger = deepcopy(batch)
                for index in (3, 4):
                    bigger["cases"].append(
                        {
                            "input": file(f"input-{index}", f"{index} 1\n".encode()),
                            "independent_expected": file(
                                f"expected-{index}", f"{index + 1}\n".encode()
                            ),
                        }
                    )
                await check(bigger, code=1)
                assert await db.fetchval("SELECT count(*) FROM evaluations") == 1
            finally:
                await db.execute(f"DROP ROLE {role}")

    asyncio.run(exercise())
