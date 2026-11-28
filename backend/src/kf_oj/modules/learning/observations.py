"""Counts over observed diagnostics, with explicit evaluated samples and outcomes."""

from datetime import timedelta

from sqlalchemy import BigInteger, and_, case, cast, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.learning_observations import (
    DiagnosticCohort,
    DiagnosticMethod,
    DiagnosticObservationCount,
    DiagnosticObservations,
    PracticeAcceptanceCount,
    PracticeCohort,
    PracticeObservations,
    SelfTestCohort,
    SelfTestComparisonCount,
    TrainingDraftCount,
    TrainingDraftPurpose,
)
from kf_oj.modules.identity.public import AdministrationPort, Principal
from kf_oj.modules.submissions.public import (
    public_self_test_comparison_totals,
    training_acceptance_totals,
)

from .agent_models import (
    AgentPlanLinkRow,
    AgentRunRow,
    AgentSessionRow,
    AgentTimingRow,
    AgentWorkflowRow,
)
from .diagnostic_models import DiagnosticRequestRow as Diagnostic
from .models import LearningPlan, LearningPlanItem

_METHODS: tuple[DiagnosticMethod, ...] = ("optimization", "ubsan", "differential", "boundary")
_PURPOSES: tuple[TrainingDraftPurpose, ...] = ("focused_training", "periodic_review")
_PRACTICE_COHORTS: tuple[PracticeCohort, ...] = ("direct", "assistant_saved", "shared")
_SELF_TEST_COHORTS: tuple[tuple[bool, SelfTestCohort], ...] = (
    (False, "direct_sample"),
    (True, "diagnostic_sample"),
)
_FOLLOW_UP = timedelta(days=7)


async def _diagnostic_counts(session: AsyncSession, start, end, cohort: DiagnosticCohort):
    method = case((Diagnostic.mode == "batch", Diagnostic.batch_purpose), else_=Diagnostic.mode)
    tracked = Diagnostic.execution_profile.is_not(None)
    differential = (
        "accepted",
        "wrong_answer",
        "reduction_verified",
        "reduction_unverified",
    )
    boundary = ("completed", "runtime_error", "resource_limit")
    eligible = and_(
        tracked,
        or_(
            and_(
                method == "optimization",
                or_(Diagnostic.o2 == "accepted", Diagnostic.o0.is_not(None)),
            ),
            and_(method == "ubsan", Diagnostic.ubsan["verdict"].astext != "compilation_error"),
            and_(method == "differential", Diagnostic.batch_outcome.in_(differential)),
            and_(method == "boundary", Diagnostic.batch_outcome.in_(boundary)),
        ),
    )
    finding = or_(
        and_(method == "optimization", Diagnostic.o2 != Diagnostic.o0),
        and_(method == "ubsan", Diagnostic.ubsan["finding"].astext.is_not(None)),
        and_(
            method == "differential",
            Diagnostic.batch_outcome.in_(("wrong_answer", "reduction_verified")),
        ),
        and_(method == "boundary", Diagnostic.batch_outcome == "resource_limit"),
    )
    elapsed_ms = (
        func.extract("epoch", Diagnostic.finished_at - Diagnostic.execution_started_at) * 1000
    )
    query = (
        select(
            method.label("method"),
            func.count().label("observed_count"),
            func.count().filter(~tracked).label("untracked_count"),
            func.count().filter(eligible).label("eligible_count"),
            func.count().filter(eligible, finding).label("finding_count"),
            func.avg(elapsed_ms)
            .filter(eligible, Diagnostic.finished_at >= Diagnostic.execution_started_at)
            .label("mean_claim_to_finish_ms"),
            func.max(
                cast(Diagnostic.execution_profile["sandbox_wall_budget_us"].astext, BigInteger)
            )
            .filter(eligible)
            .label("max_sandbox_budget_us"),
        )
        .where(
            Diagnostic.state == "observed",
            Diagnostic.finished_at >= start,
            Diagnostic.finished_at < end,
        )
        .group_by(method)
    )
    if cohort != "all":
        query = query.where(
            Diagnostic.review_context.is_not(None)
            if cohort == "linked_review"
            else Diagnostic.review_context.is_(None)
        )
    return {row.method: row for row in await session.execute(query)}


