"""Upgrade, adverse storage and cross-process admission: capped independent VM only."""

import asyncio
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from conftest import connect
from sqlalchemy.engine import make_url
from test_admin_import_vm import independent_import_vm as independent_import_vm

from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.modules.identity.passwords import hash_password

ROOT = Path(__file__).resolve().parents[2]


def policy():
    return AdmissionRegistry.model_validate_json(
        (ROOT / "tests/fixtures/contracts-v2/judge-registry.valid.json").read_bytes()
    )


def proof(name, value):
    print(
        "KF_FINAL_ACCEPTANCE " + json.dumps({"name": name, "result": value}, default=str),
        flush=True,
    )


def test_cross_process_admission_and_worker_identity(independent_import_vm, postgres_url, tmp_path):
    groups = independent_import_vm
    roles = {name: "final_" + name + "_" + uuid4().hex for name in ("api", "worker")}
    users = {f"capacity_admin_{i}": uuid4() for i in range(5)}
    password = "Synthetic capacity acceptance only 2026!"
    artifact, staging = tmp_path / "artifacts", tmp_path / "staging"
    artifact.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    processes, logs = {}, {}
    base = {
        k: v
        for k, v in os.environ.items()
        if k
        in (
            "PATH",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "LC_ALL",
            "PYTHONUNBUFFERED",
            "PYTHONDONTWRITEBYTECODE",
        )
    }

    def enter(group):
        def apply():
            (group / "cgroup.procs").write_text(str(os.getpid()))
            resource.setrlimit(resource.RLIMIT_FSIZE, (67108864, 67108864))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return apply

    def start(name, *, bad_identity=False, bad_group=False):
        worker = name == "worker"
        group = groups["import-worker" if worker else "api"]
        env = base | {
            "KF_OJ_DATABASE_URL": make_url(postgres_url)
            .set(username=roles["api" if bad_identity or not worker else "worker"])
            .render_as_string(hide_password=False),
            "KF_OJ_ENVIRONMENT": "test",
            "KF_OJ_ARTIFACT_ROOT": str(artifact),
            "KF_OJ_IMPORT_STAGING_ROOT": str(staging),
            "KF_OJ_JUDGE_ADMISSION": policy().model_dump_json(),
            "KF_OJ_BROWSER_ORIGINS": '["http://127.0.0.1:58000"]',
        }
        if worker:
            env["KF_OJ_IMPORT_WORKER_CGROUP"] = str(groups["api"] if bad_group else group)
            command = [sys.executable, "-m", "kf_oj.workers.problem_import"]
        else:
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                "kf_oj.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "58000" if name == "api0" else "58001",
                "--no-proxy-headers",
                "--limit-concurrency",
                "4",
                "--backlog",
                "16",
            ]
        log = (tmp_path / (name + ".log")).open("a")
        logs[name] = log
        processes[name] = subprocess.Popen(
            command, env=env, stdout=log, stderr=log, preexec_fn=enter(group)
        )

    def stop(name):
        process = processes.pop(name, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if name in logs:
            logs.pop(name).close()

    async def run():
        connection = await connect(postgres_url)
        try:
            for name, membership in (("api", "kf_oj_platform"), ("worker", "kf_oj_problem_import")):
                await connection.execute(
                    f"CREATE ROLE {roles[name]} LOGIN NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                await connection.execute(f"GRANT {membership} TO {roles[name]}")
            for name, identifier in users.items():
                await connection.execute(
                    "INSERT INTO accounts(id,username,password_hash) VALUES($1,$2,$3)",
                    identifier,
                    name,
                    hash_password(name, password),
                )
                await connection.execute(
                    "SELECT * FROM kf_manage_account_authority($1,$2)",
                    name,
                    "bootstrap" if name.endswith("_0") else "grant",
                )
            for wrong in ("identity", "cgroup"):
                start("worker", bad_identity=wrong == "identity", bad_group=wrong == "cgroup")
                assert await asyncio.to_thread(processes["worker"].wait, 120) != 0
                assert not await connection.fetchval(
                    "SELECT worker_ready_until>clock_timestamp() "
                    "FROM problem_import_pool WHERE id=1"
                )
                stop("worker")
            start("worker")
            deadline = time.monotonic() + 180
            while not await connection.fetchval(
                "SELECT worker_ready_until>clock_timestamp() FROM problem_import_pool WHERE id=1"
            ):
                assert time.monotonic() < deadline and processes["worker"].poll() is None
                await asyncio.sleep(0.1)
            # Quiesce consumption. The actual verified heartbeat is retained for
            # finite admission checks; synthetic capacity rows cannot be parsed.
            stop("worker")
            start("api0")
            start("api1")
            prefix = "/api/v1/admin/problem-imports"
            async with (
                httpx.AsyncClient(base_url="http://127.0.0.1:58000", timeout=30) as first,
                httpx.AsyncClient(base_url="http://127.0.0.1:58001", timeout=30) as second,
            ):
                for client in (first, second):
                    deadline = time.monotonic() + 300
                    while True:
                        try:
                            if (await client.get("/api/v1/health/live")).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        assert time.monotonic() < deadline
                        await asyncio.sleep(0.1)
                    result = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "username": "capacity_admin_0",
                            "password": password,
                        },
                        headers={"Origin": "http://127.0.0.1:58000"},
                    )
                    assert result.status_code == 200, result.text

                def headers(client):
                    return {
                        "Origin": "http://127.0.0.1:58000",
                        "X-CSRF-Token": client.cookies["kf_csrf"],
                    }

                def body(key=None):
                    return {
                        "operation_id": str(key or uuid4()),
                        "archive_digest": "a" * 64,
                        "archive_bytes": 100,
                    }

                async def refresh_worker():
                    previous = await connection.fetchval(
                        "SELECT worker_ready_until FROM problem_import_pool WHERE id=1"
                    )
                    start("worker")
                    deadline = time.monotonic() + 120
                    while True:
                        current = await connection.fetchval(
                            "SELECT worker_ready_until FROM problem_import_pool WHERE id=1"
                        )
                        if current and (previous is None or current > previous):
                            break
                        assert time.monotonic() < deadline
                        assert processes["worker"].poll() is None
                        await asyncio.sleep(0.1)
                    stop("worker")

                await refresh_worker()
                request = body()
                replies = await asyncio.gather(
                    *(
                        client.post(prefix, json=request, headers=headers(client))
                        for client in (first, second)
                    )
                )
                # Identical UUIDs converge across independent API processes. A
                # transient root-lock refusal is retried once after the winner.
                assert 201 in [r.status_code for r in replies]
                retry = await second.post(prefix, json=request, headers=headers(second))
                assert retry.status_code == 201
                assert (
                    await connection.fetchval("SELECT count(*) FROM problem_import_operations") == 1
                )
                assert (
                    await connection.fetchval("SELECT reserved_bytes FROM problem_import_pool")
                    == 134217728
                )
                other = await first.post(prefix, json=body(), headers=headers(first))
                assert other.status_code == 503
                discarded = await first.post(
                    prefix + "/" + request["operation_id"] + "/discard", headers=headers(first)
                )
                assert discarded.status_code == 200, discarded.text
                assert (
                    await connection.fetchval("SELECT reserved_bytes FROM problem_import_pool") == 0
                )

                async def capacity_rows(count):
                    # Explicit unmeasured database fixtures exercise owner/global
                    # admission boundaries; no ready candidate or grading is forged.
                    await connection.execute(
                        "DELETE FROM problem_import_operations WHERE state='uploaded'"
                    )
                    identifiers = list(users.values())
                    for i in range(count):
                        await connection.execute(
                            """
                          INSERT INTO problem_import_operations(id,owner_id,authorization_version,
                          archive_digest,archive_bytes,state,reserved_bytes,expires_at,updated_at)
                          VALUES($1,$2,2,$3,100,'uploaded',0,
                            clock_timestamp()+interval '1 hour',clock_timestamp())
                        """,
                            uuid4(),
                            identifiers[0] if count == 10 else identifiers[1 + i % 4],
                            "b" * 64,
                        )

                results = {}
                for label, count in (("owner_limit", 10), ("global_limit", 32)):
                    await capacity_rows(0)
                    await refresh_worker()
                    await capacity_rows(count)
                    before = await connection.fetchval(
                        "SELECT count(*) FROM problem_import_operations"
                    )
                    response = await second.post(prefix, json=body(), headers=headers(second))
                    assert response.status_code == 503
                    assert (
                        await connection.fetchval("SELECT count(*) FROM problem_import_operations")
                        == before
                    )
                    results[label] = {
                        "fixture_count": count,
                        "actual_http_status": 503,
                        "no_new_receipt": True,
                    }
                await capacity_rows(0)
                await refresh_worker()
                resumed = body()
                result = await second.post(prefix, json=resumed, headers=headers(second))
                assert result.status_code == 201, result.text
                assert (
                    await first.post(
                        prefix + "/" + resumed["operation_id"] + "/discard", headers=headers(first)
                    )
                ).status_code == 200
                assert (
                    await connection.fetchval("SELECT reserved_bytes FROM problem_import_pool") == 0
                )
                proof(
                    "cross_process_admission",
                    results
                    | {
                        "two_actual_api_processes": True,
                        "identical_uuid_one_receipt": True,
                        "single_slot_http_status": 503,
                        "wrong_worker_identity_and_group_exit_nonzero": True,
                        "ordinary_worker_heartbeat_verified": True,
                        "recovery_new_operation_201": True,
                        "remaining_reserved_bytes": 0,
                    },
                )
        finally:
            for name in list(processes):
                stop(name)
            for role in roles.values():
                await connection.execute(f"DROP OWNED BY {role}")
                await connection.execute(f"DROP ROLE {role}")
            await connection.close()

    asyncio.run(run())
