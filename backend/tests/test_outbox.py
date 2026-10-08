"""Real private PostgreSQL/RabbitMQ control plane, no user program execution."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aio_pika
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_problems import import_fixture, platform_role

from kf_oj.api.app import create_app
from kf_oj.contracts.submissions import CreateSubmission
from kf_oj.core.settings import Settings
from kf_oj.infra.messaging.publisher import QUEUE, ConfirmedBroker, OutboxRepository


def test_actual_confirm_duplicate_lock_recovery_and_broker_failure(
    postgres_url, tmp_path, monkeypatch
):
    asyncio.run(outbox_acceptance(postgres_url, tmp_path, monkeypatch))


async def outbox_acceptance(postgres_url, tmp_path, monkeypatch):
    config = os.environ.get("KF_OJ_TEST_AMQP_CONFIG")
    if config is None:
        pytest.skip("private RabbitMQ control acceptance requires KF_OJ_TEST_AMQP_CONFIG")
    url = json.loads(Path(config).read_text())["amqp_url"]
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1" and parsed.port == 26572
    _, policy = import_fixture(tmp_path / "import")
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
    broker = ConfirmedBroker(url)
    connection = await aio_pika.connect(url, timeout=5)
    try:
        async with app.router.lifespan_context(app):
            owner = await app.state.identity.create_account(
                "outbox_test", secrets.token_urlsafe(24)
            )
            version = await app.state.problems.import_directory(tmp_path / "import")
            created = await app.state.submissions.create(
                owner,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="synthetic control event; not compiled",
                ),
            )
            repo = OutboxRepository(app.state.sessions)
            claimed = await asyncio.gather(repo.claim(), repo.claim())
            assert sum(v is not None for v in claimed) == 1
            item = next(v for v in claimed if v is not None)
            assert item[1] == created.evaluation_id
            # Kill a separate real publisher process after broker confirm, before DB mark.
            code = """import asyncio,json,sys,signal
from uuid import UUID
from pathlib import Path
from kf_oj.infra.messaging.publisher import ConfirmedBroker
async def main():
 item=tuple(UUID(x) for x in json.loads(sys.stdin.readline()))
 broker=ConfirmedBroker(json.loads(Path(sys.argv[1]).read_text())["amqp_url"])
 await broker.publish(item)
 print("CONFIRMED",flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
            child = subprocess.Popen(
                [sys.executable, "-c", code, config],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                child.stdin.write(json.dumps([str(x) for x in item]) + "\n")
                child.stdin.flush()
                assert (
                    await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10)
                    == "CONFIRMED\n"
                )
            finally:
                child.kill()
                await asyncio.to_thread(child.wait, 5)
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()
            assert child.returncode == -9
            channel = await connection.channel()
            queue = await channel.get_queue(QUEUE)
            first = await queue.get(timeout=5)
            assert json.loads(first.body) == {
                "schema_version": 1,
                "kind": "judge.evaluation",
                "evaluation_id": str(created.evaluation_id),
            }
            assert len(first.body) <= 512 and first.delivery_mode == 2
            # A publisher lost after confirm cannot mark DB; actual DB clock lock expires.
            assert await repo.claim() is None
            await asyncio.sleep(15.1)
            retry = await repo.claim()
            assert retry[:2] == item[:2] and retry[2] != item[2]
            assert not await repo.confirmed(item)
            await broker.publish(retry)
            assert await repo.confirmed(retry)
            second = await queue.get(timeout=5)
            assert second.body == first.body and second.message_id == first.message_id
            await first.ack()
            await second.ack()
            assert await repo.claim() is None
            async with app.state.sessions() as s:
                assert (
                    await s.scalar(
                        text("SELECT count(*) FROM outbox WHERE delivered_at IS NOT NULL")
                    )
                    == 1
                )
            # A second transaction succeeds even when a separate broker is unavailable.
            await app.state.submissions.create(
                owner,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="broker failure synthetic event",
                ),
            )
            failed = await repo.claim()
            unavailable = ConfirmedBroker(url.replace(":26572/", ":26579/"))
            try:
                with pytest.raises((ConnectionError, OSError)):
                    await unavailable.publish(failed)
            finally:
                await unavailable.close()
            await repo.failed(failed)
            assert await repo.claim() is None
            async with app.state.sessions() as s:
                row = (
                    await s.execute(
                        text("""SELECT failures,last_error_code,
                    next_attempt_at>clock_timestamp() deferred,delivered_at IS NULL pending,
                    lock_owner IS NULL unlocked FROM outbox WHERE id=:id"""),
                        {"id": failed[0]},
                    )
                ).one()
                assert tuple(row) == (1, "broker_unavailable", True, True, True)
            # Preserve the broker-failure row for inspection while testing another crash window.
            async with app.state.sessions.begin() as s:
                await s.execute(
                    text(
                        "UPDATE outbox SET next_attempt_at=clock_timestamp()+interval '1 hour' "
                        "WHERE id=:id"
                    ),
                    {"id": failed[0]},
                )
            before = await app.state.submissions.create(
                owner,
                CreateSubmission(
                    problem_version_id=version,
                    language="c17",
                    idempotency_key=uuid4(),
                    source="before-confirm synthetic event",
                ),
            )
            pending = await repo.claim()
            assert pending[1] == before.evaluation_id
            preconfirm = code.replace("await broker.publish(item)", "await broker.open()")
            child = subprocess.Popen(
                [sys.executable, "-c", preconfirm, config],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                child.stdin.write(json.dumps([str(x) for x in pending]) + "\n")
                child.stdin.flush()
                assert (
                    await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10)
                    == "CONFIRMED\n"
                )
            finally:
                child.kill()
                await asyncio.to_thread(child.wait, 5)
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()
            assert child.returncode == -9
            assert await queue.get(fail=False, timeout=5) is None
            await asyncio.sleep(15.1)
            retry = await repo.claim()
            assert retry[:2] == pending[:2]
            await broker.publish(retry)
            assert await repo.confirmed(retry)
            delivered = await queue.get(timeout=5)
            assert json.loads(delivered.body)["evaluation_id"] == str(before.evaluation_id)
            await delivered.ack()

    finally:
        await broker.close()
        await connection.close()
