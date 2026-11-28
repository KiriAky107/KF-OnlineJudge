"""The model capability offered to the learning runtime."""

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, runtime_checkable

from kf_oj.contracts.agent_learning import (
    FocusedTrainingResult,
    PeriodicReviewResult,
    SubmissionReviewResult,
)
from kf_oj.contracts.judging import Verdict
from kf_oj.contracts.learning import LearningEvidence
from kf_oj.contracts.learning_model import (
    GuidedChatModelFacts,
    GuidedHelpModelFacts,
    LearningQuestion,
    ModelHint,
    ModelHintInput,
    ModelOutput,
    ModelTopic,
    PlanModelFacts,
    ReviewChangeCounts,
    ReviewCodeFacts,
    ReviewComparisonCases,
    ReviewFocusCategory,
    ReviewModelFacts,
    ReviewResourceName,
    SourceExcerptModelFacts,
)
from kf_oj.contracts.submission_evidence import CodeSubmissionRef


class ModelBudgetExceeded(RuntimeError):
    """No more model requests are available within the current budget."""


class LearningHintModel(Protocol):
    @property
    def accepts_source_excerpt(self) -> bool: ...

    async def hint(self, content: ModelHintInput) -> ModelHint: ...


@runtime_checkable
class StreamingLearningHintModel(LearningHintModel, Protocol):
    async def stream_hint(
        self, content: GuidedChatModelFacts, on_output: ModelOutput
    ) -> ModelHint: ...


@runtime_checkable
class ChatResourceModel(StreamingLearningHintModel, Protocol):
    supports_review_mcp: bool

    async def chat_with_tools(
        self,
        content: GuidedChatModelFacts,
        on_output: ModelOutput,
        read_practice: Callable[[], Awaitable[str]],
        continue_request: Callable[[tuple[ReviewResourceName, ...]], Awaitable[bool]],
    ) -> ModelHint: ...


@runtime_checkable
class ReviewResourceModel(LearningHintModel, Protocol):
    supports_review_mcp: bool

    async def review_with_tools(
        self,
        content: ReviewModelFacts,
        resources: Mapping[ReviewResourceName, Callable[[], Awaitable[str]]],
        continue_request: Callable[[tuple[ReviewResourceName, ...]], Awaitable[bool]],
    ) -> ModelHint: ...


@runtime_checkable
class StreamingReviewModel(LearningHintModel, Protocol):
    async def stream_review(
        self,
        content: ReviewModelFacts | SourceExcerptModelFacts,
        on_output: ModelOutput,
        *,
        resources: Mapping[ReviewResourceName, Callable[[], Awaitable[str]]] | None = None,
        continue_request: Callable[[tuple[ReviewResourceName, ...]], Awaitable[bool]] | None = None,
    ) -> ModelHint: ...


def guided_help_model_facts(
    topic: ModelTopic, evidence: LearningEvidence | None
) -> GuidedHelpModelFacts | None:
    if (
        evidence is None
        or evidence.state != "finished"
        or evidence.verdict not in Verdict
        or evidence.verdict in (Verdict.CANCELLED, Verdict.SYSTEM_ERROR)
    ):
        return None
    return GuidedHelpModelFacts(topic=topic, verdict=Verdict(evidence.verdict))


