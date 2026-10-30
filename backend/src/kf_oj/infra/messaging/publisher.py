"""Finite transactional outbox delivery; confirmations are notifications, not grades."""

import asyncio
import logging
import signal
from uuid import uuid4

import aio_pika
from sqlalchemy import text

from kf_oj.contracts.judge import JudgeTaskNotification
from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.infra.messaging.outbox_queries import OutboxQueries
from kf_oj.infra.messaging.publisher_diagnostics import emit_failure

QUEUE = "kf.judge.evaluations.v1"
QUEUE_ARGUMENTS = {
    "x-max-length": 64,
    "x-max-length-bytes": 32768,
    "x-overflow": "reject-publish",
    "x-message-ttl": 600000,
}


class OutboxRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    async def status(self):
        return await OutboxQueries(self.sessions).status()

    async def claim(self):
        owner = uuid4()
        async with self.sessions.begin() as s:
            row = (
                await s.execute(
                    text("""WITH candidate AS (
                        SELECT id FROM outbox
                        WHERE delivered_at IS NULL AND next_attempt_at<=clock_timestamp()
                        AND (locked_until IS NULL OR locked_until<=clock_timestamp())
                        ORDER BY next_attempt_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                        UPDATE outbox o SET lock_owner=:owner,
                        locked_until=clock_timestamp()+interval '15 seconds'
                        FROM candidate c WHERE o.id=c.id RETURNING o.id,o.evaluation_id"""),
                    {"owner": owner},
                )
            ).first()
        return None if row is None else (row.id, row.evaluation_id, owner)

    async def confirmed(self, item):
        async with self.sessions.begin() as s:
            result = await s.execute(
                text("""UPDATE outbox SET delivered_at=clock_timestamp(),
                    locked_until=NULL,lock_owner=NULL,last_error_code=NULL
                    WHERE id=:id AND lock_owner=:owner AND locked_until>clock_timestamp()"""),
                {"id": item[0], "owner": item[2]},
            )
        return result.rowcount == 1

    async def failed(self, item):
        async with self.sessions.begin() as s:
            await s.execute(
                text("""UPDATE outbox SET failures=least(failures+1,100),
                    next_attempt_at=clock_timestamp()+
                    least(power(2,least(failures+1,6)),60)*interval '1 second',
                    locked_until=NULL,lock_owner=NULL,last_error_code='broker_unavailable'
                    WHERE id=:id AND lock_owner=:owner AND locked_until>clock_timestamp()"""),
                {"id": item[0], "owner": item[2]},
            )


class ConfirmedBroker:
    def __init__(self, url):
        # Parent disabled does not suppress descendant loggers; set their inherited level.
        for name in ("aio_pika", "aiormq", "pamqp"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self.url = url
        self.connection = None
        self.channel = None

    async def open(self):
        async with asyncio.timeout(10):
            await self._open()

    async def _open(self):
        # Use a finite connect, not a reconnect loop holding an outbox lock.
        self.connection = await aio_pika.connect(self.url, timeout=5, heartbeat=30)
        self.channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
        await self.channel.declare_queue(QUEUE, durable=True, arguments=QUEUE_ARGUMENTS, timeout=5)

    async def publish(self, item):
        # The complete operation ends before the database claim expires.
        async with asyncio.timeout(12):
            await self._publish(item)

    async def _publish(self, item):
        if self.channel is None or self.channel.is_closed:
            await self.open()
        body = (
            JudgeTaskNotification(schema_version=1, kind="judge.evaluation", evaluation_id=item[1])
            .model_dump_json()
            .encode()
        )
        assert len(body) <= 512
        confirmation = await self.channel.default_exchange.publish(
            aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(item[0]),
            ),
            routing_key=QUEUE,
            mandatory=True,
            timeout=5,
        )
        from pamqp.commands import Basic

        if not isinstance(confirmation, Basic.Ack):
            raise RuntimeError("notification not confirmed")

    async def close(self):
        if self.connection is not None and not self.connection.is_closed:
            try:
                await asyncio.wait_for(self.connection.close(), timeout=2)
            except Exception:
                pass
        self.connection = self.channel = None


async def run(settings: Settings, stop: asyncio.Event):
    _startup_phase = "configuration"
    if settings.amqp_url is None:
        raise ValueError("AMQP configuration required")
    engine = build_engine(settings)
    repository = OutboxRepository(build_session_factory(engine))
    broker = ConfirmedBroker(settings.amqp_url.get_secret_value())
    logger = logging.getLogger("kf_oj.outbox")
    # Client exceptions may include connection URIs; suppress transport-library logging.
    for name in ("aio_pika", "aiormq", "pamqp"):
        logging.getLogger(name).disabled = True
    try:
        _startup_phase = "database_connect"
        async with engine.connect() as c:
            _startup_phase = "database_authority"
            allowed = await c.scalar(
                text("""SELECT NOT r.rolsuper AND NOT r.rolbypassrls
                AND pg_has_role(current_user,'kf_oj_platform','MEMBER')
                AND NOT has_column_privilege(current_user,'evaluations','state','UPDATE')
                FROM pg_roles r WHERE r.rolname=current_user""")
            )
            if not allowed:
                raise ValueError("dedicated platform database role required")
        _startup_phase = "running"
        while not stop.is_set():
            try:
                item = await repository.claim()
                if item is not None:
                    try:
                        await broker.publish(item)
                        await repository.confirmed(item)
                    except Exception:
                        await repository.failed(item)
                        await broker.close()
                        logger.warning("outbox broker unavailable")
            except Exception:
                logger.warning("outbox database unavailable")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
    finally:
        await broker.close()
        await engine.dispose()


def main():
    async def entry():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(Settings(), stop)

    try:
        asyncio.run(entry())
    except Exception as error:
        try:
            emit_failure(error, run.__code__)
        except BaseException:
            pass  # Diagnostics must never replace the original fatal result.
        raise SystemExit("outbox unavailable") from None


if __name__ == "__main__":
    main()