async def _draft_counts(session: AsyncSession, start, end):
    items = AgentWorkflowRow.result["items"]
    count = case((func.jsonb_typeof(items) == "array", func.jsonb_array_length(items)), else_=0)
    rows = await session.execute(
        select(
            AgentSessionRow.purpose,
            func.count(),
            func.count(AgentPlanLinkRow.plan_id).filter(AgentPlanLinkRow.created_at < end),
        )
        .select_from(AgentRunRow)
        .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
        .join(AgentTimingRow, AgentTimingRow.run_id == AgentRunRow.id)
        .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
        .outerjoin(AgentPlanLinkRow, AgentPlanLinkRow.run_id == AgentRunRow.id)
        .where(
            AgentRunRow.state == "succeeded",
            AgentSessionRow.purpose.in_(_PURPOSES),
            AgentWorkflowRow.result["purpose"].astext == AgentSessionRow.purpose,
            AgentWorkflowRow.result["training_set"]["search"].astext.is_distinct_from("true"),
            count.between(1, 10),
            AgentTimingRow.finished_at >= start,
            AgentTimingRow.finished_at < end,
        )
        .group_by(AgentSessionRow.purpose)
    )
    return {purpose: (drafts, saved) for purpose, drafts, saved in rows}


def _practice_items(start, end):
    source = case(
        (AgentPlanLinkRow.run_id.is_not(None), "assistant_saved"),
        (LearningPlan.publication_id.is_not(None), "shared"),
        else_="direct",
    )
    return (
        select(
            LearningPlan.owner_id.label("account_id"),
            LearningPlanItem.version_id,
            LearningPlan.created_at.label("since"),
            (LearningPlan.created_at + _FOLLOW_UP).label("until"),
            source.label("source"),
        )
        .select_from(LearningPlan)
        .join(LearningPlanItem, LearningPlanItem.plan_id == LearningPlan.id)
        .outerjoin(AgentPlanLinkRow, AgentPlanLinkRow.plan_id == LearningPlan.id)
        .where(
            LearningPlan.created_at >= start - _FOLLOW_UP,
            LearningPlan.created_at < end - _FOLLOW_UP,
        )
        .ext(distinct_on(LearningPlan.owner_id, LearningPlanItem.version_id))
        .order_by(
            LearningPlan.owner_id,
            LearningPlanItem.version_id,
            LearningPlan.created_at,
            LearningPlan.id,
        )
        .subquery()
    )


class LearningObservations:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], identity: AdministrationPort):
        self.sessions = sessions
        self.identity = identity

    async def diagnostics(
        self, principal: Principal, days: int, cohort: DiagnosticCohort
    ) -> DiagnosticObservations:
        async with self.sessions.begin() as session:
            await self.identity.require_administrator(session, principal)
            end = await session.scalar(select(func.clock_timestamp()))
            start = end - timedelta(days=days)
            counts = await _diagnostic_counts(session, start, end, cohort)
        items = []
        for method in _METHODS:
            row = counts.get(method)
            items.append(
                DiagnosticObservationCount(
                    method=method,
                    observed_count=row.observed_count if row else 0,
                    untracked_count=row.untracked_count if row else 0,
                    eligible_count=row.eligible_count if row else 0,
                    finding_count=row.finding_count if row else 0,
                    mean_claim_to_finish_ms=round(row.mean_claim_to_finish_ms)
                    if row and row.mean_claim_to_finish_ms is not None
                    else None,
                    max_sandbox_budget_us=row.max_sandbox_budget_us if row else None,
                )
            )
        return DiagnosticObservations(start_at=start, end_at=end, cohort=cohort, items=tuple(items))

    async def practice(self, principal: Principal, days: int) -> PracticeObservations:
        async with self.sessions.begin() as session:
            await self.identity.require_administrator(session, principal)
            end = await session.scalar(select(func.clock_timestamp()))
            start = end - timedelta(days=days)
            drafts = await _draft_counts(session, start, end)
            counts = await training_acceptance_totals(session, _practice_items(start, end))
            comparisons = await public_self_test_comparison_totals(session, start, end)

        draft_entries = []
        for purpose in _PURPOSES:
            draft_count, saved_count = drafts.get(purpose, (0, 0))
            draft_entries.append(
                TrainingDraftCount(
                    purpose=purpose, draft_count=draft_count, saved_count=saved_count
                )
            )
        practice = []
        for cohort in _PRACTICE_COHORTS:
            exercise_count, attempted_count, accepted_count = counts.get(cohort, (0, 0, 0))
            practice.append(
                PracticeAcceptanceCount(
                    cohort=cohort,
                    exercise_count=exercise_count,
                    attempted_count=attempted_count,
                    accepted_count=accepted_count,
                )
            )
        self_tests = []
        for linked, cohort in _SELF_TEST_COHORTS:
            completed, compared, different = comparisons.get(linked, (0, 0, 0))
            self_tests.append(
                SelfTestComparisonCount(
                    cohort=cohort,
                    completed_count=completed,
                    compared_count=compared,
                    mismatch_count=different,
                )
            )
        return PracticeObservations(
            start_at=start,
            end_at=end,
            follow_up_days=_FOLLOW_UP.days,
            drafts=tuple(draft_entries),
            practice=tuple(practice),
            self_tests=tuple(self_tests),
        )
