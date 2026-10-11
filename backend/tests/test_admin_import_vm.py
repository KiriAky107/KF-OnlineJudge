"""Real HTTP/worker/storage and adverse ZIP cases: independent capped VM only."""

import asyncio
import hashlib
import io
import json
import os
import resource
import signal
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import connect
from sqlalchemy.engine import make_url

from kf_oj.contracts.judging import ArtifactReference
from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.infra.artifacts.archive_reader import ArchiveError, ArchiveReader
from kf_oj.infra.artifacts.staging import ImportStaging
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore
from kf_oj.modules.identity.passwords import hash_password

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def independent_import_vm():
    marker = Path("/kf/INDEPENDENT_VM")
    if not marker.exists() or marker.read_text().strip() != "isolated-import-test-guest":
        pytest.skip("adverse archive/full import acceptance requires the independent capped VM")
    assert [p.name for p in Path("/sys/class/net").iterdir()] == ["lo"]
    assert all(
        x not in Path("/proc/self/mountinfo").read_text() for x in ("9p", "virtiofs", "hgfs")
    )
    return {name: Path("/sys/fs/cgroup/kf-total") / name for name in ("api", "import-worker")}


def package(
    policy, *, multi=False, method=zipfile.ZIP_STORED, mutation=None, payload_mutation=None
):
    payload = {
        "statement.md": b"Public statement only",
        "hidden.in": b"synthetic-hidden-input\x00\xff",
        "hidden.out": b"synthetic-hidden-output\x00\xfe",
        "sample.in": b"1\n",
        "sample.out": b"2\n",
    }
    manifest = {
        "schema_version": 1,
        "slug": "web-synthetic",
        "title": "Public",
        "statement_file": "statement.md",
        "languages": ["c17", "cpp20"],
        "toolchain": policy.legacy.toolchain.model_dump(mode="json"),
        "comparison": "exact_bytes",
        "compile_limits": policy.legacy.compile_ceiling.model_dump(),
        "run_limits": policy.legacy.run_ceiling.model_dump(),
        "job_wall_time_us": 60000000,
        "cases": [{"key": "binary", "input_file": "hidden.in", "expected_file": "hidden.out"}],
        "samples": [{"input_file": "sample.in", "output_file": "sample.out"}],
    }
    if multi:
        for key in ("languages", "toolchain", "compile_limits", "run_limits", "job_wall_time_us"):
            del manifest[key]
        manifest["schema_version"] = 2
        manifest["language_configurations"] = [
            {
                key: json.loads(
                    (
                        ROOT / f"tests/fixtures/contracts-v2/judge-manifest.valid-{language}.json"
                    ).read_text()
                )[key]
                for key in (
                    "language",
                    "toolchain",
                    "compile_limits",
                    "run_limits",
                    "job_wall_time_us",
                )
            }
            for language in ("c17", "cpp20", "python313", "java21")
        ]
    if mutation:
        mutation(manifest)
    payload["problem.json"] = json.dumps(manifest).encode()
    if payload_mutation:
        payload_mutation(payload)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=method) as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    return out.getvalue(), payload


