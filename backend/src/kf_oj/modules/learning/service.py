"""Bounded evidence, deterministic recommendations, and owner-scoped plans."""

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text

from kf_oj.contracts.agent_learning import (
    FocusedTrainingParameters,
    FocusedTrainingResult,
    PeriodicReviewParameters,
    PeriodicReviewResult,
)
from kf_oj.contracts.learning import (
    PRACTICE_REASONS,
    CreateTraining,
    CreateTrainingFollowUp,
    Difficulty,
    FocusedTrainingSchedule,
    LearningEvidence,
    LearningOverview,
    LearningParameters,
    LearningRecommendations,
    PeriodicReviewSchedule,
    PrerequisiteGap,
    RecommendedProblem,
    StoredTrainingParameters,
    TagStatistic,
    TrainingDetail,
    TrainingFeedback,
    TrainingItem,
    TrainingPage,
    TrainingRankingProfile,
    TrainingSummary,
    TrainingTiming,
    feedback_priority,
    focused_exercise_count,
    practice_priority,
)
from kf_oj.modules.problems import public as problems
from kf_oj.modules.submissions import public as records

from .agent_models import AgentPlanLinkRow
from .coverage import choose_for_coverage, goal_coverage, matches_goals
from .feedback import matching_feedback, recent_feedback
from .feedback import reason as feedback_reason
from .models import LearningPlan, LearningPlanItem, TrainingFeedbackRow
from .practice import recent_plan_dates, training_timing
from .prerequisites import accepted_prerequisite_evidence
from .public import (
    IdempotencyConflict,
    LearningUnavailable,
    NoCandidates,
    TrainingSourceUnavailable,
)

RULE_VERSION = "learning-rules-v1"
AGENT_PLAN_RULE_VERSION = "agent-rules-v1"
FEEDBACK_RULE_VERSION = "learning-rules-v2"
AGENT_FEEDBACK_RULE_VERSION = "agent-rules-v2"
TIMING_RULE_VERSION = "learning-rules-v3"
AGENT_TIMING_RULE_VERSION = "agent-rules-v3"
PREREQUISITE_RULE_VERSION = "learning-rules-v4"
AGENT_PREREQUISITE_RULE_VERSION = "agent-rules-v4"
COVERAGE_RULE_VERSION = "learning-rules-v5"
AGENT_COVERAGE_RULE_VERSION = "agent-rules-v5"
WEIGHTED_RULE_VERSION = "learning-rules-v6"
AGENT_WEIGHTED_RULE_VERSION = "agent-rules-v6"
# Weights correspond to timing, difficulty self-assessment and unresolved submissions.
RANKING_WEIGHTS: dict[TrainingRankingProfile, tuple[int, int, int]] = {
    "feedback_first": (2, 5, 1),
    "unresolved_first": (2, 1, 5),
}
ORDINARY = frozenset(
    (
        "accepted",
        "wrong_answer",
        "compilation_error",
        "runtime_error",
        "time_limit_exceeded",
        "memory_limit_exceeded",
        "output_limit_exceeded",
    )
)


def evidence(record):
    return LearningEvidence(
        submission_id=record.submission_id,
        evaluation_id=record.evaluation_id,
        problem_id=record.problem_id,
        problem_version_id=record.version_id,
        submitted_at=record.created_at,
        verdict=record.verdict,
        state=record.state,
    )


def ordinary(record):
    return record.state == "finished" and record.verdict in ORDINARY and record.failure_code is None


