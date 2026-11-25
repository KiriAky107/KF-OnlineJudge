"""Process one claimed learning job at a time."""

import asyncio
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.modules.identity.public import Principal, accepted_session_access
from kf_oj.modules.learning.agent_application import AgentLearning
from kf_oj.modules.learning.agent_model_budget import DEFAULT_MODEL_LIMITS, AgentModelLimits
from kf_oj.modules.learning.agent_queue import AgentQueue
from kf_oj.modules.learning.hint_policy import DEFAULT_HINT_POLICY, HintPolicy
from kf_oj.modules.skills.public import load_builtin_skills
from kf_oj.runtime.model import LearningHintModel
from kf_oj.runtime.storage import AgentRecordConflict


class LearningWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: LearningHintModel | None = None,
        *,
        model_limits: AgentModelLimits = DEFAULT_MODEL_LIMITS,
        hint_policy: HintPolicy = DEFAULT_HINT_POLICY,
    ) -> None:
        self._sessions = sessions
        self._model = model
        self._model_limits = model_limits
        self._hint_policy = hint_policy
        self._queue = AgentQueue(sessions)
        self._catalog = load_builtin_skills()

    async def run_once(self) -> bool:
        lease = await self._queue.claim()
        if lease is None:
            return False
        identity = accepted_session_access(lease.owner_id, lease.receipt)
        actor = Principal(
            lease.owner_id, "", authorization_version=lease.receipt.authorization_version
        )
        learning = AgentLearning(
            self._sessions,
            identity,
            self._catalog,
            model=self._model,
            model_limits=self._model_limits,
            hint_policy=self._hint_policy,
            claim=lease.run,
        )
        running = asyncio.create_task(
            learning.execute_claimed(actor, lease.run.run_id, lease.run.revision)
        )
        try:
            while True:
                finished, _ = await asyncio.wait({running}, timeout=5)
                if finished:
                    await running
                    return True
                if not await self._queue.renew(lease):
                    running.cancel()
                    with suppress(asyncio.CancelledError, AgentRecordConflict):
                        await running
                    await self._queue.acknowledge_cancelled(lease)
                    return True
        except AgentRecordConflict:
            await self._queue.acknowledge_cancelled(lease)
            return True
        finally:
            if not running.done():
                running.cancel()
                with suppress(asyncio.CancelledError, AgentRecordConflict):
                    await running
