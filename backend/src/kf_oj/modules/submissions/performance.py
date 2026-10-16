"""Read-only, bounded anonymous cohorts from Coordinator's immutable projection."""

import asyncio
import json
import time
from collections import OrderedDict
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.judging import I64_MAX
from kf_oj.contracts.judging_versions import decode_report
from kf_oj.contracts.submission_performance import (
    MAX_PERFORMANCE_SAMPLES,
    MIN_PERFORMANCE_SAMPLES,
    PerformanceBucket,
    PerformanceMetric,
    SubmissionPerformance,
)
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.submissions.models import Evaluation, EvaluationPerformance, Submission
from kf_oj.modules.submissions.public import SubmissionUnavailable

CACHE_TTL_SECONDS = 30
CACHE_CAPACITY = 128
HISTOGRAM_BUCKETS = 24


def metric(unit, current, rows=(), *, reason=None) -> PerformanceMetric:
    """Integer arithmetic throughout; conversion for display belongs to the client."""
    truncated = len(rows) > MAX_PERFORMANCE_SAMPLES
    rows = rows[:MAX_PERFORMANCE_SAMPLES]
    count = len(rows)
    base = dict(
        unit=unit,
        current=None if current is None else str(current),
        sample_count=count,
        window_truncated=truncated,
        oldest_sample_at=rows[-1][1] if rows else None,
    )
    if reason is None:
        reason = (
            "measurement_missing"
            if current is None
            else ("insufficient_samples" if count < MIN_PERFORMANCE_SAMPLES else None)
        )
    if reason is not None:
        return PerformanceMetric(
            **base,
            available=False,
            reason=reason,
            strictly_slower_count=None,
            tied_count=None,
            buckets=(),
        )
    values = [row[0] for row in rows]
    end = max(1, current, max(values))
    step = (end + HISTOGRAM_BUCKETS - 1) // HISTOGRAM_BUCKETS
    # Last upper bound includes the maximum, including I64_MAX. No overflow/padding.
    boundaries = list(range(0, end, step)) + [end]
    counts = [0] * (len(boundaries) - 1)
    for value in values:
        counts[min(value // step, len(counts) - 1)] += 1
    return PerformanceMetric(
        **base,
        available=True,
        reason=None,
        strictly_slower_count=sum(value > current for value in values),
        tied_count=sum(value == current for value in values),
        buckets=tuple(
            PerformanceBucket(
                start=str(start),
                end=str(stop),
                upper_inclusive=index == len(counts) - 1,
                count=counts[index],
            )
            for index, (start, stop) in enumerate(zip(boundaries, boundaries[1:], strict=False))
        ),
    )


class SubmissionPerformanceQueries:
    def __init__(self, sessions: async_sessionmaker):
        self.sessions = sessions
        # No queued aggregate workload. At most one bounded in-memory row cohort per process.
        self._slot = asyncio.Lock()
        # Cache contains small immutable aggregate DTOs, never 10,000-row arrays.
        self._cache: OrderedDict[tuple, tuple[float, SubmissionPerformance]] = OrderedDict()

    async def detail(self, principal: Principal, submission_id: UUID) -> SubmissionPerformance:
        if self._slot.locked():
            raise ValueError("performance query capacity unavailable")
        async with self._slot:
            try:
                async with asyncio.timeout(3), self.sessions() as session, session.begin():
                    await session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                    await session.execute(text("SET LOCAL statement_timeout='2s'"))
                    await session.execute(text("SET LOCAL lock_timeout='500ms'"))
                    # Ownership checked even on a cache hit. Session authentication is upstream.
                    row = (
                        await session.execute(
                            select(Submission, Evaluation, EvaluationPerformance)
                            .join(Evaluation, Evaluation.submission_id == Submission.id)
                            .outerjoin(
                                EvaluationPerformance,
                                EvaluationPerformance.evaluation_id == Evaluation.id,
                            )
                            .where(
                                Submission.id == submission_id,
                                Submission.account_id == principal.account_id,
                            )
                        )
                    ).one_or_none()
                    if row is None:
                        raise SubmissionUnavailable("submission unavailable")
                    submission, evaluation, projection = row
                    as_of = await session.scalar(text("SELECT clock_timestamp()"))
                    accepted = (
                        evaluation.state == "finished"
                        and evaluation.verdict == "accepted"
                        and evaluation.failure_code is None
                    )
                    if projection is not None and (
                        not accepted
                        or projection.attempt_id != evaluation.current_attempt_id
                        or projection.report_digest != evaluation.final_report_digest
                    ):
                        raise ValueError("stored performance binding invalid")
                    cpu = memory = None
                    reason = None if accepted else "not_accepted"
                    if projection is not None:
                        cpu, memory = projection.cpu_time_us, projection.memory_peak_bytes
                    elif accepted:
                        # A single owned legacy report, never a full-table JSON scan or promotion.
                        report = decode_report(
                            json.dumps(evaluation.final_report, ensure_ascii=False).encode()
                        )
                        if (
                            report.binding.evaluation_id != evaluation.id
                            or report.binding.attempt_id != evaluation.current_attempt_id
                            or report.verdict.value != "accepted"
                            or not report.cleanup_complete
                        ):
                            raise ValueError("stored result binding invalid")
                        cpu, memory = report.run_cpu_time_us, report.run_memory_peak_bytes
                        reason = "environment_unverified"
                    if any(v is not None and not 0 <= v <= I64_MAX for v in (cpu, memory)):
                        raise ValueError("stored measurement out of range")
                    base = dict(
                        submission_id=submission.id,
                        problem_version_id=submission.problem_version_id,
                        language=submission.language,
                        as_of=as_of,
                        comparison_group=None
                        if projection is None
                        else projection.comparison_group,
                    )
                    if projection is None:
                        return SubmissionPerformance(
                            **base,
                            cpu=metric("cpu_microseconds", cpu, reason=reason),
                            memory=metric("bytes", memory, reason=reason),
                        )
                    group = projection.comparison_group
                    watermark = (
                        await session.execute(
                            select(
                                EvaluationPerformance.finished_at,
                                EvaluationPerformance.evaluation_id,
                            )
                            .where(EvaluationPerformance.comparison_group == group)
                            .order_by(
                                EvaluationPerformance.finished_at.desc(),
                                EvaluationPerformance.evaluation_id.desc(),
                            )
                            .limit(1)
                        )
                    ).one()
                    key = (submission.id, projection.report_digest, group, tuple(watermark))
                    cached = self._cache.get(key)
                    now = time.monotonic()
                    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                        self._cache.move_to_end(key)
                        return cached[1]
                    metrics = []
                    for column, unit, current in [
                        (EvaluationPerformance.cpu_time_us, "cpu_microseconds", cpu),
                        (EvaluationPerformance.memory_peak_bytes, "bytes", memory),
                    ]:
                        rows = (
                            await session.execute(
                                select(column, EvaluationPerformance.finished_at)
                                .where(
                                    EvaluationPerformance.comparison_group == group,
                                    EvaluationPerformance.evaluation_id != evaluation.id,
                                    column.is_not(None),
                                )
                                .order_by(
                                    EvaluationPerformance.finished_at.desc(),
                                    EvaluationPerformance.evaluation_id.desc(),
                                )
                                .limit(MAX_PERFORMANCE_SAMPLES + 1)
                            )
                        ).all()
                        metrics.append(metric(unit, current, rows))
                    result = SubmissionPerformance(**base, cpu=metrics[0], memory=metrics[1])
                    self._cache[key] = (now, result)
                    self._cache.move_to_end(key)
                    while len(self._cache) > CACHE_CAPACITY:
                        self._cache.popitem(last=False)
                    return result
            except (SQLAlchemyError, TimeoutError) as error:
                raise ValueError("performance database unavailable") from error
