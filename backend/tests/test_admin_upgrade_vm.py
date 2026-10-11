"""Approved baseline upgrade/retention: actual PostgreSQL in an independent capped VM."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from conftest import connect, migrate
from sqlalchemy.engine import make_url
from test_admin_import_vm import independent_import_vm as independent_import_vm
from test_admin_import_vm import package

from kf_oj.contracts.judging_v2 import AdmissionRegistry

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


def test_baseline_upgrade_preserves_existing_data(independent_import_vm, postgres_url, tmp_path):
    baseline = Path("/kf/baseline-platform")
    assert baseline.is_dir(), "curated approved baseline required"
    url = make_url(postgres_url).set(database="kf_oj_test_upgrade_" + uuid4().hex)
    rendered = url.render_as_string(hide_password=False)

    async def manage(command):
        connection = await connect(postgres_url)
        try:
            await connection.execute(command)
        finally:
            await connection.close()

    def old_run(args):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=baseline,
            env=os.environ
            | {
                "PYTHONPATH": "/kf/baseline-platform/src:/kf/site",
                "KF_OJ_DATABASE_URL": rendered,
                "KF_BASELINE_FIXTURES": str(tmp_path),
            },
            capture_output=True,
            timeout=300,
        )
        assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")[
            -2000:
        ]
        return result

    # Real old-schema data is created through the old public services. No student
    # source executes here; historical report fixtures are explicitly unmeasured.
    for multi in (False, True):
        directory = tmp_path / ("v2" if multi else "v1")
        directory.mkdir(mode=0o700)
        _, payload = package(policy(), multi=multi)
        for name, data in payload.items():
            (directory / name).write_bytes(data)
            (directory / name).chmod(0o600)
    seed = """
import asyncio,hashlib,json,os,secrets
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text,update
from kf_oj.api.app import create_app
from kf_oj.core.settings import Settings
from kf_oj.contracts.judging import ArtifactReference
from kf_oj.contracts.judging_v2 import AdmissionRegistry
from kf_oj.contracts.judging_versions import manifest_bytes
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.modules.submissions.models import Evaluation,JudgeAttempt
async def main():
 root=Path(os.environ['KF_BASELINE_FIXTURES'])
 fixture=Path('/kf/tests/fixtures/contracts-v2/judge-registry.valid.json')
 policy=AdmissionRegistry.model_validate_json(fixture.read_bytes())
 database=os.environ['KF_OJ_DATABASE_URL']
 app=create_app(Settings(_env_file=None,database_url=database,
                         artifact_root=root/'objects',judge_admission=policy))
 async with app.router.lifespan_context(app):
  password='Synthetic upgrade acceptance only 2026!'
  user=await app.state.identity.create_account('upgrade_student',password)
  for name in ('v1','v2'):
   version=await app.state.problems.import_directory(root/name)
   request=CreateSubmission(problem_version_id=version,language='c17',
                            idempotency_key=uuid4(),source='int main(void){return 0;}')
   created=await app.state.submissions.create(user,request)
   # Controlled, unmeasured historical database fixtures prove preservation of
   # reports and their digests. They never claim execution or trusted performance.
   async with app.state.sessions.begin() as session:
    projected=await app.state.problems.for_submission(session,version)
    source=ArtifactReference(digest=hashlib.sha256(request.source.encode()).hexdigest(),
                             bytes=len(request.source.encode()))
    manifest=projected.manifest(created.evaluation_id,created.submission_id,'c17',source)
    md=hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    aid,wid=uuid4(),uuid4()
    template=('contracts' if name=='v1' else 'contracts-v2')
    file=('judge-result.valid.json' if name=='v1' else 'judge-result.valid-c17.json')
    report=json.loads((Path('/kf/tests/fixtures')/template/file).read_text())
    report['binding']={'evaluation_id':str(created.evaluation_id),'attempt_id':str(aid),
                       'worker_id':str(wid),'fencing_version':1}
    report['manifest_digest']=md
    report['cases']=[{'case_id':str(case.case_id),'verdict':'accepted',
                      'cpu_time_us':100,'memory_peak_bytes':262144}
                     for case in manifest.cases]
    report['run_cpu_time_us']=100*len(manifest.cases)
    report['run_memory_peak_bytes']=262144
    digest=hashlib.sha256(json.dumps(report,sort_keys=True,separators=(',',':'),
                                    ensure_ascii=False).encode()).hexdigest()
    await session.execute(text('SET LOCAL ROLE kf_oj_coordinator'))
    session.add(JudgeAttempt(id=aid,evaluation_id=created.evaluation_id,worker_id=wid,
                number=1,fencing_version=1,lease_token_hash=hashlib.sha256(
                 secrets.token_hex(32).encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=10),
                manifest=manifest.model_dump(mode='json'),manifest_digest=md,
                report=report,report_digest=digest,cleanup_complete=True))
    await session.flush()
    await session.execute(update(Evaluation).where(Evaluation.id==created.evaluation_id)
                .values(current_attempt_id=aid,state='finished',attempts_count=1,
                        fencing_version=1,verdict='accepted',final_report=report,
                        final_report_digest=digest))
