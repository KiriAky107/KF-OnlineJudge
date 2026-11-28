"""Owner-scoped learning ports and business errors."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.submission_evidence import SubmissionCodeEvidence
from kf_oj.modules.identity.public import AcceptedSession, ActiveSessionPort, Principal
from kf_oj.modules.learning.agent_store import LearningQueueBusy as LearningQueueBusy
from kf_oj.modules.learning.agent_store import PostgresAgentStorage
from kf_oj.modules.learning.agent_tool_policy import AgentToolGrants as AgentToolGrants
from kf_oj.modules.learning.agent_tool_policy import AgentToolLimits as AgentToolLimits
from kf_oj.runtime.storage import AgentStorage
from kf_oj.runtime.tools import SOURCE_EVIDENCE as SOURCE_EVIDENCE
from kf_oj.runtime.tools import SUBMISSION_HISTORY as SUBMISSION_HISTORY
from kf_oj.runtime.tools import TRAINING_CANDIDATES as TRAINING_CANDIDATES

if TYPE_CHECKING:
    from kf_oj.modules.learning.agent_tools import ScopedAgentTools
    from kf_oj.modules.problems.public import PublicDiagnosticSample


AGENT_TOOL_SCOPES = MappingProxyType(
    {
        SOURCE_EVIDENCE: "submissions:own:source-evidence",
        SUBMISSION_HISTORY: "submissions:own:read",
        TRAINING_CANDIDATES: "training:candidates:read",
    }
)


def agent_storage(sessions: async_sessionmaker[AsyncSession], principal: Principal) -> AgentStorage:
    """Bind the storage port to the authenticated principal at composition time."""
    return PostgresAgentStorage(sessions, principal.account_id)


async def linked_public_sample_diagnostic(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    sample: PublicDiagnosticSample,
    language: str,
) -> bool:
    """Verify the original account, session and public sample without returning the diagnosis."""
    from sqlalchemy import select

    from .diagnostic_models import DiagnosticRequestRow as Diagnostic

    if principal.session_token_hash is None:
        return False
    proof = AcceptedSession.from_principal(principal)
    matched = await session.scalar(
        select(Diagnostic.id).where(
            Diagnostic.id == request_id,
            Diagnostic.owner_id == principal.account_id,
            Diagnostic.authorization_version == proof.authorization_version,
            Diagnostic.session_proof == proof.session_proof,
            Diagnostic.state == "observed",
            Diagnostic.mode.in_(("optimization", "ubsan")),
            Diagnostic.problem_version_id == sample.version_id,
            Diagnostic.sample_ordinal == sample.ordinal,
            Diagnostic.language == language,
            Diagnostic.input_digest == sha256(sample.input_bytes).hexdigest(),
            Diagnostic.expected_digest == sha256(sample.expected_bytes).hexdigest(),
        )
    )
    return matched is not None


class LearningUnavailable(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class NoCandidates(ValueError):
    pass


class TrainingSourceUnavailable(LookupError):
    pass


def scoped_agent_tools(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    identity: ActiveSessionPort,
    grants: AgentToolGrants,
    limits: AgentToolLimits | None = None,
    *,
    source_evidence: SubmissionCodeEvidence | None = None,
    as_of: datetime | None = None,
) -> ScopedAgentTools:
    from kf_oj.modules.learning.agent_tools import _scoped_agent_tools

    return _scoped_agent_tools(
        sessions, principal, identity, grants, limits or AgentToolLimits(), source_evidence, as_of
    )
