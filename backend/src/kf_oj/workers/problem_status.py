"""Ordinary import-only read status; no parser, staging mutation or judging writes."""

import asyncio
import json

from kf_oj.core.settings import Settings
from kf_oj.infra.db.session import build_engine, build_session_factory
from kf_oj.modules.problems.operations import ImportOperationQueries


async def run() -> dict:
    settings = Settings()
    if settings.judge_admission is None:
        raise ValueError("import admission required")
    engine = build_engine(settings)
    try:
        return await ImportOperationQueries(
            build_session_factory(engine), settings.judge_admission
        ).status()
    finally:
        await engine.dispose()


def main() -> None:
    try:
        result = asyncio.run(asyncio.wait_for(run(), timeout=6))
        print(json.dumps(result, sort_keys=True))
    except Exception:
        raise SystemExit("import status unavailable") from None


if __name__ == "__main__":
    main()