asyncio.run(main())
"""
    asyncio.run(manage(f'CREATE DATABASE "{url.database}"'))
    try:
        old_run(["-m", "alembic", "upgrade", "head"])
        old_run(["-c", seed])

        async def snapshot(columns=None):
            connection = await connect(rendered)
            try:
                if columns is None:
                    rows = await connection.fetch("""
                        SELECT table_name,column_name FROM information_schema.columns
                        WHERE table_schema='public' AND table_name<>'alembic_version'
                        ORDER BY table_name,ordinal_position
                    """)
                    columns = {}
                    for row in rows:
                        columns.setdefault(row["table_name"], []).append(row["column_name"])
                data = {}
                for table, names in columns.items():
                    query = (
                        "SELECT " + ",".join('"' + n + '"' for n in names) + ' FROM "' + table + '"'
                    )
                    records = sorted(
                        json.dumps(dict(r), sort_keys=True, default=str)
                        for r in await connection.fetch(query)
                    )
                    data[table] = {
                        "rows": len(records),
                        "sha256": hashlib.sha256("\n".join(records).encode()).hexdigest(),
                    }
                return columns, data
            finally:
                await connection.close()

        columns, before = asyncio.run(snapshot())
        assert before["accounts"]["rows"] == 1 and before["problem_versions"]["rows"] == 2
        assert before["submissions"]["rows"] == 2
        assert before["judge_attempts"]["rows"] == 2
        migrate(rendered, "upgrade", "head")
        _, after = asyncio.run(snapshot(columns))
        assert after == before
        migrate(rendered, "check")

        async def defaults():
            connection = await connect(rendered)
            try:
                account = await connection.fetchrow(
                    "SELECT is_administrator,authorization_version FROM accounts"
                )
                assert not account["is_administrator"] and account["authorization_version"] == 1
                assert await connection.fetchval("SELECT count(*) FROM evaluation_performance") == 0
                assert (
                    await connection.fetchval("SELECT count(*) FROM problem_maintenance_events")
                    == 0
                )
                for role in ("kf_oj_identity_operator", "kf_oj_problem_import"):
                    row = await connection.fetchrow("SELECT * FROM pg_roles WHERE rolname=$1", role)
                    assert not any(
                        row[k]
                        for k in (
                            "rolcanlogin",
                            "rolsuper",
                            "rolcreatedb",
                            "rolcreaterole",
                            "rolreplication",
                            "rolbypassrls",
                        )
                    )
            finally:
                await connection.close()

        asyncio.run(defaults())
        proof(
            "baseline_upgrade",
            {
                "baseline": "5df3c370aab0b12a1c3f8670a0c066f8b77a4078",
                "retained_tables": before,
                "old_v1_v2_manifests_and_source_bytes": "exact",
                "historical_report_retention_fixture_unmeasured": True,
                "fixture_reports_not_student_execution_or_performance_acceptance": True,
                "fresh_current_database": postgres_url.split("/")[-1].split("?")[0],
            },
        )
    finally:
        asyncio.run(manage(f'DROP DATABASE "{url.database}"'))
