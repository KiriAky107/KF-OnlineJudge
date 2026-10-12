"""Read only publisher-owned counts; never claim, publish or acknowledge a task."""

import asyncio
import json

from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.infra.messaging.publisher import OutboxRepository


async def run() -> dict:
    engine = build_engine(Settings())
    try:
        return await OutboxRepository(build_session_factory(engine)).status()
    finally:
        await engine.dispose()


def main() -> None:
    try:
        print(json.dumps(asyncio.run(asyncio.wait_for(run(), timeout=6)), sort_keys=True))
    except Exception:
        raise SystemExit("outbox status unavailable") from None


if __name__ == "__main__":
    main()
