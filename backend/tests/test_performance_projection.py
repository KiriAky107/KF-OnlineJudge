"""Finite actual control-plane PG/HTTP tests; all reports explicitly synthetic.

No user program runs here. Privilege/tamper/fault acceptance runs in the VM.
"""

import asyncio
import subprocess

from test_coordinator import (
    ROOT,
    claim,
    fixture,
    manifest,
    stages,
    synthetic_report,
    worker_headers,
)


def test_final_projection_atomic_idempotent_and_bound(postgres_url, tmp_path, monkeypatch):
    async def run():
        async with fixture(
            postgres_url, tmp_path, monkeypatch, measurement_epochs=["a" * 64, "b" * 64]
        ) as (client, c, workers, _, create):
            groups = []
            for index in [0, 0, 1]:
                created = await create()
                worker = workers[index]
                grant = await claim(client, created, worker)
                await stages(client, worker, grant)
                report = synthetic_report(grant, await manifest(c, grant))
                result = await client.post(
                    "/internal/report", json=report, headers=worker_headers(worker, grant)
                )
                assert result.status_code == 204, result.text
                row = await c.fetchrow(
                    "SELECT p.*,e.final_report_digest,a.report_digest AS attempt_digest "
                    "FROM evaluation_performance p JOIN evaluations e ON e.id=p.evaluation_id "
                    "JOIN judge_attempts a ON a.id=p.attempt_id WHERE p.evaluation_id=$1",
                    created.evaluation_id,
                )
                assert row is not None
                assert row["cpu_time_us"] == report["run_cpu_time_us"]
                assert row["memory_peak_bytes"] == report["run_memory_peak_bytes"]
                assert row["measurement_epoch"] == worker["measurement_epoch"]
                assert row["report_digest"] == row["final_report_digest"] == row["attempt_digest"]
                groups.append(row["comparison_group"])
                repeated = await client.post(
                    "/internal/report", json=report, headers=worker_headers(worker, grant)
                )
                assert repeated.status_code == 204
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM evaluation_performance WHERE evaluation_id=$1",
                        created.evaluation_id,
                    )
                    == 1
                )
                backfill = await asyncio.to_thread(
                    subprocess.run,
                    [
                        str(ROOT / "judge/target/debug/kf-coordinator"),
                        "--config",
                        str(client.coordinator_config),
                        "--backfill-performance",
                        str(created.evaluation_id),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert backfill.returncode == 0, backfill.stderr
                assert backfill.stdout.strip() == "trusted performance rows created: 0"
            assert groups[0] == groups[1] and groups[2] != groups[0]
            assert await c.fetchval("SELECT count(*) FROM evaluation_performance") == 3
            # Read grants rather than attempting denied writes on this host.
            grants = await c.fetchrow(
                "SELECT has_table_privilege('kf_oj_platform','evaluation_performance','SELECT') "
                "AS read,has_table_privilege('kf_oj_platform','evaluation_performance','INSERT') "
                "AS platform_insert,has_table_privilege('kf_oj_coordinator',"
                "'evaluation_performance','INSERT') AS coord_insert,has_table_privilege("
                "'kf_oj_coordinator','evaluation_performance','UPDATE') AS coord_update,"
                "has_column_privilege('kf_oj_coordinator','judge_attempts',"
                "'measurement_epoch','UPDATE') AS epoch_update"
            )
            assert dict(grants) == dict(
                read=True,
                platform_insert=False,
                coord_insert=True,
                coord_update=False,
                epoch_update=False,
            )

    asyncio.run(run())


def test_unbound_legacy_and_nonaccepted_stay_out(postgres_url, tmp_path, monkeypatch):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (client, c, workers, _, create):
            for verdict in ["accepted", "wrong_answer"]:
                created = await create()
                grant = await claim(client, created, workers[0])
                await stages(client, workers[0], grant)
                report = synthetic_report(grant, await manifest(c, grant), verdict)
                result = await client.post(
                    "/internal/report", json=report, headers=worker_headers(workers[0], grant)
                )
                assert result.status_code == 204, result.text
                assert await c.fetchval(
                    "SELECT final_report_digest IS NOT NULL FROM evaluations WHERE id=$1",
                    created.evaluation_id,
                )
            assert await c.fetchval("SELECT count(*) FROM evaluation_performance") == 0

    asyncio.run(run())
