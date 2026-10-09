"""Actual Rust HTTP + PostgreSQL state tests, using explicitly synthetic reports.

No program is compiled/run by this host suite. Root stop proofs and lease-loss
execution are separately required in the independent VM (F05/F06).
"""

import asyncio
import json
import secrets
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_problems import import_fixture, platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def fixture(
    postgres_url, tmp_path, monkeypatch, *, import_builder=import_fixture, node_policy=None
):
    _, policy = import_builder(tmp_path / "import")
    engine = create_async_engine(postgres_url)
    platform_role(engine)
    monkeypatch.setattr("kf_oj.api.app.build_engine", lambda _: engine)
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=postgres_url,
            artifact_root=tmp_path / "artifacts",
            judge_admission=policy,
        )
    )
    url = make_url(postgres_url)
    c = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    role = "jp_coord_" + uuid4().hex
    password = secrets.token_hex(32)
    await c.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    await c.execute(f"GRANT kf_oj_coordinator TO {role}")
    coord_url = url.set(drivername="postgresql", username=role, password=password).render_as_string(
        hide_password=False
    )
    # Only loopback control-plane service is started; finite actual systemd scope.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    workers = [dict(id=str(uuid4()), token=secrets.token_hex(32)) for _ in range(2)]
    platform = secrets.token_hex(32)
    config = tmp_path / "coordinator.json"
    config.write_text(
        json.dumps(
            dict(
                listen=f"127.0.0.1:{port}",
                database_url=coord_url,
                workers=workers,
                platform_token=platform,
                admission=(node_policy or policy).model_dump(mode="json"),
                lease_seconds=5,
                recovery_seconds=1,
            )
        )
    )
    config.chmod(0o600)
    unit = "kf-jp-coord-" + uuid4().hex
    binary = ROOT / "judge/target/debug/kf-coordinator"
    assert binary.is_file(), "build actual Coordinator before this acceptance"
    subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={unit}",
            "--collect",
            "--service-type=exec",
            "--property=MemoryMax=128M",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=50%",
            "--property=TasksMax=32",
            "--property=RuntimeMaxSec=180",
            "--property=NoNewPrivileges=yes",
            str(binary),
            "--config",
            str(config),
        ],
        check=True,
        capture_output=True,
    )
    try:
        async with app.router.lifespan_context(app):
            owner = await app.state.identity.create_account(
                "coordinator_fixture", secrets.token_urlsafe(24)
            )
            version = await app.state.problems.import_directory(tmp_path / "import")
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
                for _ in range(100):
                    try:
                        if (await client.get("/health/ready")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("actual Coordinator not ready")
                props = subprocess.check_output(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        unit,
                        "--property=ControlGroup,MemoryMax,MemorySwapMax,TasksMax,CPUQuotaPerSecUSec,RuntimeMaxUSec,MainPID",
                    ],
                    text=True,
                )
                values = dict(line.split("=", 1) for line in props.splitlines())
                group = Path("/sys/fs/cgroup") / values["ControlGroup"].lstrip("/")
                assert {
                    n: (group / n).read_text().strip()
                    for n in ["memory.max", "memory.swap.max", "pids.max", "cpu.max"]
                } == {
                    "memory.max": "134217728",
                    "memory.swap.max": "0",
                    "pids.max": "32",
                    "cpu.max": "50000 100000",
                }

                async def create(language="c17"):
                    return await app.state.submissions.create(
                        owner,
                        CreateSubmission(
                            problem_version_id=version,
                            language=language,
                            idempotency_key=uuid4(),
                            source="synthetic report fixture; never compiled on host",
                        ),
                    )

                client.fixture_app = app
                client.fixture_account = owner
                client.fixture_version = version
                client.coordinator_unit = unit
                client.coordinator_role = role
                yield client, c, workers, platform, create
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], check=True, capture_output=True)
        assert (
            subprocess.run(
                ["systemctl", "--user", "is-active", unit], capture_output=True
            ).returncode
            != 0
        )
        await c.execute(f"DROP ROLE {role}")
        await c.close()


def notice(eid):
    return dict(schema_version=1, kind="judge.evaluation", evaluation_id=str(eid))


def worker_headers(worker, grant=None):
    headers = {"Authorization": "Bearer " + worker["token"]}
    if grant:
        headers["X-Lease-Token"] = grant["lease_token"]
    return headers


async def claim(client, created, worker):
    r = await client.post(
        "/internal/claim", json=notice(created.evaluation_id), headers=worker_headers(worker)
    )
    assert r.status_code == 200, r.text
    return r.json()


