"""Real maintenance concurrency/SQL/HTTP acceptance, independent capped VM only."""

import asyncio
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy.engine import make_url
from test_admin_import_vm import independent_import_vm as independent_import_vm
from test_admin_import_vm import package

from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.modules.identity.passwords import hash_password

ROOT = Path(__file__).resolve().parents[2]


def test_actual_publication_receipts_concurrency_audit_and_disable(
    independent_import_vm, postgres_url, tmp_path
):
    groups = independent_import_vm
    policy = AdmissionRegistry.model_validate_json(
        (ROOT / "tests/fixtures/contracts-v2/judge-registry.valid.json").read_bytes()
    )
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
    roles = {"api": "import_api_" + uuid4().hex, "worker": "import_worker_" + uuid4().hex}
    password = "Synthetic import acceptance only 2026!"
    users = {name: uuid4() for name in ("web_admin", "web_backup", "web_student")}
    processes = {}
    logs = {}
    artifact = tmp_path / "artifacts"
    staging = tmp_path / "staging"
    artifact.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)

    def enter(group):
        def apply():
            (group / "cgroup.procs").write_text(str(os.getpid()))
            resource.setrlimit(resource.RLIMIT_FSIZE, (67108864, 67108864))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        return apply

    def start(name, *, configured=True, pair=False):
        group = groups["api" if name == "api" else "import-worker"]
        env = base | {
            "KF_OJ_DATABASE_URL": make_url(postgres_url)
            .set(username=roles[name])
            .render_as_string(hide_password=False),
            "KF_OJ_ENVIRONMENT": "test",
            "KF_OJ_ARTIFACT_ROOT": str(artifact),
            "KF_OJ_IMPORT_STAGING_ROOT": str(staging),
            "KF_OJ_JUDGE_ADMISSION": policy.model_dump_json(),
            "KF_OJ_BROWSER_ORIGINS": '["http://127.0.0.1:58000"]',
        }
        if not configured:
            env.pop("KF_OJ_IMPORT_STAGING_ROOT")
        if name == "worker":
            env["KF_OJ_IMPORT_WORKER_CGROUP"] = str(group)
        command = (
            [
                sys.executable,
                "-m",
                "uvicorn",
                "kf_oj.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "58000",
                "--no-proxy-headers",
                "--limit-concurrency",
                "4",
                "--backlog",
                "16",
            ]
            if name == "api"
            else [sys.executable, "-m", "kf_oj.workers.problem_import"]
        )
        if pair:
            assert name == "worker"
            # Trusted fixture composition; both coroutines call the actual ordinary Worker.
            code = """
import asyncio
from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine,build_session_factory
from kf_oj.infra.artifacts.store import ArtifactStore
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.modules.identity.service import ReservedMaintenance
from kf_oj.modules.operations.service import MaintenanceAudit
from kf_oj.modules.problems.import_worker import ImportWorker
async def run():
 s=Settings();e=build_engine(s);store=ArtifactStore(s.artifact_root)
 staging=ImportStaging(s.import_staging_root)
 try:
  w=ImportWorker(build_session_factory(e),staging,ArtifactRepository(store),
   ReservedMaintenance(),MaintenanceAudit(),s.judge_admission,s.import_worker_cgroup)
  results=await asyncio.gather(w.once(),w.once())
  assert sorted(results)==[False,True],results
  print('ACTUAL_CONCURRENT_IMPORT_CLAIM_ACCEPTED',flush=True)
 finally:
  staging.close();store.close();await e.dispose()
asyncio.run(run())
"""
            command = [sys.executable, "-c", code]
        log = (tmp_path / f"{name}.log").open("a")
        logs[name] = log
        processes[name] = subprocess.Popen(
            command, env=env, stdout=log, stderr=log, preexec_fn=enter(group)
        )

    def stop(name):
        p = processes.pop(name, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=10)
        if name in logs:
            logs.pop(name).close()

    async def run():
        c = await connect(postgres_url)
        try:
            for name, group in (("api", "kf_oj_platform"), ("worker", "kf_oj_problem_import")):
                await c.execute(
                    f"CREATE ROLE {roles[name]} LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                await c.execute(f"GRANT {group} TO {roles[name]}")
            for name, identifier in users.items():
                await c.execute(
                    "INSERT INTO accounts(id,username,password_hash) VALUES($1,$2,$3)",
                    identifier,
                    name,
                    hash_password(name, password),
                )
            await c.execute("SELECT * FROM kf_manage_account_authority('web_admin','bootstrap')")
            await c.execute("SELECT * FROM kf_manage_account_authority('web_backup','grant')")
            start("api")
            start("worker")
            async with httpx.AsyncClient(base_url="http://127.0.0.1:58000", timeout=45) as client:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    assert processes["api"].poll() is None, (tmp_path / "api.log").read_text()[
                        -2000:
                    ]
                    assert processes["worker"].poll() is None, (
                        tmp_path / "worker.log"
                    ).read_text()[-2000:]
                    try:
                        if (
                            await client.get("/api/v1/health/live")
                        ).status_code == 200 and await c.fetchval(
                            "SELECT worker_ready_until>clock_timestamp() "
                            "FROM problem_import_pool WHERE id=1"
                        ):
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("real service startup deadline")
                admin = "/api/v1/admin"
                imports = admin + "/problem-imports"
                assert (await client.get(admin + "/problems")).status_code == 401

                async def login(name):
                    client.cookies.clear()
                    r = await client.post(
                        "/api/v1/auth/login",
                        json={"username": name, "password": password},
                        headers={"Origin": "http://127.0.0.1:58000"},
                    )
                    assert r.status_code == 200, r.text
                    return {
                        "Origin": "http://127.0.0.1:58000",
                        "X-CSRF-Token": client.cookies["kf_csrf"],
                    }

                headers = await login("web_student")
                for path in (
                    "/problems",
                    "/maintenance-audit",
                    "/problem-maintenance/" + str(uuid4()),
                ):
                    assert (await client.get(admin + path)).status_code == 403
                headers = await login("web_admin")

                async def ready(slug, *, multi=False):
                    blob, payload = package(
                        policy, multi=multi, mutation=lambda m: m.update(slug=slug)
                    )
                    key = str(uuid4())
                    body = {
                        "operation_id": key,
                        "archive_digest": hashlib.sha256(blob).hexdigest(),
                        "archive_bytes": len(blob),
                    }
                    end = time.monotonic() + 20
                    while True:
                        r = await client.post(imports, json=body, headers=headers)
                        if r.status_code != 503 or time.monotonic() >= end:
                            break
                        await asyncio.sleep(0.2)
                    assert r.status_code == 201, r.text
                    end = time.monotonic() + 30
                    while True:
                        r = await client.put(
                            imports + "/" + key + "/archive",
                            content=blob,
                            headers=headers | {"Content-Type": "application/zip"},
                        )
                        if r.status_code != 503 or time.monotonic() >= end:
                            break
                        await asyncio.sleep(0.2)
                    assert r.status_code == 200, r.text
                    end = time.monotonic() + 120
                    while time.monotonic() < end:
                        r = await client.get(imports + "/" + key)
                        assert r.status_code == 200, r.text
                        if r.json()["state"] == "ready":
                            return r.json(), payload
                        assert r.json()["state"] != "failed", r.text
                        await asyncio.sleep(0.2)
                    raise AssertionError("candidate deadline")

                async def publish(candidate, expected=None, key=None):
                    body = {
                        "operation_id": str(key or uuid4()),
                        "import_operation_id": candidate["operation_id"],
                        "candidate_id": candidate["candidate_id"],
                        "expected_active_version_id": expected,
                    }
                    r = await client.post(
                        admin + "/problem-publications", json=body, headers=headers
                    )
                    return r, body

                first, payload = await ready("managed-synthetic")
                for invalid in ({}, headers | {"Origin": "https://invalid.example"}):
                    r = await client.post(
                        admin + "/problem-publications",
                        json={
                            "operation_id": str(uuid4()),
                            "import_operation_id": first["operation_id"],
                            "candidate_id": first["candidate_id"],
                            "expected_active_version_id": None,
                        },
                        headers=invalid,
                    )
                    assert r.status_code == 403
                r, body = await publish(first)
                assert r.status_code == 201, r.text
                receipt = r.json()
                pid = receipt["problem"]["id"]
                vid = receipt["published_version_id"]
                assert receipt["problem"]["revision"] == 1
                assert (
                    await client.post(admin + "/problem-publications", json=body, headers=headers)
                ).json() == receipt
                assert (
                    await client.get(admin + "/problem-maintenance/" + body["operation_id"])
                ).json() == receipt
                assert (
                    await client.post(
                        admin + "/problem-publications",
                        json=body | {"expected_active_version_id": vid},
                        headers=headers,
                    )
                ).status_code == 409
                assert (await client.get(imports + "/" + first["operation_id"])).json()[
                    "state"
                ] == "published"
                public = await client.get("/api/v1/problems/versions/" + vid)
                assert public.status_code == 200, public.text
                assert public.json()["statement"] == "Public statement only"
                assert "digest" not in public.text and "hidden" not in public.text
                old = await c.fetchrow("SELECT * FROM problem_versions WHERE id=$1", UUID(vid))
                second, _ = await ready("managed-synthetic")
                third, _ = await ready("managed-synthetic")
                b1 = {
                    "operation_id": str(uuid4()),
                    "import_operation_id": second["operation_id"],
                    "candidate_id": second["candidate_id"],
                    "expected_active_version_id": vid,
                }
                b2 = b1 | {
                    "operation_id": str(uuid4()),
                    "import_operation_id": third["operation_id"],
                    "candidate_id": third["candidate_id"],
                }
                pair = await asyncio.gather(
                    *(
                        client.post(admin + "/problem-publications", json=b, headers=headers)
                        for b in (b1, b2)
                    )
                )
                assert sorted(r.status_code for r in pair) == [201, 409], [
                    (r.status_code, r.text) for r in pair
                ]
                latest = next(r.json() for r in pair if r.status_code == 201)
                active = latest["published_version_id"]
                assert latest["problem"]["revision"] == 2
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM problem_versions WHERE problem_id=$1", UUID(pid)
                    )
                    == 2
                )
                assert dict(
                    await c.fetchrow("SELECT * FROM problem_versions WHERE id=$1", UUID(vid))
                ) == dict(old)
                # The local CLI uses the same publication/admission service; rejected
                # toolchains do not create rows, and successful v1/v2 keep exact bytes.
                local = tmp_path / "cli"
                local.mkdir(mode=0o700)
                for name, data in payload.items():
                    (local / name).write_bytes(data)
                env = base | {
                    "KF_OJ_DATABASE_URL": make_url(postgres_url)
                    .set(username=roles["api"])
                    .render_as_string(hide_password=False),
                    "KF_OJ_ARTIFACT_ROOT": str(artifact),
                    "KF_OJ_JUDGE_ADMISSION": policy.model_dump_json(),
                }
                cli = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "kf_oj.modules.problems.cli", str(local)],
                    env=env,
                    capture_output=True,
                    timeout=90,
                )
                assert cli.returncode == 0, cli.stderr.decode()[-1500:]
                active = str(
                    await c.fetchval(
                        "SELECT active_version_id FROM problems WHERE id=$1", UUID(pid)
                    )
                )
                assert (
                    await c.fetchval(
                        "SELECT max(revision) FROM problem_versions WHERE problem_id=$1", UUID(pid)
                    )
                    == 3
                )
                # Disable is preconditioned and idempotent; new admission and public
                # reads close, while immutable accepted history remains authorized.
                toggle = {
                    "operation_id": str(uuid4()),
                    "expected_active_version_id": active,
                    "expected_enabled": True,
                }
                r = await client.post(
                    admin + f"/problems/{pid}/disable", json=toggle, headers=headers
                )
                assert r.status_code == 200, r.text
                assert not r.json()["problem"]["enabled"]
                assert (
                    await client.post(
                        admin + f"/problems/{pid}/disable", json=toggle, headers=headers
                    )
                ).json() == r.json()
                assert (await client.get("/api/v1/problems/versions/" + vid)).status_code == 404
                assert not (await client.get("/api/v1/problems")).json()["items"]
                headers = await login("web_student")
                submission = {
                    "problem_version_id": active,
                    "language": "c17",
                    "source": "synthetic source, never executed",
                    "idempotency_key": str(uuid4()),
                }
                assert (
                    await client.post("/api/v1/submissions", json=submission, headers=headers)
                ).status_code == 404
                headers = await login("web_admin")
                cli = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "kf_oj.modules.problems.cli", str(local)],
                    env=env,
                    capture_output=True,
                    timeout=90,
                )
                assert cli.returncode != 0
                restore = {
                    "operation_id": str(uuid4()),
                    "expected_active_version_id": active,
                    "expected_enabled": False,
                }
                r = await client.post(
                    admin + f"/problems/{pid}/restore", json=restore, headers=headers
                )
                assert r.status_code == 200, r.text
                assert r.json()["problem"]["enabled"]
                history = (await client.get(admin + f"/problems/{pid}/versions?limit=1")).json()
                assert history["items"][0]["revision"] == 3 and history["next_before"] == 3
                history2 = (
                    await client.get(admin + f"/problems/{pid}/versions?limit=1&before=3")
                ).json()
                assert history2["items"][0]["revision"] == 2
                assert "statement" not in json.dumps(history) and "digest" not in json.dumps(
                    history
                )
                # Inject a transaction-local audit failure through a trusted test
                # DB trigger; no version/active pointer/import receipt survives rollback.
                rollback, _ = await ready("rollback-synthetic")
                await c.execute(
                    """CREATE FUNCTION reject_publish_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN IF NEW.action='published' THEN
                    RAISE EXCEPTION 'synthetic audit failure'; END IF; RETURN NEW; END $$"""
                )
                await c.execute(
                    "CREATE TRIGGER reject_publish_audit BEFORE INSERT "
                    "ON problem_maintenance_events "
                    "FOR EACH ROW EXECUTE FUNCTION reject_publish_audit()"
                )
                r, rb = await publish(rollback)
                assert r.status_code == 503, r.text
                assert not await c.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM problems WHERE slug='rollback-synthetic')"
                )
                assert not await c.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM problem_maintenance_operations WHERE id=$1)",
                    UUID(rb["operation_id"]),
                )
                assert (
                    await c.fetchval(
                        "SELECT state FROM problem_import_operations WHERE id=$1",
                        UUID(rollback["operation_id"]),
                    )
                    == "ready"
                )
                await c.execute("DROP TRIGGER reject_publish_audit ON problem_maintenance_events")
                await c.execute("DROP FUNCTION reject_publish_audit()")
                r = await client.post(admin + "/problem-publications", json=rb, headers=headers)
                assert r.status_code == 201, r.text
                multi, _ = await ready("managed-multi", multi=True)
                r, _ = await publish(multi)
                assert r.status_code == 201, r.text
                assert (
                    len(
                        (
                            await client.get(
                                "/api/v1/problems/versions/" + r.json()["published_version_id"]
                            )
                        ).json()["language_configurations"]
                    )
                    == 4
                )
                # Live foreign administrator cannot adopt another actor's candidate or receipt.
                headers = await login("web_backup")
                assert (
                    await client.get(admin + "/problem-maintenance/" + body["operation_id"])
                ).status_code == 404
                assert (
                    await client.post(admin + "/problem-publications", json=body, headers=headers)
                ).status_code == 404
                headers = await login("web_admin")
                audit = (await client.get(admin + "/maintenance-audit?limit=100")).json()["items"]
                assert any(
                    e["action"] == "authority_bootstrap"
                    and e["actor_kind"] == "operator"
                    and e["actor_id"] is None
                    for e in audit
                )
                assert (
                    sum(
                        e["operation_id"] == body["operation_id"] and e["action"] == "published"
                        for e in audit
                    )
                    == 1
                )
                assert "hidden" not in json.dumps(audit) and "password" not in json.dumps(audit)
                # Finite page traversal excludes duplicates and has precise committed SQL count.
                seen = set()
                cursor = None
                for _ in range(100):
                    r = await client.get(
                        admin + "/maintenance-audit",
                        params={"limit": 2} | ({"before": cursor} if cursor else {}),
                    )
                    assert r.status_code == 200, r.text
                    page = r.json()
                    ids = {e["id"] for e in page["items"]}
                    assert not seen & ids
                    seen |= ids
                    cursor = page["next_before"]
                    if cursor is None:
                        break
                else:
                    raise AssertionError("audit page bound")
                assert len(seen) == await c.fetchval(
                    "SELECT count(*) FROM problem_maintenance_events"
                )
                for role in ("kf_oj_platform", "kf_oj_problem_import", "kf_oj_coordinator"):
                    await c.execute("SET ROLE " + role)
                    for table in ("problem_maintenance_operations", "problem_maintenance_events"):
                        for command in (f"UPDATE {table} SET id=id", f"DELETE FROM {table}"):
                            with pytest.raises(Exception) as error:
                                await c.execute(command)
                            assert getattr(error.value, "sqlstate", None) == "42501"
                    await c.execute("RESET ROLE")
                with pytest.raises(Exception) as error:
                    await c.execute(
                        "UPDATE problem_versions SET title=title WHERE id=$1", UUID(vid)
                    )
                assert getattr(error.value, "sqlstate", None) == "23514"
                concurrent, _ = await ready("revocation-synthetic")
                revoke_body = {
                    "operation_id": str(uuid4()),
                    "import_operation_id": concurrent["operation_id"],
                    "candidate_id": concurrent["candidate_id"],
                    "expected_active_version_id": None,
                }
                published_result, _ = await asyncio.gather(
                    client.post(admin + "/problem-publications", json=revoke_body, headers=headers),
                    c.execute("SELECT * FROM kf_manage_account_authority('web_admin','revoke')"),
                )
                assert published_result.status_code in (201, 401, 403, 503), published_result.text
                committed = await c.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM problem_maintenance_operations WHERE id=$1)",
                    UUID(revoke_body["operation_id"]),
                )
                assert committed == (published_result.status_code == 201)
                assert (
                    await c.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM problems WHERE slug='revocation-synthetic')"
                    )
                    == committed
                )
                assert await c.fetchval(
                    "SELECT count(*) FROM problem_maintenance_events "
                    "WHERE operation_id=$1 AND action='published'",
                    UUID(revoke_body["operation_id"]),
                ) == int(committed)
                assert (await client.get(admin + "/problems")).status_code == 401
                assert (
                    await client.post(admin + "/problem-publications", json=body, headers=headers)
                ).status_code == 401
                print(
                    "ACTUAL_MAINTENANCE_CLI_V1_V2_UUID_CONCURRENCY_AUDIT_ROLLBACK_DISABLE_ACCEPTED",
                    flush=True,
                )
        finally:
            stop("api")
            stop("worker")
            for role in roles.values():
                await c.execute("DROP OWNED BY " + role)
                await c.execute("DROP ROLE " + role)
            await c.close()

    asyncio.run(run())