def test_bounded_zip_and_streaming_adoption(independent_import_vm, tmp_path):
    policy = AdmissionRegistry.model_validate_json(
        (ROOT / "tests/fixtures/contracts-v2/judge-registry.valid.json").read_bytes()
    )
    for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        blob, payload = package(
            policy,
            method=method,
            payload_mutation=lambda p: p.update({"hidden.in": b"A" * 1048576 + b"\x00\xff"}),
        )
        file = tmp_path / f"archive-{method}"
        file.write_bytes(blob)
        file.chmod(0o600)
        output = tmp_path / f"members-{method}"
        output.mkdir(mode=0o700)
        fd = os.open(file, os.O_RDONLY)
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            reader = ArchiveReader(fd, len(blob), deadline=time.monotonic() + 60)
            reader.verify_digest(hashlib.sha256(blob).hexdigest())
            assert reader.read("problem.json", 262144) == payload.pop("problem.json")
            references = reader.extract_remaining(directory)
            assert reader.unpacked_bytes == sum(len(v) for v in payload.values()) + len(
                package(policy, method=method)[1]["problem.json"]
            )
            store = ArtifactStore(tmp_path / f"objects-{method}")
            try:
                for name, (digest, size) in references.items():
                    assert (output / name).read_bytes() == payload[name]
                    source = os.open(name, os.O_RDONLY, dir_fd=directory)
                    try:
                        reference = ArtifactReference(digest=digest, bytes=size)
                        assert store.put_file(source, reference) == reference
                        assert store.put_file(source, reference) == reference
                    finally:
                        os.close(source)
            finally:
                store.close()
        finally:
            os.close(fd)
            os.close(directory)
    good, _ = package(policy)
    wrong_crc = bytearray(good)
    struct.pack_into("<I", wrong_crc, 14, 1)
    cases = [b"prefix" + good, good + b"trailing", bytes(wrong_crc)]
    for name in ("../escape", "bad/name", "Statement.md"):
        cases.append(package(policy, payload_mutation=lambda p, n=name: p.update({n: b"bad"}))[0])
    symlink = io.BytesIO()
    with zipfile.ZipFile(symlink, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        z.writestr(info, b"/outside")
    cases.append(symlink.getvalue())
    # Real damaged stored data and forged consistent local/central size/CRC/method/flags.
    corrupt = bytearray(good)
    name_bytes, extra_bytes = struct.unpack_from("<HH", corrupt, 26)
    corrupt[30 + name_bytes + extra_bytes] ^= 1
    cases.append(bytes(corrupt))
    central = good.index(b"PK\x01\x02")
    for local_offset, central_offset, value, fmt in (
        (6, 8, 1, "<H"),  # encryption
        (8, 10, 99, "<H"),  # unsupported method
        (22, 24, 67108865, "<I"),  # claimed size outside budget
    ):
        damaged = bytearray(good)
        struct.pack_into(fmt, damaged, local_offset, value)
        struct.pack_into(fmt, damaged, central + central_offset, value)
        cases.append(bytes(damaged))
    compressed, _ = package(policy, method=zipfile.ZIP_DEFLATED)
    forged = bytearray(compressed)
    central = compressed.index(b"PK\x01\x02")
    struct.pack_into("<I", forged, 22, 1)
    struct.pack_into("<I", forged, central + 24, 1)
    cases.append(bytes(forged))  # actual inflation exceeds the forged size
    for index, blob in enumerate(cases):
        file = tmp_path / f"bad-{index}"
        file.write_bytes(blob)
        file.chmod(0o600)
        fd = os.open(file, os.O_RDONLY)
        try:
            with pytest.raises(ArchiveError):
                reader = ArchiveReader(fd, len(blob), deadline=time.monotonic() + 60)
                # Unknown declaration references are rejected by the child, rather than
                # the raw ZIP decoder. Here invalid paths/types/CRC/header bytes are read.
                for member in reader.members:
                    reader.read(member.name, 64 * 1024**2)
        finally:
            os.close(fd)


def test_real_admin_upload_worker_candidate_faults_restart_and_cleanup(
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
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    assert processes["api"].poll() is None, (tmp_path / "api.log").read_text()[
                        -1500:
                    ]
                    assert processes["worker"].poll() is None, (
                        tmp_path / "worker.log"
                    ).read_text()[-1500:]
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
                    raise AssertionError(
                        "real API/worker startup deadline; api="
                        + (tmp_path / "api.log").read_text()[-1500:]
                        + " worker="
                        + (tmp_path / "worker.log").read_text()[-1500:]
                    )
                prefix = "/api/v1/admin/problem-imports"
                assert (await client.get(prefix + "/capabilities")).status_code == 401

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

                student_headers = await login("web_student")
                assert (await client.get(prefix + "/capabilities")).status_code == 403
                assert (
                    await client.post(
                        prefix,
                        json={
                            "operation_id": str(uuid4()),
                            "archive_digest": "a" * 64,
                            "archive_bytes": 100,
                        },
                        headers=student_headers,
                    )
                ).status_code == 403
                headers = await login("web_admin")
                assert (await client.get(prefix + "/capabilities")).json()[
                    "uploads_available"
                ] is True
                for invalid in (
                    {},
                    {"Origin": "http://127.0.0.1:58000", "X-CSRF-Token": "0" * 64},
                    headers | {"Origin": "https://evil.invalid"},
                    headers
                    | {"Origin": "https://evil.invalid", "X-Forwarded-Host": "127.0.0.1:58000"},
                ):
                    assert (
                        await client.post(
                            prefix,
                            json={
                                "operation_id": str(uuid4()),
                                "archive_digest": "a" * 64,
                                "archive_bytes": 100,
                            },
                            headers=invalid,
                        )
                    ).status_code == 403

                async def create(blob, key=None):
                    key = key or uuid4()
                    body = {
                        "operation_id": str(key),
                        "archive_digest": hashlib.sha256(blob).hexdigest(),
                        "archive_bytes": len(blob),
                    }
                    deadline = time.monotonic() + 15
                    while True:
                        r = await client.post(prefix, json=body, headers=headers)
                        if r.status_code != 503 or time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(0.2)
                    assert r.status_code == 201, r.text
                    assert r.headers["cache-control"] == "no-store"
                    assert (
                        await client.post(prefix, json=body, headers=headers)
                    ).json() == r.json()
                    assert (
                        await client.post(
                            prefix, json=body | {"archive_digest": "f" * 64}, headers=headers
                        )
                    ).status_code == 409
                    return str(key)

                async def put_blob(blob, key):
                    deadline = time.monotonic() + 30
                    while True:
                        result = await client.put(
                            f"{prefix}/{key}/archive",
                            content=blob,
                            headers=headers | {"Content-Type": "application/zip"},
                        )
                        if result.status_code != 503 or time.monotonic() >= deadline:
                            return result
                        await asyncio.sleep(0.2)

                async def discard_owned(key):
                    deadline = time.monotonic() + 15
                    while True:
                        result = await client.post(f"{prefix}/{key}/discard", headers=headers)
                        if result.status_code != 503 or time.monotonic() >= deadline:
                            return result
                        await asyncio.sleep(0.2)

                async def upload(blob, key):
                    r = await put_blob(blob, key)
                    assert r.status_code == 200, r.text
                    deadline = time.monotonic() + 100
                    while time.monotonic() < deadline:
                        r = await client.get(f"{prefix}/{key}")
                        assert r.status_code == 200, r.text
                        if r.json()["state"] in ("ready", "failed"):
                            return r.json()
                        assert processes["worker"].poll() is None, (
                            tmp_path / "worker.log"
                        ).read_text()[-2000:]
                        await asyncio.sleep(0.2)
                    raise AssertionError(
                        "candidate deadline; " + (tmp_path / "worker.log").read_text()[-2000:]
                    )

                async def wait_released(key, state, code=None, seconds=120):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        row = await c.fetchrow(
                            "SELECT state,failure_code,reserved_bytes,attempts FROM "
                            "problem_import_operations WHERE id=$1",
                            UUID(key),
                        )
                        if row["state"] == state and row["reserved_bytes"] == 0:
                            if code:
                                assert row["failure_code"] == code, dict(row)
                            return row
                        await asyncio.sleep(0.1)
                    raise AssertionError("operation cleanup deadline: " + str(dict(row)))

                async def restart_ready_worker():
                    previous = await c.fetchval(
                        "SELECT worker_ready_until FROM problem_import_pool WHERE id=1"
                    )
                    assert previous is not None
                    start("worker")
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        assert processes["worker"].poll() is None
                        if await c.fetchval(
                            "SELECT worker_ready_until>clock_timestamp()+interval '80 seconds' "
                            "AND worker_ready_until>$1 FROM problem_import_pool WHERE id=1",
                            previous,
                        ):
                            return
                        await asyncio.sleep(0.1)
                    raise AssertionError("fresh worker heartbeat deadline")

                async def raw_body(key, declared, data, *, delay=0, disconnect=False):
                    reader, writer = await asyncio.open_connection("127.0.0.1", 58000)
                    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                    request = (
                        f"PUT {prefix}/{key}/archive HTTP/1.1\r\n"
                        "Host: 127.0.0.1:58000\r\nOrigin: http://127.0.0.1:58000\r\n"
                        f"Cookie: {cookie}\r\nX-CSRF-Token: {headers['X-CSRF-Token']}\r\n"
                        f"Content-Type: application/zip\r\nContent-Length: {declared}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    try:
                        writer.write(request + data)
                        await writer.drain()
                        if delay:
                            await asyncio.sleep(delay)
                        if disconnect:
                            return None
                        return await asyncio.wait_for(reader.read(8192), 45)
                    finally:
                        writer.close()
                        await writer.wait_closed()

                for multi, method in ((False, zipfile.ZIP_STORED), (True, zipfile.ZIP_DEFLATED)):
                    blob, payload = package(policy, multi=multi, method=method)
                    key = await create(blob)
                    detail = await upload(blob, key)
                    assert detail["state"] == "ready", (detail["failure_code"], detail["state"])
                    print("ACTUAL_READY_IMPORT", multi, method, detail["candidate_id"], flush=True)
                    await wait_released(key, "ready")
                    retry = await put_blob(blob, key)
                    assert retry.status_code == 200
                    different = blob[:-1] + bytes([blob[-1] ^ 1])
                    conflict = await put_blob(different, key)
                    assert conflict.status_code == 409
                    assert (await client.get(f"{prefix}/{key}")).json()["candidate_id"] == detail[
                        "candidate_id"
                    ]
                    assert detail["candidate"]["judging_schema_version"] == (2 if multi else 1)
                    assert detail["candidate"]["case_count"] == 1
                    assert len(detail["candidate"]["languages"]) == (4 if multi else 2)
                    assert "synthetic-hidden" not in json.dumps(detail) and str(
                        tmp_path
                    ) not in json.dumps(detail)
                    # Actual database enforcement, not trigger inspection alone.
                    worker_db = await connect(
                        make_url(postgres_url)
                        .set(username=roles["worker"])
                        .render_as_string(hide_password=False)
                    )
                    try:
                        from asyncpg import CheckViolationError, InsufficientPrivilegeError

                        with pytest.raises(InsufficientPrivilegeError):
                            await worker_db.execute(
                                "UPDATE problem_candidates SET preview=preview WHERE id=$1",
                                UUID(detail["candidate_id"]),
                            )
                        with pytest.raises(CheckViolationError):
                            await worker_db.execute(
                                "DELETE FROM problem_candidates WHERE id=$1",
                                UUID(detail["candidate_id"]),
                            )
                        with pytest.raises(CheckViolationError):
                            await worker_db.execute(
                                """INSERT INTO problem_candidate_cases
                              (candidate_id,position,key,input_digest,input_bytes,expected_digest,expected_bytes)
                              SELECT candidate_id,1,'late',input_digest,input_bytes,
                              expected_digest,expected_bytes
                              FROM problem_candidate_cases WHERE candidate_id=$1""",
                                UUID(detail["candidate_id"]),
                            )
                    finally:
                        await worker_db.close()
                    refs = await c.fetch(
                        "SELECT input_digest,input_bytes,expected_digest,expected_bytes "
                        "FROM problem_candidate_cases WHERE candidate_id=$1",
                        UUID(detail["candidate_id"]),
                    )
                    assert len(refs) == 1
                    for field, name in (("input", "hidden.in"), ("expected", "hidden.out")):
                        digest = refs[0][field + "_digest"]
                        data = (artifact / digest).read_bytes()
                        assert data == payload[name] and hashlib.sha256(data).hexdigest() == digest
                    assert await c.fetchval("SELECT count(*) FROM problem_versions") == 0
                    assert (await client.get("/api/v1/problems")).json()["items"] == []
                    assert (
                        await c.fetchval(
                            "SELECT reserved_bytes FROM problem_import_pool WHERE id=1"
                        )
                        == 0
                    )
                    assert sorted(p.name for p in staging.iterdir()) == [".lock"]

                def reuse_public_member(payload):
                    declaration = json.loads(payload["problem.json"])
                    declaration["samples"][0]["output_file"] = "sample.in"
                    payload["problem.json"] = json.dumps(declaration).encode()
                    del payload["sample.out"]

                # Actual child consumes malformed data but never executes contents.
                for mutation in (
                    lambda p: p.update({"../escape": b"bad"}),
                    lambda p: p.update({"unknown.bin": b"bad"}),
                    lambda p: p.update({"hidden.in": b"PK\x03\x04" + b"bad"}),
                    lambda p: p.update({"sample.in": p["hidden.in"]}),
                    reuse_public_member,
                ):
                    blob, _ = package(policy, payload_mutation=mutation)
                    key = await create(blob)
                    result = await upload(blob, key)
                    # Publicly declared samples can happen to equal hidden bytes; declarations,
                    # not byte equality, decide visibility. This last finite case is legal.
                    if b"PK\x03\x04bad" in blob or b"../escape" in blob or b"unknown.bin" in blob:
                        assert result["state"] == "failed" and result["candidate"] is None, result
                    else:
                        assert result["state"] == "ready", result
                    await wait_released(key, result["state"])
                    assert (
                        await c.fetchval(
                            "SELECT reserved_bytes FROM problem_import_pool WHERE id=1"
                        )
                        == 0
                    )
                blob, _ = package(policy)
                key = await create(blob)
                stop("worker")
                occupied = ImportStaging(staging)
                try:
                    with occupied.exclusive():
                        mismatch = await client.put(
                            f"{prefix}/{key}/archive",
                            content=blob[:-1],
                            headers=headers | {"Content-Type": "application/zip"},
                        )
                        assert mismatch.status_code == 409
                finally:
                    occupied.close()
                assert (await discard_owned(key)).status_code == 200
                # Restart after receipt before consumption preserves the immutable operation.
                stop("worker")
                key = await create(blob)
                r = await client.put(
                    f"{prefix}/{key}/archive",
                    content=blob,
                    headers=headers | {"Content-Type": "application/zip"},
                )
                assert r.status_code == 200 and r.json()["state"] == "uploaded"
                start("worker")
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    detail = (await client.get(f"{prefix}/{key}")).json()
                    if detail["state"] in ("ready", "failed"):
                        break
                    await asyncio.sleep(0.2)
                assert detail["state"] == "ready", (detail["failure_code"], detail["state"])
                assert (
                    await c.fetchval(
                        "SELECT attempts FROM problem_import_operations WHERE id=$1",
                        UUID(key),
                    )
                    == 1
                )
                await wait_released(key, "ready")
                # A second real administrator cannot inspect, upload or discard another's candidate.
                headers = await login("web_backup")
                stop("worker")
                occupied = ImportStaging(staging)
                try:
                    with occupied.exclusive():
                        assert (await client.get(f"{prefix}/{key}")).status_code == 404
                        assert (
                            await client.post(f"{prefix}/{key}/discard", headers=headers)
                        ).status_code == 404
                        assert (
                            await client.put(
                                f"{prefix}/{key}/archive",
                                content=blob,
                                headers=headers | {"Content-Type": "application/zip"},
                            )
                        ).status_code == 404
                finally:
                    occupied.close()
                    start("worker")
                headers = await login("web_admin")
                # Incomplete TCP body and actual 30-second streaming deadline free reservations.
                interrupted = await create(blob)
                stop("worker")  # Exercise the upload path without a competing GC root lock.
                await raw_body(interrupted, len(blob), blob[:8], disconnect=True)
                await wait_released(interrupted, "failed", "upload_interrupted")
                await restart_ready_worker()
                slow = await create(blob)
                stop("worker")
                response = await raw_body(slow, len(blob), blob[:8], delay=33)
                await wait_released(slow, "failed", "upload_deadline")
                assert response.startswith(b"HTTP/1.1 408"), response[:200]
                await restart_ready_worker()
                # A full received body with the wrong SHA is never acknowledged as an upload.
                wrong = await create(blob)
                stop("worker")
                r = await client.put(
                    f"{prefix}/{wrong}/archive",
                    content=blob[:-1] + b"!",
                    headers=headers | {"Content-Type": "application/zip"},
                )
                assert r.status_code == 409
                await wait_released(wrong, "failed", "digest_mismatch")
                await restart_ready_worker()
                # Two actual reservations fill the global staging pool with Worker stopped.
                stop("worker")
                queued = []
                for _ in range(2):
                    queued_key = await create(blob)
                    r = await client.put(
                        f"{prefix}/{queued_key}/archive",
                        content=blob,
                        headers=headers | {"Content-Type": "application/zip"},
                    )
                    assert r.status_code == 200
                    queued.append(queued_key)
                assert (
                    await c.fetchval("SELECT reserved_bytes FROM problem_import_pool WHERE id=1")
                    == 268435456
                )
                r = await client.post(
                    prefix,
                    json={
                        "operation_id": str(uuid4()),
                        "archive_digest": hashlib.sha256(blob).hexdigest(),
                        "archive_bytes": len(blob),
                    },
                    headers=headers,
                )
                assert r.status_code == 503
                for queued_key in queued:
                    assert (
                        await client.post(f"{prefix}/{queued_key}/discard", headers=headers)
                    ).status_code == 200
                assert (
                    await c.fetchval("SELECT reserved_bytes FROM problem_import_pool WHERE id=1")
                    == 0
                )
                start("worker")
                # Kill parent AND parser in their already verified cgroup while a lease is held.
                # This finite injection tests both retries. No uploaded code is executed.
                interrupted_parser = await create(blob)
                r = await put_blob(blob, interrupted_parser)
                assert r.status_code == 200
                for expected_attempt in (1, 2):
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        row = await c.fetchrow(
                            "SELECT state,attempts FROM problem_import_operations WHERE id=$1",
                            UUID(interrupted_parser),
                        )
                        parser = []
                        for pid in (
                            groups["import-worker"].joinpath("cgroup.procs").read_text().split()
                        ):
                            cmdline = Path(f"/proc/{pid}/cmdline")
                            if (
                                cmdline.exists()
                                and b"kf_oj.workers._archive_child" in cmdline.read_bytes()
                            ):
                                parser.append(int(pid))
                        if (
                            row["state"] == "validating"
                            and row["attempts"] == expected_attempt
                            and parser
                        ):
                            # Freeze parser, kill parent, then parser. A live child keeps the flock.
                            for pid in parser:
                                os.kill(pid, signal.SIGSTOP)
                            processes["worker"].kill()
                            processes["worker"].wait(timeout=10)
                            await c.execute(
                                "UPDATE problem_import_operations SET lease_until="
                                "clock_timestamp()-interval '1 second' WHERE id=$1",
                                UUID(interrupted_parser),
                            )
                            # A live stopped child retains the real kernel lock after parent death.
                            probe = ImportStaging(staging)
                            try:
                                with pytest.raises(ArtifactError):
                                    with probe.exclusive():
                                        pass
                            finally:
                                probe.close()
                            denied = await client.post(
                                prefix,
                                json={
                                    "operation_id": str(uuid4()),
                                    "archive_digest": hashlib.sha256(blob).hexdigest(),
                                    "archive_bytes": len(blob),
                                },
                                headers=headers,
                            )
                            assert denied.status_code == 503
                            for pid in parser:
                                os.kill(pid, signal.SIGKILL)
                            stop("worker")
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("claimed parser injection deadline")
                    # Advance the lease under the VM test operator; requests cannot set clocks.
                    await c.execute(
                        "UPDATE problem_import_operations SET lease_until="
                        "clock_timestamp()-interval '1 second' WHERE id=$1",
                        UUID(interrupted_parser),
                    )
                    start("worker")
                row = await wait_released(interrupted_parser, "failed", "validation_unavailable")
                assert row["attempts"] == 2
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM problem_candidates WHERE operation_id=$1",
                        UUID(interrupted_parser),
                    )
                    == 0
                )
                # Real concurrent claims share the same capped process and use separate lock FDs.
                stop("worker")
                paired_key = await create(blob)
                r = await client.put(
                    f"{prefix}/{paired_key}/archive",
                    content=blob,
                    headers=headers | {"Content-Type": "application/zip"},
                )
                assert r.status_code == 200 and r.json()["state"] == "uploaded"
                start("worker", pair=True)
                row = await wait_released(paired_key, "ready")
                assert row["attempts"] == 1
                assert (
                    await c.fetchval(
                        "SELECT count(*) FROM problem_candidates WHERE operation_id=$1",
                        UUID(paired_key),
                    )
                    == 1
                )
                deadline = time.monotonic() + 120
                while processes["worker"].poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                assert processes["worker"].returncode == 0, (tmp_path / "worker.log").read_text()[
                    -1500:
                ]
                assert (
                    "ACTUAL_CONCURRENT_IMPORT_CLAIM_ACCEPTED"
                    in (tmp_path / "worker.log").read_text()
                )
                stop("worker")
                start("worker")
                # Insert an old receipt with the test operator; immutable timestamps stay unchanged.
                expired_id = uuid4()
                async with c.transaction():
                    await c.execute(
                        """INSERT INTO problem_import_operations
                  (id,owner_id,authorization_version,archive_digest,archive_bytes,created_at,
                   updated_at,expires_at,lease_until,attempts,fence,reserved_bytes)
                  SELECT $1,$2,authorization_version,$3,$4,
                  clock_timestamp()-interval '2 days',
                  clock_timestamp()-interval '2 days',clock_timestamp()-interval '1 day',
                  clock_timestamp()-interval '1 day',0,0,134217728 FROM accounts WHERE id=$2""",
                        expired_id,
                        users["web_admin"],
                        hashlib.sha256(blob).hexdigest(),
                        len(blob),
                    )
                    assert (
                        await c.fetchval(
                            "SELECT state FROM problem_import_operations WHERE id=$1", expired_id
                        )
                        == "uploading"
                    )
                    await c.execute(
                        "UPDATE problem_import_pool SET reserved_bytes="
                        "reserved_bytes+134217728 WHERE id=1"
                    )
                await wait_released(str(expired_id), "expired", "expired")
                print(
                    "ACTUAL_IMPORT_DISCONNECT_DEADLINE_CAPACITY_TWO_RETRIES_EXPIRY_ACCEPTED",
                    flush=True,
                )
                # Revocation is live and old cookies cannot read or write management APIs.
                await c.execute("SELECT * FROM kf_manage_account_authority('web_admin','revoke')")
                assert (await client.get(prefix + "/capabilities")).status_code == 401
                assert (
                    await client.post(f"{prefix}/{key}/discard", headers=headers)
                ).status_code == 401
                await c.execute("SELECT * FROM kf_manage_account_authority('web_admin','grant')")
                headers = await login("web_admin")
                stop("worker")
                discard_after = await c.fetchval("SELECT clock_timestamp()")
                page = (await client.get(prefix, params={"limit": 100})).json()
                for entry in page["items"]:
                    assert (
                        await client.post(
                            f"{prefix}/{entry['operation_id']}/discard", headers=headers
                        )
                    ).status_code == 200
                await restart_ready_worker()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and await c.fetchval(
                    "SELECT count(*) FROM problem_candidates"
                ):
                    await asyncio.sleep(0.1)
                assert await c.fetchval("SELECT count(*) FROM problem_candidates") == 0
                assert await c.fetchval("SELECT count(*) FROM problem_candidate_cases") == 0
                assert (
                    await c.fetchval("SELECT reserved_bytes FROM problem_import_pool WHERE id=1")
                    == 0
                )
                assert await c.fetchval("SELECT count(*) FROM problem_maintenance_events") >= 10
                assert (
                    await c.fetchval(
                        "SELECT bool_and(e.authorization_version=a.authorization_version) "
                        "FROM problem_maintenance_events e JOIN accounts a ON a.id=e.actor_id "
                        "WHERE e.action='import_discarded' AND e.actor_id=$1 "
                        "AND e.created_at >= $2",
                        users["web_admin"],
                        discard_after,
                    )
                    is True
                )
                for table in (
                    "evaluations",
                    "judge_attempts",
                    "evaluation_performance",
                    "accounts",
                    "sessions",
                ):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert not await c.fetchval(
                            "SELECT has_table_privilege($1,$2,$3)",
                            roles["worker"],
                            table,
                            privilege,
                        )
                assert not await c.fetchval(
                    "SELECT has_table_privilege($1,'problem_maintenance_events','UPDATE')",
                    roles["api"],
                )
                for privilege in ("SELECT", "UPDATE", "DELETE"):
                    assert not await c.fetchval(
                        "SELECT has_table_privilege($1,'problem_maintenance_events',$2)",
                        roles["worker"],
                        privilege,
                    )
                # Missing configuration is observed through the real server, never preview data.
                stop("api")
                start("api", configured=False)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    try:
                        r = await client.get(prefix + "/capabilities")
                        if r.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.1)
                assert r.status_code == 200 and not r.json()["uploads_available"]
                assert not r.json()["validation_available"]
                assert (
                    await client.post(
                        prefix,
                        json={
                            "operation_id": str(uuid4()),
                            "archive_digest": hashlib.sha256(blob).hexdigest(),
                            "archive_bytes": len(blob),
                        },
                        headers=headers,
                    )
                ).status_code == 503
                print("ACTUAL_IMPORT_HTTP_WORKER_ACCEPTED", flush=True)
        finally:
            stop("api")
            stop("worker")
            for role in roles.values():
                await c.execute(f"DROP OWNED BY {role}")
                await c.execute(f"DROP ROLE {role}")
            await c.close()

    asyncio.run(run())