async def manifest(c, g):
    v = await c.fetchval(
        "SELECT manifest FROM judge_attempts WHERE id=$1", UUID(g["binding"]["attempt_id"])
    )
    return json.loads(v)


async def stages(client, worker, g):
    h = worker_headers(worker, g)
    for seq, stage, index in [(1, "compiling", None), (2, "running", 0), (3, "running", 1)]:
        r = await client.post(
            "/internal/stage",
            headers=h,
            json=dict(
                schema_version=1,
                kind="judge.stage",
                binding=g["binding"],
                sequence=seq,
                stage=stage,
                case_index=index,
            ),
        )
        assert r.status_code == 204, r.text


def synthetic_report(g, m, verdict="accepted"):
    cases = [
        dict(case_id=c["case_id"], verdict=verdict, cpu_time_us=100, memory_peak_bytes=4096)
        for c in m["cases"]
    ]
    return dict(
        schema_version=1,
        kind="judge.result",
        binding=g["binding"],
        manifest_digest=g["manifest_digest"],
        compilation=dict(
            status="succeeded",
            program=dict(digest="d" * 64, bytes=4096),
            diagnostics="",
            diagnostics_truncated=False,
            cpu_time_us=1000,
            memory_peak_bytes=16384,
        ),
        cases=cases,
        verdict=verdict,
        cleanup_complete=True,
        failure_code=None,
        run_cpu_time_us=len(cases) * 100,
        run_memory_peak_bytes=4096,
    )


