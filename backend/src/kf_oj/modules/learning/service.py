"""Bounded evidence, deterministic recommendations, and owner-scoped plans."""

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text

from kf_oj.contracts.learning import (
    LearningEvidence,
    LearningOverview,
    LearningParameters,
    LearningRecommendations,
    RecommendedProblem,
    TagStatistic,
    TrainingDetail,
    TrainingItem,
    TrainingPage,
    TrainingSummary,
)
from kf_oj.modules.problems import public as problems
from kf_oj.modules.submissions import public as records

from .models import LearningPlan, LearningPlanItem
from .public import IdempotencyConflict, LearningUnavailable, NoCandidates

RULE_VERSION = "learning-rules-v1"
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


def rank_candidates(candidates, parameters, history, versions, accepted, now):
    unresolved = [
        r
        for r in history.records
        if ordinary(r) and r.verdict != "accepted" and r.version_id not in accepted
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
        and (not parameters.tags or set(parameters.tags) <= set(c.tags))
        and (parameters.difficulty is None or parameters.difficulty == c.difficulty)
    ]
    eligible.sort(
        key=lambda c: (
            c.version_id in accepted,
            not bool(matching(c)),
            {"easy": 0, "medium": 1, "hard": 2}.get(c.difficulty, 3),
            str(c.problem_id),
            str(c.version_id),
        )
    )
    items, seen = [], set()
    for c in eligible:
        if c.problem_id in seen:
            continue
        seen.add(c.problem_id)
        reasons = [
            "本版本已有历史 AC，可用于复习"
            if c.version_id in accepted
            else "本人全历史中本版本未见 AC"
        ]
        if parameters.tags:
            reasons.append("匹配所选知识点")
        related = matching(c)
        if related:
            reasons.append("近期同标签有未解决的普通判定，仅作为练习方向，不代表知识点掌握程度")
        if not c.metadata_revision:
            reasons.append("尚无学习元数据，不推测难度或知识点")
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
            )
        )
        if len(items) == parameters.count:
            break
    gaps = []
    if len(items) < parameters.count:
        gaps.append("当前可用且符合条件的真实题目不足")
    if any(not c.metadata_revision for c in candidates):
        gaps.append("部分题目缺少学习元数据，不能用于未知标签或难度的匹配")
    return LearningRecommendations(
        generated_at=now,
        rule_version=RULE_VERSION,
        parameters=parameters,
        items=tuple(items),
        gaps=tuple(gaps),
    )


class LearningService:
    def __init__(self, sessions):
        self.sessions = sessions

    async def overview(self, principal):
        now = datetime.now(UTC)
        async with self.sessions() as session:
            history = await records.learning_records(
                session, principal, since=now - timedelta(days=90), limit=500
            )
            versions = await problems.learning_versions(
                session, {r.version_id for r in history.records}
            )
            return summarize(history, versions, now)

    async def _recommend(self, session, principal, parameters):
        now = datetime.now(UTC)
        candidates = await problems.learning_candidates(
            session, tags=parameters.tags, difficulty=parameters.difficulty, limit=500
        )
        truncated = len(candidates) > 500
        candidates = candidates[:500]
        history = await records.learning_records(
            session, principal, since=now - timedelta(days=90), limit=500
        )
        ids = {r.version_id for r in history.records} | {c.version_id for c in candidates}
        versions = await problems.learning_versions(session, ids)
        accepted = await records.accepted_versions(session, principal, ids)
        result = rank_candidates(candidates, parameters, history, versions, accepted, now)
        return result.model_copy(
            update={
                "candidate_limit": 500,
                "candidates_truncated": truncated,
                "gaps": result.gaps
                + (("候选范围超过500题，本次仅在稳定ID排序的前500题中推荐",) if truncated else ()),
            }
        )

    async def recommend(self, principal, parameters):
        async with self.sessions() as session:
            return await self._recommend(session, principal, parameters)

    async def create(self, principal, body):
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
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
            plan = await session.scalar(
                select(LearningPlan).where(
                    LearningPlan.owner_id == principal.account_id,
                    LearningPlan.idempotency_key == body.idempotency_key,
                )
            )
            if plan:
                if plan.request_digest != digest:
                    raise IdempotencyConflict("idempotency key reused with different request")
            else:
                parameters = LearningParameters(
                    **body.model_dump(include={"tags", "difficulty", "count"})
                )
                result = await self._recommend(session, principal, parameters)
                if not result.items:
                    raise NoCandidates("no available matching problems")
                current = await problems.learning_selected(
                    session, {i.problem_version_id for i in result.items}
                )
                if any(
                    i.problem_version_id not in current
                    or not current[i.problem_version_id].available
                    or not current[i.problem_version_id].is_current_version
                    for i in result.items
                ):
                    raise LearningUnavailable("recommendation changed; regenerate")
                plan = LearningPlan(
                    id=uuid4(),
                    owner_id=principal.account_id,
                    idempotency_key=body.idempotency_key,
                    request_digest=digest,
                    title=body.title,
                    rule_version=RULE_VERSION,
                    parameters=parameters.model_dump(mode="json"),
                )
                session.add(plan)
                await session.flush()
                for position, item in enumerate(result.items):
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
            return await self._detail(session, principal, plan.id)

    async def _detail(self, session, principal, plan_id):
        plan = await session.scalar(
            select(LearningPlan).where(
                LearningPlan.id == plan_id, LearningPlan.owner_id == principal.account_id
            )
        )
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
        progress = await records.learning_progress(session, principal, ids, since=plan.created_at)
        return self._project(plan, rows, current, progress)

    @staticmethod
    def _project(plan, rows, current, progress):
        items = tuple(
            TrainingItem(
                **r.snapshot,
                id=r.id,
                position=r.position,
                note=r.note,
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
        return TrainingDetail(
            id=plan.id,
            title=plan.title,
            created_at=plan.created_at,
            rule_version=plan.rule_version,
            item_count=len(items),
            completed_count=sum(i.progress == "completed" for i in items),
            parameters=LearningParameters(**plan.parameters),
            items=items,
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
            for plan in plans:
                items = by_plan[plan.id]
                progress = await records.learning_progress(
                    session, principal, {r.version_id for r in items}, since=plan.created_at
                )
                details.append(self._project(plan, items, current, progress))
            return TrainingPage(
                truncated=truncated,
                items=tuple(
                    TrainingSummary(**d.model_dump(include=set(TrainingSummary.model_fields)))
                    for d in details
                ),
            )

    async def note(self, principal, plan_id, item_id, body):
        async with self.sessions.begin() as session:
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
            row.note = body.note
            await session.flush()
            return await self._detail(session, principal, plan_id)
