"""Persistence port scoped by the caller to an authenticated account."""

from typing import Protocol
from uuid import UUID

from kf_oj.contracts.agent_runtime import AgentPurpose, AgentRun, AgentSession, AgentStep, RunState


class AgentRecordUnavailable(LookupError):
    """Missing and foreign records share the same outcome."""


class AgentRecordConflict(ValueError):
    """The request disagrees with a stored record or its current revision."""


class AgentStorage(Protocol):
    async def create_session(self, purpose: AgentPurpose) -> AgentSession: ...

    async def get_session(self, session_id: UUID) -> AgentSession: ...

    async def enqueue(
        self,
        session_id: UUID,
        *,
        request_id: UUID,
        skill_version_id: UUID,
        configuration_digest: str,
        input_digest: str,
    ) -> AgentRun: ...

    async def get_run(self, run_id: UUID) -> AgentRun: ...

    async def transition(self, run_id: UUID, revision: int, following: RunState) -> AgentRun: ...

    async def get_step(self, run_id: UUID, sequence: int) -> AgentStep: ...

    async def record_step(self, step: AgentStep, run_revision: int) -> AgentStep: ...
