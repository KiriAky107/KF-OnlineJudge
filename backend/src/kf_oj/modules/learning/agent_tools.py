"""Bind the learning tool handlers and their policy to the verified caller."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_learning import (
    SourceEvidenceArguments,
    SourceEvidenceResult,
    SubmissionHistoryArguments,
    SubmissionHistoryResult,
    TrainingCandidate,
    TrainingCandidatesArguments,
    TrainingCandidatesResult,
)
from kf_oj.contracts.submission_evidence import SubmissionCodeEvidence
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.learning.agent_tool_policy import (
    AgentToolGrants,
    AgentToolLimits,
    PostgresToolPolicy,
)
from kf_oj.modules.learning.public import (
    AGENT_TOOL_SCOPES,
    SOURCE_EVIDENCE,
    SUBMISSION_HISTORY,
    TRAINING_CANDIDATES,
)
from kf_oj.modules.learning.service import LearningService, evidence
from kf_oj.modules.submissions import public as records
from kf_oj.runtime.tools import Tool, ToolContext, ToolKey, ToolRegistry


@dataclass(frozen=True, slots=True)
class ScopedAgentTools:
    registry: ToolRegistry
    policy: PostgresToolPolicy

    async def invoke(self, context: ToolContext, key: ToolKey, arguments: bytes):
        return await self.registry.invoke(self.policy, context, key, arguments)

    async def restore(self, context: ToolContext, key: ToolKey, output: dict):
        return await self.registry.restore(self.policy, context, key, output)


def _scoped_agent_tools(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    identity: ActiveSessionPort,
    grants: AgentToolGrants,
    limits: AgentToolLimits,
    source_evidence: SubmissionCodeEvidence | None,
    as_of: datetime | None,
) -> ScopedAgentTools:
    service = LearningService(sessions)

    async def history(arguments: SubmissionHistoryArguments) -> SubmissionHistoryResult:
        cutoff = as_of or datetime.now(UTC)
        since = cutoff - timedelta(days=arguments.days)
        async with sessions() as db:
            owned = await records.learning_records(
                db,
                principal,
                since=since,
                as_of=cutoff,
                limit=arguments.limit,
                problem_ids=arguments.problem_ids,
                submission_id=arguments.submission_id,
                anchor_submission_id=arguments.anchor_submission_id,
            )
        return SubmissionHistoryResult(
            since=since,
            records=tuple(evidence(row) for row in owned.records),
            truncated=owned.truncated,
        )

    async def candidates(arguments: TrainingCandidatesArguments) -> TrainingCandidatesResult:
        result = await service.recommend(
            principal,
            arguments,
            allowed_difficulties=arguments.allowed_difficulties,
            exclude_problem_ids=arguments.exclude_problem_ids,
            as_of=as_of,
        )
        return TrainingCandidatesResult(
            items=tuple(
                TrainingCandidate(
                    problem_id=item.problem_id,
                    problem_version_id=item.problem_version_id,
                    title=item.title,
                    tags=item.tags,
                    difficulty=item.difficulty,
                    previously_accepted=item.previously_accepted,
                    feedback_evidence=item.feedback_evidence,
                    timing=item.timing,
                    prerequisites=item.prerequisites,
                )
                for item in result.items
            ),
            candidate_limit=result.candidate_limit,
            candidates_truncated=result.candidates_truncated,
            prerequisite_gap=result.prerequisite_gap,
            goal_coverage=result.goal_coverage,
        )

    async def anchored_code(_: SourceEvidenceArguments) -> SourceEvidenceResult:
        return SourceEvidenceResult(evidence=source_evidence)

    registry = ToolRegistry(
        (
            Tool(
                SOURCE_EVIDENCE,
                AGENT_TOOL_SCOPES[SOURCE_EVIDENCE],
                SourceEvidenceArguments,
                SourceEvidenceResult,
                anchored_code,
                read_only=True,
                timeout_seconds=2,
            ),
            Tool(
                SUBMISSION_HISTORY,
                AGENT_TOOL_SCOPES[SUBMISSION_HISTORY],
                SubmissionHistoryArguments,
                SubmissionHistoryResult,
                history,
                read_only=True,
                timeout_seconds=5,
                retry_on_timeout=True,
            ),
            Tool(
                TRAINING_CANDIDATES,
                AGENT_TOOL_SCOPES[TRAINING_CANDIDATES],
                TrainingCandidatesArguments,
                TrainingCandidatesResult,
                candidates,
                read_only=True,
                timeout_seconds=10,
                retry_on_timeout=True,
            ),
        )
    )
    return ScopedAgentTools(
        registry, PostgresToolPolicy(sessions, principal, identity, grants, limits)
    )