def test_actual_auth_claim_competition_stage_fence_and_report(postgres_url, tmp_path, monkeypatch):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (
            client,
            c,
            workers,
            platform,
            create,
        ):
            created = await create()
            n = notice(created.evaluation_id)
            assert (await client.post("/internal/claim", json=n)).status_code == 401
            assert (
                await client.post(
                    "/internal/claim", json=n, headers={"Authorization": "Bearer " + platform}
                )
            ).status_code == 409
            r = await client.post(
                "/internal/claim",
                content=b"x" * 262145,
                headers=worker_headers(workers[0]) | {"Content-Type": "application/json"},
            )
            assert r.status_code == 413
            outcomes = await asyncio.gather(
                *(
                    client.post("/internal/claim", json=n, headers=worker_headers(w))
                    for w in workers
                )
            )
            assert sorted(r.status_code for r in outcomes) == [200, 204]
            g = next(r.json() for r in outcomes if r.status_code == 200)
            w = next(w for w in workers if w["id"] == g["binding"]["worker_id"])
            other = next(w2 for w2 in workers if w2 != w)
            assert await c.fetchval("SELECT count(*) FROM judge_attempts") == 1
            assert (
                await client.post(
                    "/internal/renew", json=g["binding"], headers=worker_headers(other, g)
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/internal/renew",
                    json=g["binding"] | {"fencing_version": 2},
                    headers=worker_headers(w, g),
                )
            ).status_code == 409
            bad = dict(
                schema_version=1,
                kind="judge.stage",
                binding=g["binding"],
                sequence=1,
                stage="running",
                case_index=0,
            )
            assert (
                await client.post("/internal/stage", json=bad, headers=worker_headers(w, g))
            ).status_code == 409
            await stages(client, w, g)
            progress = await client.post(
                "/internal/progress", json=g["binding"], headers=worker_headers(w, g)
            )
            assert progress.status_code == 200
            assert progress.json() == {
                "state": "running",
                "sequence": 3,
                "stage": "running",
                "case_index": 1,
            }
            subprocess.run(
                ["systemctl", "--user", "restart", client.coordinator_unit],
                check=True,
                capture_output=True,
            )
            progress = await client.post(
                "/internal/progress", json=g["binding"], headers=worker_headers(w, g)
            )
            assert progress.status_code == 200 and progress.json()["sequence"] == 3

            assert (
                await client.post("/internal/stage", json=bad, headers=worker_headers(w, g))
            ).status_code == 409
            assert (
                await client.post(
                    "/internal/renew", json=g["binding"], headers=worker_headers(w, g)
                )
            ).status_code == 200
            m = await manifest(c, g)
            report = synthetic_report(g, m)
            wrong = report | {"cases": list(reversed(report["cases"]))}
            assert (
                await client.post("/internal/report", json=wrong, headers=worker_headers(w, g))
            ).status_code == 422
            assert await c.fetchval("SELECT count(*) FROM case_results") == 0
            good = await client.post("/internal/report", json=report, headers=worker_headers(w, g))
            assert good.status_code == 204, good.text
            assert (
                await client.post("/internal/report", json=report, headers=worker_headers(w, g))
            ).status_code == 204
            assert (
                await client.post(
                    "/internal/report",
                    json=synthetic_report(g, m, "wrong_answer"),
                    headers=worker_headers(w, g),
                )
            ).status_code == 409
            assert (
                await c.fetchval("SELECT state FROM evaluations WHERE id=$1", created.evaluation_id)
                == "finished"
            )
            assert await c.fetchval("SELECT count(*) FROM case_results") == 2
            assert (
                await client.post("/internal/claim", json=n, headers=worker_headers(w))
            ).status_code == 204

            queued = await create()
            await c.execute(
                "UPDATE outbox SET delivered_at=clock_timestamp()-interval '31 seconds' "
                "WHERE evaluation_id=$1",
                queued.evaluation_id,
            )
            for _ in range(60):
                if await c.fetchval(
                    "SELECT delivered_at IS NULL FROM outbox WHERE evaluation_id=$1",
                    queued.evaluation_id,
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("missing notification not compensated")
            assert (
                await c.fetchval("SELECT state FROM evaluations WHERE id=$1", queued.evaluation_id)
                == "queued"
            )
            await claim(client, queued, w)
            await c.execute(f"ALTER ROLE {client.coordinator_role} NOLOGIN")
            assert (await client.get("/health/ready")).status_code == 503
            assert (await client.get("/health/live")).status_code == 200
            await c.execute(f"ALTER ROLE {client.coordinator_role} LOGIN")

    asyncio.run(run())


def test_actual_report_transaction_rollback(postgres_url, tmp_path, monkeypatch):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (client, c, workers, _, create):
            created = await create()
            w = workers[0]
            g = await claim(client, created, w)
            await stages(client, w, g)
            m = await manifest(c, g)
            await c.execute("""CREATE FUNCTION test_case_reject() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN IF NEW.position=1 THEN
                    RAISE EXCEPTION 'synthetic rollback';
                END IF; RETURN NEW; END $$""")
            await c.execute(
                "CREATE TRIGGER test_case_reject BEFORE INSERT ON case_results "
                "FOR EACH ROW EXECUTE FUNCTION test_case_reject()"
            )
            r = await client.post(
                "/internal/report", json=synthetic_report(g, m), headers=worker_headers(w, g)
            )
            assert r.status_code == 503, r.text
            assert await c.fetchval("SELECT count(*) FROM case_results") == 0
            assert await c.fetchval("SELECT report FROM judge_attempts") is None
            assert await c.fetchval("SELECT state FROM evaluations") == "running"
            await c.execute("DROP TRIGGER test_case_reject ON case_results")
            await c.execute("DROP FUNCTION test_case_reject()")
            assert (
                await client.post(
                    "/internal/report", json=synthetic_report(g, m), headers=worker_headers(w, g)
                )
            ).status_code == 204

    asyncio.run(run())


def test_actual_expiry_stopping_quarantine_retry_and_old_attempt(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (client, c, workers, _, create):
            created = await create()
            other = await create()
            w = workers[0]
            g = await claim(client, created, w)
            # Expired server-side lease (not a client clock claim) causes actual recovery scan.
            await c.execute(
                "UPDATE judge_attempts SET expires_at=clock_timestamp()-interval '1 second'"
            )
            assert (
                await client.post(
                    "/internal/renew", json=g["binding"], headers=worker_headers(w, g)
                )
            ).status_code == 409
            for _ in range(40):
                pending = (
                    await client.get("/internal/pending-stops", headers=worker_headers(w))
                ).json()
                if pending:
                    break
                await asyncio.sleep(0.05)
            assert pending[0]["binding"] == g["binding"]
            assert pending == [dict(binding=g["binding"], case_count=2)]
            stopped = dict(
                schema_version=1, kind="judge.stopped", binding=g["binding"], cleanup_complete=False
            )
            assert (
                await client.post("/internal/stopped", json=stopped, headers=worker_headers(w))
            ).status_code == 204
            assert (
                await c.fetchval(
                    "SELECT failure_code FROM evaluations WHERE id=$1", created.evaluation_id
                )
                == "cleanup_failed"
            )
            assert (
                await client.post(
                    "/internal/claim", json=notice(other.evaluation_id), headers=worker_headers(w)
                )
            ).status_code == 429
            stopped["cleanup_complete"] = True
            # Synthetic stop assertion only tests DB state. Actual Root proof is required in VM.
            assert (
                await client.post("/internal/stopped", json=stopped, headers=worker_headers(w))
            ).status_code == 204
            assert (
                await client.post("/internal/stopped", json=stopped, headers=worker_headers(w))
            ).status_code == 204
            assert (
                await c.fetchval("SELECT state FROM evaluations WHERE id=$1", created.evaluation_id)
                == "queued"
            )
            await c.execute(
                "UPDATE evaluations SET not_before=clock_timestamp() WHERE id=$1",
                created.evaluation_id,
            )
            fresh = await claim(client, created, w)
            assert fresh["attempt_number"] == 2 and fresh["binding"]["fencing_version"] == 2
            assert (
                await client.post(
                    "/internal/stage",
                    json=dict(
                        schema_version=1,
                        kind="judge.stage",
                        binding=g["binding"],
                        sequence=1,
                        stage="compiling",
                        case_index=None,
                    ),
                    headers=worker_headers(w, g),
                )
            ).status_code == 409
            assert (
                await client.post("/internal/stopped", json=stopped, headers=worker_headers(w))
            ).status_code == 409

    asyncio.run(run())


def test_actual_user_cancel_all_stages_and_bounded_infrastructure_retry(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        async with fixture(postgres_url, tmp_path, monkeypatch) as (
            client,
            c,
            workers,
            platform,
            create,
        ):
            w = workers[0]
            for stage in ["queued", "compiling", "running"]:
                created = await create()
                g = None
                if stage != "queued":
                    g = await claim(client, created, w)
                    r = dict(
                        schema_version=1,
                        kind="judge.stage",
                        binding=g["binding"],
                        sequence=1,
                        stage="compiling",
                        case_index=None,
                    )
                    assert (
                        await client.post("/internal/stage", json=r, headers=worker_headers(w, g))
                    ).status_code == 204
                    if stage == "running":
                        r |= dict(sequence=2, stage="running", case_index=0)
                        assert (
                            await client.post(
                                "/internal/stage", json=r, headers=worker_headers(w, g)
                            )
                        ).status_code == 204
                assert (
                    await client.post(
                        "/internal/cancel",
                        json=notice(created.evaluation_id),
                        headers={"Authorization": "Bearer " + platform},
                    )
                ).status_code == 204
                if g:
                    assert (
                        await c.fetchval(
                            "SELECT state FROM evaluations WHERE id=$1", created.evaluation_id
                        )
                        == "cancelling"
                    )
                    assert (
                        await client.post(
                            "/internal/renew", json=g["binding"], headers=worker_headers(w, g)
                        )
                    ).status_code == 409
                    r = dict(
                        schema_version=1,
                        kind="judge.stopped",
                        binding=g["binding"],
                        cleanup_complete=True,
                    )
                    assert (
                        await client.post("/internal/stopped", json=r, headers=worker_headers(w))
                    ).status_code == 204
                assert (
                    await c.fetchval(
                        "SELECT state FROM evaluations WHERE id=$1", created.evaluation_id
                    )
                    == "cancelled"
                )
            created = await create()
            for number in range(1, 4):
                g = await claim(client, created, w)
                m = await manifest(c, g)
                report = synthetic_report(g, m) | dict(
                    compilation=dict(
                        status="system_error",
                        program=None,
                        diagnostics="",
                        diagnostics_truncated=False,
                        cpu_time_us=None,
                        memory_peak_bytes=None,
                    ),
                    cases=[],
                    verdict="system_error",
                    failure_code="sandbox_unavailable",
                    run_cpu_time_us=None,
                    run_memory_peak_bytes=None,
                )
                assert (
                    await client.post("/internal/report", json=report, headers=worker_headers(w, g))
                ).status_code == 204
                state = await c.fetchval(
                    "SELECT state FROM evaluations WHERE id=$1", created.evaluation_id
                )
                assert state == ("failed" if number == 3 else "queued")
                if state == "queued":
                    await c.execute(
                        "UPDATE evaluations SET not_before=clock_timestamp() WHERE id=$1",
                        created.evaluation_id,
                    )
            assert (
                await client.post(
                    "/internal/claim", json=notice(created.evaluation_id), headers=worker_headers(w)
                )
            ).status_code == 204
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM judge_attempts WHERE evaluation_id=$1",
                    created.evaluation_id,
                )
                == 3
            )

    asyncio.run(run())
