"""Deterministic stages for the four learning operations."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel

from kf_oj.contracts.agent_learning import (
    AgentLearningInput,
    AgentLearningResult,
    FocusedTrainingParameters,
    FocusedTrainingResult,
    GuidedHelpParameters,
    GuidedHelpResult,
    PeriodicReviewParameters,
    PeriodicReviewRequest,
    PeriodicReviewResult,
    ProposedExercise,
    ReviewComparison,
    SourceEvidenceArguments,
    SourceEvidenceResult,
    StoredAgentLearningInput,
    SubmissionHistoryArguments,
    SubmissionHistoryResult,
    SubmissionReviewParameters,
    SubmissionReviewResult,
    TrainingCandidate,
    TrainingCandidatesArguments,
    TrainingCandidatesResult,
    TrainingExerciseReplacement,
    TrainingSetDraft,
)
from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.learning import (
    Difficulty,
    LearningEvidence,
    LearningParameters,
    TrainingGoalCoverage,
    feedback_priority,
    focused_exercise_count,
    practice_priority,
    practice_reason,
)
from kf_oj.contracts.learning_model import ModelTopic
from kf_oj.contracts.submission_evidence import CompilerCorrectionKind, SubmissionCodeEvidence
from kf_oj.runtime.tools import (
    SOURCE_EVIDENCE,
    SUBMISSION_HISTORY,
    TRAINING_CANDIDATES,
    ToolBudgetExceeded,
    ToolKey,
    ToolResultInvalid,
)

ToolCall = Callable[[ToolKey, BaseModel], Awaitable[BaseModel]]

_DETAIL_LIMITS = {"brief": 10, "normal": 25, "detailed": 50}
_LEVELS: tuple[Difficulty, ...] = ("easy", "medium", "hard")
_ORDINARY_FAILURES = frozenset(
    (
        "wrong_answer",
        "compilation_error",
        "runtime_error",
        "time_limit_exceeded",
        "memory_limit_exceeded",
        "output_limit_exceeded",
    )
)
_TOPIC_HINTS: dict[ModelTopic, str] = {
    "binary_search": "先说明答案一定留在哪个区间。每次缩小区间后，用一个最小样例检查这条约束。",
    "graph_traversal": "逐层找最短步数时用队列；需要探到底再回溯时用栈。记录已到访的顶点。",
    "dynamic_programming": "用一句话写明每个状态代表什么，再手算最小输入，检查初始条件和转移顺序。",
}
_CONCEPT_HINTS = {
    "wrong_answer": "手算一个最小样例，再检查边界条件与题目要求的输出格式。",
    "compilation_error": "从第一条编译诊断开始，检查标识符、类型和所选语言版本。",
    "runtime_error": "用可公开的最小输入检查下标、空输入和异常退出。",
    "time_limit_exceeded": "比较输入上界和最内层循环次数，检查是否可以减少重复工作。",
    "memory_limit_exceeded": "估算主要数据结构的元素数量和空间开销。",
    "output_limit_exceeded": "确认循环有终止条件，并核对是否误输出调试信息。",
}
_LOCAL_HINTS: dict[CompilerCorrectionKind, tuple[str, str]] = {
    "missing_semicolon": (
        "检查该行和上一条完整语句，在缺少结束分号的语句末尾补上分号。",
        "value = next_value;",
    ),
    "missing_colon": (
        "在该行的条件或循环头末尾检查冒号，并保持下一行作为缩进的语句体。",
        "if condition:",
    ),
    "expected_indented_block": (
        "找到冒号结束的控制语句或定义，将其语句体缩进一级，并与同一代码块对齐。",
        "if condition:\n    perform_step()",
    ),
    "unclosed_parenthesis": (
        "从该行的左括号开始逐层配对。在当前表达式结束处补上缺失的右括号。",
        "value = (first + second)",
    ),
}


class LearningEvidenceUnavailable(LookupError):
    """A specifically requested submission is outside the available evidence."""


def _compare(records: tuple[LearningEvidence, ...]) -> tuple[ReviewComparison, ...]:
    previous: dict[tuple[UUID, UUID], LearningEvidence] = {}
    comparisons = []
    for record in reversed(records):
        if record.state != "finished" or (
            record.verdict != "accepted" and record.verdict not in _ORDINARY_FAILURES
        ):
            continue
        key = record.problem_id, record.problem_version_id
        before = previous.get(key)
        previous[key] = record
        if before is None:
            continue
        outcome = "other"
        if before.verdict in _ORDINARY_FAILURES:
            if record.verdict == "accepted":
                outcome = "later_accepted"
            elif record.verdict in _ORDINARY_FAILURES:
                outcome = "later_non_accepted"
        comparisons.append(
            ReviewComparison(
                earlier_submission_id=before.submission_id,
                later_submission_id=record.submission_id,
                outcome=outcome,
            )
        )
    return tuple(comparisons)


def _exercise(
    candidate: TrainingCandidate, position: int, minutes: int | None, *, search: bool = False
) -> ProposedExercise:
    objective = "定期复习本题版本" if candidate.previously_accepted else "练习并核验所选目标"
    if candidate.timing is not None:
        objective = practice_reason(candidate.timing)
    return ProposedExercise(
        problem_id=candidate.problem_id,
        problem_version_id=candidate.problem_version_id,
        title=candidate.title,
        difficulty=candidate.difficulty,
        tags=candidate.tags,
        position=position,
        objective=objective,
        difficulty_score=candidate.difficulty_score,
        previously_accepted=candidate.previously_accepted if search else None,
        estimated_minutes=minutes,
        feedback_evidence=candidate.feedback_evidence,
        timing=candidate.timing,
        prerequisites=candidate.prerequisites,
    )


class LearningWorkflow:
    def __init__(self, invoke: ToolCall):
        self._invoke = invoke
        self._remaining = 4

    async def _call[Result: BaseModel](
        self, key: ToolKey, arguments: BaseModel, expected: type[Result]
    ) -> Result:
        if self._remaining == 0:
            raise ToolBudgetExceeded("learning workflow tool limit reached")
        self._remaining -= 1
        result = await self._invoke(key, arguments)
        if not isinstance(result, expected):
            raise ToolResultInvalid("unexpected learning tool result")
        return result

    async def _code(self, body: AgentLearningInput) -> SubmissionCodeEvidence | None:
        if body.review_question is not None:
            if not isinstance(body, StoredAgentLearningInput) or body.source_evidence is None:
                return None
        elif not body.include_source_evidence:
            return None
        evidence = await self._call(
            SOURCE_EVIDENCE, SourceEvidenceArguments(), SourceEvidenceResult
        )
        return evidence.evidence

    async def run(
        self,
        parameters: BaseModel,
        body: AgentLearningInput,
        *,
        review_anchor: UUID | None = None,
        training_source: FocusedTrainingResult | None = None,
    ) -> AgentLearningResult:
        if isinstance(parameters, SubmissionReviewParameters):
            records = await self._call(
                SUBMISSION_HISTORY,
                SubmissionHistoryArguments(
                    days=parameters.days,
                    limit=_DETAIL_LIMITS[parameters.detail],
                    problem_ids=parameters.problem_ids,
                    anchor_submission_id=review_anchor,
                ),
                SubmissionHistoryResult,
            )
            if review_anchor is not None and (
                not records.records or records.records[0].submission_id != review_anchor
            ):
                raise LearningEvidenceUnavailable("submission evidence unavailable")
            comparisons = _compare(records.records)
            code = await self._code(body)
            gaps = [] if comparisons else ["当前范围缺少同一题目版本的可比较评测。"]
            if (
                code is not None or (body.include_source_evidence and body.review_question is None)
            ) and (code is None or code.comparison is None):
                gaps.append("当前范围缺少可比较的同版本、同语言源码。")
            return SubmissionReviewResult(
                purpose=AgentPurpose.SUBMISSION_REVIEW,
                since=records.since,
                truncated=records.truncated,
                records=records.records,
                comparisons=comparisons,
                anchor_submission_id=review_anchor,
                code=code,
                gaps=tuple(gaps),
            )
        if isinstance(parameters, GuidedHelpParameters):
            return await self._guided_help(parameters, body)
        if isinstance(parameters, FocusedTrainingParameters):
            if body.training_set is not None and training_source is not None:
                return await self._revise_training(parameters, body.training_set, training_source)
            return await self._focused_training(parameters, body.training_set)
        if isinstance(parameters, PeriodicReviewParameters):
            return await self._periodic_review(parameters, body.periodic_review)
        raise ValueError("unsupported learning workflow")

    async def verify_review_evidence(
        self, parameters: SubmissionReviewParameters, target: LearningEvidence
    ) -> LearningEvidence | None:
        result = await self._call(
            SUBMISSION_HISTORY,
            SubmissionHistoryArguments(
                days=parameters.days,
                limit=1,
                problem_ids=parameters.problem_ids,
                submission_id=target.submission_id,
            ),
            SubmissionHistoryResult,
        )
        return next((record for record in result.records if record == target), None)

    async def _guided_help(
        self, parameters: GuidedHelpParameters, body: AgentLearningInput
    ) -> GuidedHelpResult:
        if body.model_topic is not None or body.chat is not None:
            evidence = None
            if body.include_model_verdict:
                history = await self._call(
                    SUBMISSION_HISTORY,
                    SubmissionHistoryArguments(days=90, limit=1, submission_id=body.submission_id),
                    SubmissionHistoryResult,
                )
                evidence = next(iter(history.records), None)
                if evidence is None:
                    raise LearningEvidenceUnavailable("submission evidence unavailable")
            return GuidedHelpResult(
                purpose=AgentPurpose.GUIDED_HELP,
                requested_level="concept",
                hint=_TOPIC_HINTS[body.model_topic] if body.model_topic else "",
                evidence=evidence,
                gaps=(),
            )
        code = await self._code(body)
        selected = code.source.submission_id if code is not None else body.submission_id
        history = await self._call(
            SUBMISSION_HISTORY,
            SubmissionHistoryArguments(days=90, limit=1, submission_id=selected),
            SubmissionHistoryResult,
        )
        evidence = next(iter(history.records), None)
        if evidence is None and body.submission_id is not None:
            raise LearningEvidenceUnavailable("submission evidence unavailable")
        hint = "用题目公开的样例复述输入、输出和边界条件，再检查解题思路。"
        gaps = []
        if evidence is None:
            gaps.append("当前时间范围没有可引用的提交。")
        elif evidence.state == "finished":
            hint = _CONCEPT_HINTS.get(evidence.verdict, hint)
        else:
            gaps.append("该提交没有可用于定位学生错误的终态判定。")
        delivered = "concept"
        example = None
        if parameters.hint_level != "concept":
            if code is not None and code.compiler_locations:
                location = code.compiler_locations[0]
                delivered = "location"
                hint = f"诊断指向源码第 {location.line} 行。先对照该行及其前后的结构和类型。"
                if parameters.hint_level == "local_change":
                    correction = code.compiler_correction
                    if (
                        correction is not None
                        and evidence is not None
                        and evidence.state == "finished"
                        and evidence.verdict == "compilation_error"
                        and evidence.evaluation_id == code.source.evaluation_id
                    ):
                        action, sample = _LOCAL_HINTS[correction.kind]
                        delivered = "local_change"
                        hint = f"编译诊断指向第 {correction.location.line} 行。{action}"
                        if parameters.allow_code_examples:
                            example = sample
                    else:
                        gaps.append("目前有可核对的位置线索，缺少足以支持局部修改的诊断。")
            else:
                gaps.append("当前流程只能提供概念提醒，缺少可核对的代码位置。")
        return GuidedHelpResult(
            purpose=AgentPurpose.GUIDED_HELP,
            requested_level=parameters.hint_level,
            delivered_level=delivered,
            hint=hint,
            hint_example=example,
            evidence=evidence,
            code=code,
            gaps=tuple(gaps),
        )

    async def _revise_training(
        self,
        defaults: FocusedTrainingParameters,
        draft: TrainingSetDraft,
        source: FocusedTrainingResult,
    ) -> FocusedTrainingResult:
        assert draft.revision is not None
        position = draft.revision.replace_position
        no_replacement = "当前条件下没有可替换的题目，保留原题单。"
        gaps = tuple(gap for gap in source.gaps if gap != no_replacement)
        changes = {
            "goal": draft.title,
            "training_set": draft,
            "replacement": None,
            "gaps": gaps,
            "model_hint": None,
            "model_status": None,
        }
        if position is None:
            return source.model_copy(update=changes)

        parameters = draft.parameters_for(defaults)
        current = next(item for item in source.items if item.position == position)
        retained = tuple(item for item in source.items if item.position != position)
        loss = TrainingGoalCoverage.from_tags(
            parameters.tags, (item.tags for item in retained)
        ).missing
        needed = tuple(tag for tag in loss if tag in current.tags)
        allowed = _LEVELS[
            _LEVELS.index(parameters.minimum_difficulty) : _LEVELS.index(
                parameters.maximum_difficulty
            )
            + 1
        ]
        cover_each_goal = parameters.cover_each_goal and bool(parameters.tags)
        choices = await self._call(
            TRAINING_CANDIDATES,
            TrainingCandidatesArguments(
                tags=(needed or parameters.tags) if cover_each_goal else parameters.tags,
                count=1,
                cover_each_goal=cover_each_goal and not needed,
                allowed_difficulties=allowed,
                ranking_profile=parameters.ranking_profile,
                exclude_problem_ids=tuple(item.problem_id for item in source.items),
            ),
            TrainingCandidatesResult,
        )
        if not choices.items:
            changes["gaps"] = (*gaps, no_replacement)
            return source.model_copy(update=changes)
        replacement = _exercise(choices.items[0], position, current.estimated_minutes)
        items = tuple(replacement if item.position == position else item for item in source.items)
        changes["items"] = items
        changes["replacement"] = TrainingExerciseReplacement(
            position=position, previous_title=current.title
        )
        if cover_each_goal:
            changes["goal_coverage"] = TrainingGoalCoverage.from_tags(
                parameters.tags, (item.tags for item in items)
            )
        return source.model_copy(update=changes)

    async def _focused_training(
        self, parameters: FocusedTrainingParameters, draft: TrainingSetDraft | None = None
    ) -> FocusedTrainingResult:
        if draft is not None and draft.search is not None:
            found = await self._call(
                TRAINING_CANDIDATES,
                TrainingCandidatesArguments(
                    tags=draft.tags,
                    count=draft.count,
                    difficulty=draft.difficulty,
                    search=draft.search,
                ),
                TrainingCandidatesResult,
            )
            gaps = []
            if len(found.items) < draft.count:
                gaps.append("符合筛选条件的当前题目不足。")
            if found.candidates_truncated:
                gaps.append("题库候选超过本次查询上限。")
            return FocusedTrainingResult(
                purpose=AgentPurpose.FOCUSED_TRAINING,
                goal=draft.title,
                daily_minutes=parameters.daily_minutes,
                items=tuple(
                    _exercise(item, index, None, search=True)
                    for index, item in enumerate(found.items, start=1)
                ),
                gaps=tuple(gaps),
                training_set=draft,
            )
        count = draft.count if draft else focused_exercise_count(parameters.daily_minutes)
        if draft:
            parameters = draft.parameters_for(parameters)
        levels = _LEVELS[
            _LEVELS.index(parameters.minimum_difficulty) : _LEVELS.index(
                parameters.maximum_difficulty
            )
            + 1
        ]
        choices: list[TrainingCandidate] = []
        truncated = False
        prerequisites: dict[str, None] = {}
        goal_coverage = None
        cover_each_goal = parameters.cover_each_goal and bool(parameters.tags)
        queries = (
            (
                TrainingCandidatesArguments(
                    tags=parameters.tags,
                    count=count,
                    cover_each_goal=cover_each_goal,
                    allowed_difficulties=levels,
                    ranking_profile=parameters.ranking_profile,
                ),
            )
            if draft or cover_each_goal or parameters.ranking_profile is not None
            else tuple(
                LearningParameters(tags=parameters.tags, difficulty=difficulty, count=count)
                for difficulty in levels
            )
        )
        for arguments in queries:
            result = await self._call(
                TRAINING_CANDIDATES,
                arguments,
                TrainingCandidatesResult,
            )
            choices.extend(result.items)
            truncated |= result.candidates_truncated
            if result.prerequisite_gap is not None:
                prerequisites[result.prerequisite_gap.message()] = None
            goal_coverage = result.goal_coverage
        if draft is None and not cover_each_goal and parameters.ranking_profile is None:
            choices.sort(
                key=lambda candidate: (
                    practice_priority(candidate.previously_accepted, candidate.timing),
                    feedback_priority(candidate.difficulty, candidate.feedback_evidence),
                )
            )
        unique: dict[UUID, TrainingCandidate] = {}
        for candidate in choices:
            unique.setdefault(candidate.problem_id, candidate)
        selected = list(unique.values())[:count]
        gaps = list(prerequisites)
        if len(selected) < count:
            gaps.append("符合知识点和难度范围的可用题目不足。")
        if truncated:
            gaps.append("题库候选超过查询上限。")
        return FocusedTrainingResult(
            purpose=AgentPurpose.FOCUSED_TRAINING,
            goal=parameters.goal,
            daily_minutes=parameters.daily_minutes,
            items=tuple(
                _exercise(candidate, i, max(1, parameters.daily_minutes // len(selected)))
                for i, candidate in enumerate(selected, start=1)
            ),
            gaps=tuple(gaps),
            goal_coverage=goal_coverage,
            ranking_profile=parameters.ranking_profile,
            training_set=draft,
        )

    async def _periodic_review(
        self, parameters: PeriodicReviewParameters, request: PeriodicReviewRequest | None
    ) -> PeriodicReviewResult:
        evidence = ()
        if request is None:
            history = await self._call(
                SUBMISSION_HISTORY,
                SubmissionHistoryArguments(days=parameters.cycle_days),
                SubmissionHistoryResult,
            )
            evidence = history.records
        minutes = None if request is None else request.available_minutes
        tags = () if request is None else request.tags
        count = parameters.sessions_per_week if minutes is None else focused_exercise_count(minutes)
        candidates = await self._call(
            TRAINING_CANDIDATES,
            LearningParameters(tags=tags, count=count, ranking_profile=parameters.ranking_profile),
            TrainingCandidatesResult,
        )
        gaps = []
        if candidates.prerequisite_gap is not None:
            gaps.append(candidates.prerequisite_gap.message())
        if request is None and not evidence:
            gaps.append("本复习周期暂无提交证据，先收集诊断结果。")
        if len(candidates.items) < count:
            gaps.append("当前可用复习题不足。")
        estimate = None if minutes is None else minutes // max(1, len(candidates.items))
        return PeriodicReviewResult(
            purpose=AgentPurpose.PERIODIC_REVIEW,
            goal=parameters.stage_goal,
            cycle_days=parameters.cycle_days,
            sessions_per_week=parameters.sessions_per_week,
            available_minutes=minutes,
            tags=tags,
            evidence=evidence,
            items=tuple(
                _exercise(candidate, i, estimate) for i, candidate in enumerate(candidates.items, 1)
            ),
            gaps=tuple(gaps),
            ranking_profile=parameters.ranking_profile,
        )