def summarize(history, versions, now):
    # Submission is the student attempt unit, never infrastructure attempt rows.
    unique = {r.submission_id: r for r in history.records}
    rows = sorted(unique.values(), key=lambda r: (r.created_at, str(r.submission_id)), reverse=True)
    samples = [r for r in rows if ordinary(r)]
    accepted = [r for r in samples if r.verdict == "accepted"]
    tags = sorted(
        {t for r in samples if r.version_id in versions for t in versions[r.version_id].tags}
    )
    return LearningOverview(
        generated_at=now,
        rule_version=RULE_VERSION,
        since=now - timedelta(days=90),
        limit=500,
        truncated=history.truncated,
        earliest_record_at=min((r.created_at for r in rows), default=None),
        submission_count=len(rows),
        ordinary_sample_count=len(samples),
        distinct_problem_count=len({r.problem_id for r in rows}),
        accepted_problem_count=len({r.problem_id for r in accepted}),
        accepted_version_ids=tuple(sorted({r.version_id for r in accepted}, key=str)),
        verdict_counts=dict(Counter(r.verdict for r in samples)),
        tags=tuple(
            TagStatistic(
                tag=t,
                sample_count=len(rs),
                accepted_count=sum(r.verdict == "accepted" for r in rs),
                non_accepted_count=sum(r.verdict != "accepted" for r in rs),
                evidence=tuple(evidence(r) for r in rs),
            )
            for t in tags
            for rs in [
                [
                    r
                    for r in samples
                    if r.version_id in versions and t in versions[r.version_id].tags
                ]
            ]
        ),
        recent=tuple(evidence(r) for r in rows),
        missing_metadata_count=sum(
            r.version_id not in versions or not versions[r.version_id].metadata_revision
            for r in rows
        ),
        metadata_revisions={
            r.version_id: versions[r.version_id].metadata_revision
            if r.version_id in versions
            else None
            for r in rows
        },
    )


def rank_candidates(
    candidates,
    parameters,
    history,
    versions,
    accepted,
    now,
    feedback=(),
    *,
    timings=None,
    prerequisite_evidence=None,
):
    practice = timings or {}
    prerequisites = prerequisite_evidence or {}
    goals = set(parameters.tags)
    cover_each_goal = parameters.cover_each_goal and bool(goals)
    unresolved = [
        r
        for r in history.records
        if r.created_at <= now
        and ordinary(r)
        and r.verdict != "accepted"
        and r.version_id not in accepted
    ]

    def matching(c):
        return [
            r
            for r in unresolved
            if r.version_id in versions and set(c.tags) & set(versions[r.version_id].tags)
        ]

    eligible = [
        c
        for c in candidates
        if c.available
        and c.is_current_version
        and matches_goals(c.tags, goals, cover_each_goal)
        and (parameters.difficulty is None or parameters.difficulty == c.difficulty)
    ]
    requires_practice = any(candidate.prerequisite_tags for candidate in eligible)
    missing_by_candidate = [
        set(candidate.prerequisite_tags).difference(prerequisites) for candidate in eligible
    ]
    missing_tags = sorted(set().union(*missing_by_candidate))
    gap = (
        PrerequisiteGap(
            affected_candidates=sum(bool(missing) for missing in missing_by_candidate),
            missing_tags=tuple(missing_tags[:20]),
            remaining_tags=max(0, len(missing_tags) - 20),
        )
        if missing_tags
        else None
    )
    eligible = [
        candidate
        for candidate, missing in zip(eligible, missing_by_candidate, strict=True)
        if not missing
    ]
    sources = {c.version_id: matching_feedback(c.tags, feedback) for c in eligible}
    related_records = {c.version_id: matching(c) for c in eligible}
    weights = RANKING_WEIGHTS.get(parameters.ranking_profile)

    def order(candidate):
        signals = (
            practice_priority(candidate.version_id in accepted, practice.get(candidate.version_id)),
            feedback_priority(candidate.difficulty, sources[candidate.version_id]),
            int(not related_records[candidate.version_id]),
        )
        weighted = (
            sum(weight * signal for weight, signal in zip(weights, signals, strict=True))
            if weights is not None
            else 0
        )
        return (
            weighted,
            *signals,
            {"easy": 0, "medium": 1, "hard": 2}.get(candidate.difficulty, 3),
            str(candidate.problem_id),
            str(candidate.version_id),
        )

    eligible.sort(key=order)
    unique = {}
    for c in eligible:
        unique.setdefault(c.problem_id, c)
    ordered = list(unique.values())
    selected = (
        choose_for_coverage(ordered, goals, parameters.count)
        if cover_each_goal
        else ordered[: parameters.count]
    )
    items = []
    for c in selected:
        reasons = [
            "本版本已有历史 AC，可用于复习"
            if c.version_id in accepted
            else "本人全历史中本版本未见 AC"
        ]
        timing = practice.get(c.version_id)
        own_prerequisites = tuple(prerequisites[tag] for tag in c.prerequisite_tags)
        if own_prerequisites:
            reasons.append("本人已有符合条件的先修通过记录")
        if timing is not None:
            reasons.append(PRACTICE_REASONS[timing.status])
        if cover_each_goal:
            matched = "、".join(tag for tag in dict.fromkeys(parameters.tags) if tag in c.tags)
            reasons.append(f"覆盖目标知识点 {matched}")
        elif parameters.tags:
            reasons.append("匹配所选知识点")
        related = related_records[c.version_id]
        if related:
            reasons.append("近期同标签有未解决的普通判定，仅作为练习方向，不代表知识点掌握程度")
        if not c.metadata_revision:
            reasons.append("尚无学习元数据，不推测难度或知识点")
        source = sources[c.version_id]
        if source:
            reasons.append(feedback_reason(source))
        items.append(
            RecommendedProblem(
                problem_id=c.problem_id,
                problem_version_id=c.version_id,
                slug=c.slug,
                title=c.title,
                tags=c.tags,
                difficulty=c.difficulty,
                metadata_revision=c.metadata_revision or None,
                previously_accepted=c.version_id in accepted,
                reasons=tuple(reasons),
                evidence=tuple(evidence(r) for r in related),
                feedback_evidence=source,
                timing=timing,
                prerequisites=own_prerequisites,
            )
        )
    gaps = [gap.message()] if gap is not None else []
    if len(items) < parameters.count:
        gaps.append("当前可用且符合条件的真实题目不足")
    if any(not c.metadata_revision for c in candidates):
        gaps.append("部分题目缺少学习元数据，不能用于未知标签或难度的匹配")
    rule_version = RULE_VERSION
    if weights is not None:
        rule_version = WEIGHTED_RULE_VERSION
    elif cover_each_goal:
        rule_version = COVERAGE_RULE_VERSION
    elif requires_practice:
        rule_version = PREREQUISITE_RULE_VERSION
    elif practice:
        rule_version = TIMING_RULE_VERSION
    elif any(sources.values()):
        rule_version = FEEDBACK_RULE_VERSION
    return LearningRecommendations(
        generated_at=now,
        rule_version=rule_version,
        parameters=parameters,
        items=tuple(items),
        gaps=tuple(gaps),
        prerequisite_gap=gap,
        goal_coverage=goal_coverage(parameters.tags, (c.tags for c in selected))
        if cover_each_goal
        else None,
    )