def _review_code_facts(
    result: SubmissionReviewResult, *, include_control_flow: bool, include_comparison_cases: bool
) -> ReviewCodeFacts | None:
    code = result.code
    if code is None or not code.source_available:
        return None

    finished = {
        (record.submission_id, record.evaluation_id, record.problem_version_id): record.verdict
        for record in result.records
        if record.state == "finished" and record.verdict is not None
    }

    def verdict(reference: CodeSubmissionRef) -> str | None:
        return finished.get(
            (reference.submission_id, reference.evaluation_id, reference.problem_version_id)
        )

    latest_verdict = verdict(code.source)
    if latest_verdict is None:
        return None

    comparison = code.comparison
    changes = None
    control_flow = None
    comparison_cases = None
    if (
        comparison is not None
        and comparison.earlier.problem_version_id == code.source.problem_version_id
        and comparison.earlier.language == code.source.language
        and verdict(comparison.earlier) is not None
    ):
        regions = comparison.regions
        changes = ReviewChangeCounts(
            regions=len(regions),
            inserted_lines=sum(region.after_count for region in regions if region.kind == "insert"),
            deleted_lines=sum(region.before_count for region in regions if region.kind == "delete"),
            replaced_before_lines=sum(
                region.before_count for region in regions if region.kind == "replace"
            ),
            replaced_after_lines=sum(
                region.after_count for region in regions if region.kind == "replace"
            ),
            truncated=comparison.truncated,
        )
        if include_control_flow:
            control_flow = comparison.control_flow
        if include_comparison_cases:
            cases = Counter(
                case for change in comparison.boundary_changes for case in change.changed_cases
            )
            if cases:
                comparison_cases = ReviewComparisonCases(
                    less=cases["less"], equal=cases["equal"], greater=cases["greater"]
                )
    compiler_issue = (
        code.compiler_correction.kind
        if latest_verdict == "compilation_error" and code.compiler_correction is not None
        else None
    )
    if changes is None and compiler_issue is None:
        return None
    return ReviewCodeFacts(
        changes=changes,
        compiler_issue=compiler_issue,
        control_flow=control_flow,
        comparison_cases=comparison_cases,
    )


def review_model_facts(
    result: SubmissionReviewResult,
    *,
    include_code_counts: bool = False,
    include_control_flow: bool = False,
    include_comparison_cases: bool = False,
    focus_options: tuple[ReviewFocusCategory, ...] = (),
    question: LearningQuestion | None = None,
) -> ReviewModelFacts | None:
    verdicts = Counter(
        record.verdict
        for record in result.records
        if record.state == "finished" and record.verdict is not None
    )
    if not verdicts:
        return None
    finished = sum(verdicts.values())
    accepted = verdicts["accepted"]
    wrong = verdicts["wrong_answer"]
    compile_errors = verdicts["compilation_error"]
    runtime_errors = verdicts["runtime_error"]
    limits = sum(
        verdicts[item]
        for item in ("time_limit_exceeded", "memory_limit_exceeded", "output_limit_exceeded")
    )
    return ReviewModelFacts(
        question=question,
        finished=finished,
        accepted=accepted,
        wrong_answers=wrong,
        compile_errors=compile_errors,
        runtime_errors=runtime_errors,
        resource_limits=limits,
        other_verdicts=finished - accepted - wrong - compile_errors - runtime_errors - limits,
        same_version_pairs=len(result.comparisons),
        later_accepted_pairs=sum(pair.outcome == "later_accepted" for pair in result.comparisons),
        truncated=result.truncated,
        code=(
            _review_code_facts(
                result,
                include_control_flow=include_control_flow,
                include_comparison_cases=include_comparison_cases,
            )
            if include_code_counts
            else None
        ),
        focus_options=focus_options,
    )


def review_focus_targets(
    result: SubmissionReviewResult,
) -> dict[ReviewFocusCategory, LearningEvidence]:
    later_accepted = {
        pair.later_submission_id for pair in result.comparisons if pair.outcome == "later_accepted"
    }
    targets: dict[ReviewFocusCategory, LearningEvidence] = {}
    for record in result.records:
        if record.state != "finished":
            continue
        if record.verdict == "wrong_answer":
            targets.setdefault("wrong_answer", record)
        if record.verdict == "compilation_error":
            targets.setdefault("compilation_error", record)
        if record.verdict == "accepted" and record.submission_id in later_accepted:
            targets.setdefault("later_accepted", record)
    return targets


def plan_model_facts(result: FocusedTrainingResult | PeriodicReviewResult) -> PlanModelFacts:
    counts = Counter(item.difficulty for item in result.items)
    coverage = result.goal_coverage if isinstance(result, FocusedTrainingResult) else None
    return PlanModelFacts(
        easy=counts["easy"],
        medium=counts["medium"],
        hard=counts["hard"],
        unclassified=counts[None],
        daily_minutes=result.daily_minutes if isinstance(result, FocusedTrainingResult) else None,
        cycle_days=result.cycle_days if isinstance(result, PeriodicReviewResult) else None,
        sessions_per_week=result.sessions_per_week
        if isinstance(result, PeriodicReviewResult)
        else None,
        covered_goals=len(coverage.covered) if coverage is not None else None,
        uncovered_goals=len(coverage.missing) if coverage is not None else None,
    )