class LearningService:
    def __init__(self, sessions):
        self.sessions = sessions

    async def overview(self, principal):
        now = datetime.now(UTC)
        async with self.sessions() as session:
            history = await records.learning_records(
                session, principal, since=now - timedelta(days=90), as_of=now, limit=500
            )
            versions = await problems.learning_versions(
                session, {r.version_id for r in history.records}
            )
            return summarize(history, versions, now)

    async def _recommend(
        self,
        session,
        principal,
        parameters,
        *,
        allowed_difficulties: tuple[Difficulty, ...] = (),
        as_of: datetime | None = None,
    ):
        now = as_of or datetime.now(UTC)
        candidates = await problems.learning_candidates(
            session,
            tags=parameters.tags,
            difficulty=parameters.difficulty,
            allowed_difficulties=allowed_difficulties,
            match_any_tag=parameters.cover_each_goal,
            limit=500,
        )
        truncated = len(candidates) > 500
        candidates = candidates[:500]
        history = await records.learning_records(
            session, principal, since=now - timedelta(days=90), as_of=now, limit=500
        )
        ids = {r.version_id for r in history.records} | {c.version_id for c in candidates}
        versions = await problems.learning_versions(session, ids)
        practice = await records.learning_practice(session, principal, ids, as_of=now)
        accepted = {version for version, dates in practice.items() if dates.last_accepted_at}
        last_planned = await recent_plan_dates(
            session, principal.account_id, {candidate.problem_id for candidate in candidates}, now
        )
        timings: dict[UUID, TrainingTiming] = {}
        for candidate in candidates:
            timing = training_timing(
                practice.get(candidate.version_id), last_planned.get(candidate.problem_id), now
            )
            if timing is not None:
                timings[candidate.version_id] = timing
        feedback = await recent_feedback(session, principal.account_id, now)
        prerequisites = await accepted_prerequisite_evidence(
            session, principal.account_id, candidates, now
        )
        result = rank_candidates(
            candidates,
            parameters,
            history,
            versions,
            accepted,
            now,
            feedback,
            timings=timings,
            prerequisite_evidence=prerequisites,
        )
        return result.model_copy(
            update={
                "candidate_limit": 500,
                "candidates_truncated": truncated,
                "gaps": result.gaps
                + (("候选范围超过500题，本次仅在稳定ID排序的前500题中推荐",) if truncated else ()),
            }
        )

    async def recommend(
        self,
        principal,
        parameters,
        *,
        allowed_difficulties: tuple[Difficulty, ...] = (),
        as_of: datetime | None = None,
    ):
        async with self.sessions() as session:
            return await self._recommend(
                session,
                principal,
                parameters,
                allowed_difficulties=allowed_difficulties,
                as_of=as_of,
            )

    async def create(
        self,
        principal,
        body: CreateTraining | CreateTrainingFollowUp,
        *,
        source_plan_id: UUID | None = None,
    ):
        payload = body.model_dump_json()
        if source_plan_id is not None:
            payload = f"training-follow-up-v1:{source_plan_id}:{payload}"
        digest = hashlib.sha256(payload.encode()).hexdigest()
        async with self.sessions.begin() as session:
            # Serialize only this owner's idempotency key, including concurrent double clicks.
            key = int.from_bytes(
                hashlib.sha256(f"{principal.account_id}:{body.idempotency_key}".encode()).digest()[
                    :8
                ],
                "big",
                signed=True,
            )
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            source = None
            if source_plan_id is not None:
                source = await self._owned_plan(session, principal, source_plan_id)
                if source is None:
                    raise TrainingSourceUnavailable("source training unavailable")
            plan = await session.scalar(
                select(LearningPlan).where(
                    LearningPlan.owner_id == principal.account_id,
                    LearningPlan.idempotency_key == body.idempotency_key,
                )
            )
            if plan:
                if plan.request_digest != digest:
                    raise IdempotencyConflict("idempotency key reused with different request")
                return await self._detail(session, principal, plan.id)

            if source is None:
                assert isinstance(body, CreateTraining)
                title = body.title
                parameters = StoredTrainingParameters.model_validate(
                    body.model_dump(include=set(LearningParameters.model_fields))
                )
            else:
                assert isinstance(body, CreateTrainingFollowUp)
                title = source.title
                parameters = StoredTrainingParameters.model_validate(source.parameters).model_copy(
                    update={"previous_plan_id": source.id}
                )
                changes = body.model_dump(exclude={"idempotency_key"}, exclude_none=True)
                if changes:
                    saved_schedule = parameters.schedule
                    if (
                        saved_schedule is None
                        or not changes.keys() <= type(saved_schedule).model_fields.keys()
                    ):
                        raise LearningUnavailable("time settings do not match the source training")
                    count = parameters.count
                    if (
                        isinstance(saved_schedule, FocusedTrainingSchedule)
                        and body.daily_minutes is not None
                        and body.daily_minutes != saved_schedule.daily_minutes
                    ):
                        count = focused_exercise_count(body.daily_minutes)
                    if (
                        isinstance(saved_schedule, PeriodicReviewSchedule)
                        and body.sessions_per_week is not None
                        and body.sessions_per_week != saved_schedule.sessions_per_week
                    ):
                        count = body.sessions_per_week
                    parameters = parameters.model_copy(
                        update={
                            "schedule": saved_schedule.model_copy(update=changes),
                            "count": count,
                        }
                    )
            result = await self._recommend(
                session,
                principal,
                parameters.selection(),
                allowed_difficulties=parameters.allowed_difficulties,
            )
            if not result.items:
                raise NoCandidates("no available matching problems")
            current = await problems.learning_selected(
                session, {i.problem_version_id for i in result.items}
            )
            if any(
                i.problem_version_id not in current
                or not current[i.problem_version_id].available
                or not current[i.problem_version_id].is_current_version
                or (current[i.problem_version_id].metadata_revision or None) != i.metadata_revision
                for i in result.items
            ):
                raise LearningUnavailable("recommendation changed; regenerate")
            verified = await self._verify_prerequisites(session, principal, current, result.items)
            schedule = parameters.schedule
            estimated = (
                schedule.daily_minutes // len(result.items)
                if isinstance(schedule, FocusedTrainingSchedule)
                else None
            )
            plan = LearningPlan(
                id=uuid4(),
                owner_id=principal.account_id,
                idempotency_key=body.idempotency_key,
                request_digest=digest,
                title=title,
                rule_version=result.rule_version,
                parameters=parameters.model_dump(mode="json"),
            )
            await self._persist_training(
                session,
                plan,
                tuple(
                    item.model_copy(
                        update={
                            "prerequisites": verified[item.problem_version_id],
                            "estimated_minutes": estimated,
                        }
                    )
                    for item in result.items
                ),
            )
            return await self._detail(session, principal, plan.id)

    @staticmethod
    async def _verify_prerequisites(session, principal, current, items):
        evidence_by_tag = await accepted_prerequisite_evidence(
            session, principal.account_id, current.values(), datetime.now(UTC)
        )
        verified = {}
        for item in items:
            current_tags = current[item.problem_version_id].prerequisite_tags
            earlier_tags = {proof.tag for proof in item.prerequisites}
            if earlier_tags != set(current_tags) or any(
                tag not in evidence_by_tag for tag in current_tags
            ):
                raise LearningUnavailable("training prerequisites changed; regenerate")
            verified[item.problem_version_id] = tuple(evidence_by_tag[tag] for tag in current_tags)
        return verified

    @staticmethod
    async def _persist_training(session, plan: LearningPlan, items: tuple[RecommendedProblem, ...]):
        session.add(plan)
        await session.flush()
        for position, item in enumerate(items):
            session.add(
                LearningPlanItem(
                    id=uuid4(),
                    plan_id=plan.id,
                    position=position,
                    problem_id=item.problem_id,
                    version_id=item.problem_version_id,
                    snapshot=item.model_dump(mode="json"),
                    note="",
                )
            )
        await session.flush()

    async def saved_agent_plan(self, session, principal, run_id):
        link = await session.get(AgentPlanLinkRow, run_id)
        if link is not None:
            return await self._detail(session, principal, link.plan_id)
        return None

    async def save_agent_plan(
        self,
        session,
        principal,
        run_id,
        draft: FocusedTrainingResult | PeriodicReviewResult,
        config: FocusedTrainingParameters | PeriodicReviewParameters,
    ) -> TrainingDetail:
        items = draft.items
        if not 1 <= len(items) <= 10:
            raise NoCandidates("no available matching problems")
        ids = {item.problem_version_id for item in items}
        if len(ids) != len(items) or len({item.problem_id for item in items}) != len(items):
            raise LearningUnavailable("invalid duplicate training selection")
        current = await problems.learning_selected(session, ids)
        if set(current) != ids:
            raise LearningUnavailable("recommendation changed; regenerate")

        cover_each_goal = False
        allowed_difficulties = ()
        if isinstance(draft, FocusedTrainingResult) and isinstance(
            config, FocusedTrainingParameters
        ):
            title = "专项训练"
            tags = config.tags
            goals = set(tags)
            cover_each_goal = config.cover_each_goal and bool(goals)
            levels = ("easy", "medium", "hard")
            allowed = levels[
                levels.index(config.minimum_difficulty) : levels.index(config.maximum_difficulty)
                + 1
            ]
            allowed_difficulties = allowed
            difficulty = config.minimum_difficulty if len(allowed) == 1 else None
            if any(
                candidate.difficulty not in allowed
                or not matches_goals(candidate.tags, goals, cover_each_goal)
                for candidate in current.values()
            ):
                raise LearningUnavailable("recommendation no longer matches its skill")
            if cover_each_goal and draft.goal_coverage != goal_coverage(
                tags, (candidate.tags for candidate in current.values())
            ):
                raise LearningUnavailable("training goal coverage changed; regenerate")
            source_evidence = ()
            schedule = FocusedTrainingSchedule(goal=config.goal, daily_minutes=config.daily_minutes)
        elif isinstance(draft, PeriodicReviewResult) and isinstance(
            config, PeriodicReviewParameters
        ):
            title, tags, difficulty = "阶段复习", (), None
            source_evidence = draft.evidence
            schedule = PeriodicReviewSchedule(
                goal=config.stage_goal,
                cycle_days=config.cycle_days,
                sessions_per_week=config.sessions_per_week,
            )
        else:
            raise LearningUnavailable("training selection does not match its skill")

        practice = await records.learning_practice(session, principal, ids, as_of=datetime.now(UTC))
        accepted = {version for version, dates in practice.items() if dates.last_accepted_at}
        verified = await self._verify_prerequisites(session, principal, current, items)
        snapshots = []
        for item in items:
            selected = current[item.problem_version_id]
            if selected.problem_id != item.problem_id:
                raise LearningUnavailable("recommendation no longer matches its problem")
            snapshots.append(
                RecommendedProblem(
                    problem_id=selected.problem_id,
                    problem_version_id=selected.version_id,
                    slug=selected.slug,
                    title=selected.title,
                    tags=selected.tags,
                    difficulty=selected.difficulty,
                    metadata_revision=selected.metadata_revision or None,
                    previously_accepted=selected.version_id in accepted,
                    reasons=(item.objective,)
                    + (
                        (feedback_reason(item.feedback_evidence),) if item.feedback_evidence else ()
                    ),
                    feedback_evidence=item.feedback_evidence,
                    timing=item.timing,
                    prerequisites=verified[item.problem_version_id],
                    estimated_minutes=item.estimated_minutes,
                    evidence=tuple(
                        e for e in source_evidence if e.problem_version_id == selected.version_id
                    ),
                )
            )

        parameters = StoredTrainingParameters(
            count=len(items),
            tags=tags,
            difficulty=difficulty,
            cover_each_goal=cover_each_goal,
            schedule=schedule,
            allowed_difficulties=allowed_difficulties,
            ranking_profile=config.ranking_profile,
        )
        rule_version = AGENT_PLAN_RULE_VERSION
        if parameters.ranking_profile is not None:
            rule_version = AGENT_WEIGHTED_RULE_VERSION
        elif cover_each_goal:
            rule_version = AGENT_COVERAGE_RULE_VERSION
        elif any(verified.values()):
            rule_version = AGENT_PREREQUISITE_RULE_VERSION
        elif any(item.timing is not None for item in items):
            rule_version = AGENT_TIMING_RULE_VERSION
        elif any(item.feedback_evidence is not None for item in items):
            rule_version = AGENT_FEEDBACK_RULE_VERSION
        plan = LearningPlan(
            id=uuid4(),
            owner_id=principal.account_id,
            idempotency_key=uuid4(),
            request_digest=hashlib.sha256(
                draft.model_dump_json(exclude={"model_hint", "model_status"}).encode()
            ).hexdigest(),
            title=title,
            rule_version=rule_version,
            parameters=parameters.model_dump(mode="json"),
        )
        await self._persist_training(session, plan, tuple(snapshots))
        session.add(AgentPlanLinkRow(run_id=run_id, plan_id=plan.id))
        return await self._detail(session, principal, plan.id)

    @staticmethod
    async def _owned_plan(session, principal, plan_id):
        return await session.scalar(
            select(LearningPlan).where(
                LearningPlan.id == plan_id, LearningPlan.owner_id == principal.account_id
            )
        )

    async def _detail(self, session, principal, plan_id):
        plan = await self._owned_plan(session, principal, plan_id)
        if plan is None:
            raise LearningUnavailable("plan unavailable")
        rows = (
            await session.scalars(
                select(LearningPlanItem)
                .where(LearningPlanItem.plan_id == plan.id)
                .order_by(LearningPlanItem.position)
            )
        ).all()
        ids = {r.version_id for r in rows}
        current = await problems.learning_versions(session, ids)
        progress = await records.learning_progress(
            session, principal, ids, since=plan.created_at, as_of=datetime.now(UTC)
        )
        evaluations = await session.scalars(
            select(TrainingFeedbackRow).where(
                TrainingFeedbackRow.owner_id == principal.account_id,
                TrainingFeedbackRow.plan_id == plan.id,
            )
        )
        feedback = {
            row.item_id: TrainingFeedback(rating=row.rating, rated_at=row.rated_at)
            for row in evaluations
        }
        return self._project(plan, rows, current, progress, feedback)

    @staticmethod
    def _project(plan, rows, current, progress, feedback=None):
        feedback = {} if feedback is None else feedback
        items = tuple(
            TrainingItem(
                **r.snapshot,
                id=r.id,
                position=r.position,
                note=r.note,
                feedback=feedback.get(r.id),
                progress=progress[r.version_id].status
                if r.version_id in progress
                else "not_started",
                progress_evidence=tuple(evidence(e) for e in progress[r.version_id].evidence)
                if r.version_id in progress
                else (),
                available=r.version_id in current and current[r.version_id].available,
                is_current_version=r.version_id in current
                and current[r.version_id].is_current_version,
            )
            for r in rows
        )
        stored = StoredTrainingParameters.model_validate(plan.parameters)
        parameters = stored.selection()
        return TrainingDetail(
            id=plan.id,
            title=plan.title,
            created_at=plan.created_at,
            rule_version=plan.rule_version,
            item_count=len(items),
            completed_count=sum(i.progress == "completed" for i in items),
            parameters=parameters,
            items=items,
            goal_coverage=goal_coverage(parameters.tags, (item.tags for item in items))
            if parameters.cover_each_goal and parameters.tags
            else None,
            schedule=stored.schedule,
            previous_plan_id=stored.previous_plan_id,
        )

    async def detail(self, principal, plan_id):
        async with self.sessions() as session:
            return await self._detail(session, principal, plan_id)

    async def page(self, principal):
        async with self.sessions() as session:
            plans = (
                await session.scalars(
                    select(LearningPlan)
                    .where(LearningPlan.owner_id == principal.account_id)
                    .order_by(LearningPlan.created_at.desc(), LearningPlan.id)
                    .limit(51)
                )
            ).all()
            truncated = len(plans) > 50
            plans = plans[:50]
            rows = (
                await session.scalars(
                    select(LearningPlanItem)
                    .where(LearningPlanItem.plan_id.in_([p.id for p in plans]))
                    .order_by(LearningPlanItem.position)
                )
            ).all()
            current = await problems.learning_versions(session, {r.version_id for r in rows})
            by_plan = {p.id: [] for p in plans}
            for row in rows:
                by_plan[row.plan_id].append(row)
            details = []
            as_of = datetime.now(UTC)
            for plan in plans:
                items = by_plan[plan.id]
                progress = await records.learning_progress(
                    session,
                    principal,
                    {r.version_id for r in items},
                    since=plan.created_at,
                    as_of=as_of,
                )
                details.append(self._project(plan, items, current, progress))
            return TrainingPage(
                truncated=truncated,
                items=tuple(
                    TrainingSummary(**d.model_dump(include=set(TrainingSummary.model_fields)))
                    for d in details
                ),
            )

    @staticmethod
    async def _locked_item(session, principal, plan_id, item_id) -> LearningPlanItem:
        row = await session.scalar(
            select(LearningPlanItem)
            .join(LearningPlan)
            .where(
                LearningPlan.owner_id == principal.account_id,
                LearningPlan.id == plan_id,
                LearningPlanItem.id == item_id,
            )
            .with_for_update(of=LearningPlanItem)
        )
        if row is None:
            raise LearningUnavailable("plan item unavailable")
        return row

    async def note(self, principal, plan_id, item_id, body):
        async with self.sessions.begin() as session:
            row = await self._locked_item(session, principal, plan_id, item_id)
            row.note = body.note
            await session.flush()
            return await self._detail(session, principal, plan_id)

    async def rate(self, principal, plan_id, item_id, body):
        async with self.sessions.begin() as session:
            row = await self._locked_item(session, principal, plan_id, item_id)
            prior = await session.get(TrainingFeedbackRow, item_id)
            if body.rating is None:
                if prior is not None:
                    await session.delete(prior)
            elif prior is None:
                problem = RecommendedProblem.model_validate(row.snapshot)
                session.add(
                    TrainingFeedbackRow(
                        item_id=item_id,
                        owner_id=principal.account_id,
                        plan_id=plan_id,
                        version_id=row.version_id,
                        difficulty=problem.difficulty,
                        tags=list(problem.tags),
                        rating=body.rating,
                        rated_at=func.clock_timestamp(),
                    )
                )
            elif prior.rating != body.rating:
                prior.rating = body.rating
                prior.rated_at = func.clock_timestamp()
            await session.flush()
            return await self._detail(session, principal, plan_id)
