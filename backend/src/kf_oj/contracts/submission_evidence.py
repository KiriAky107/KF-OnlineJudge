"""Privately scoped source locations and structural comparison evidence."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kf_oj.contracts.judging import Counter, Identifier, Measurement, WireModel
from kf_oj.contracts.judging_v2 import Language


class CodeSubmissionRef(WireModel):
    submission_id: Identifier
    evaluation_id: Identifier
    problem_version_id: Identifier
    language: Language


MAX_SOURCE_EXCERPT_LINES = 24
MAX_SOURCE_EXCERPT_BYTES = 2048


class SourceLineRange(WireModel):
    start_line: Counter
    end_line: Counter

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if not self.start_line <= self.end_line < self.start_line + MAX_SOURCE_EXCERPT_LINES:
            raise ValueError("invalid excerpt line range")
        return self


class SubmissionSourceExcerpt(SourceLineRange):
    source: CodeSubmissionRef
    text: Annotated[str, Field(max_length=MAX_SOURCE_EXCERPT_BYTES, repr=False)]


class CompilerLocation(WireModel):
    line: Counter
    column: Counter | None = None
    kind: Literal["error", "warning", "note", "location"]


CompilerCorrectionKind = Literal[
    "missing_semicolon", "missing_colon", "expected_indented_block", "unclosed_parenthesis"
]


class CompilerCorrection(WireModel):
    location: CompilerLocation
    kind: CompilerCorrectionKind


class CodeDiffRegion(WireModel):
    kind: Literal["replace", "insert", "delete"]
    before_line: Counter
    before_count: Measurement
    after_line: Counter
    after_count: Measurement


ControlKeywordCount = Annotated[int, Field(strict=True, ge=0, le=16384)]


class CodeControlFlowCounts(WireModel):
    branch_keywords: ControlKeywordCount
    loop_keywords: ControlKeywordCount
    return_keywords: ControlKeywordCount


class CodeControlFlowDiff(WireModel):
    method: Literal["control-keywords-v1"] = "control-keywords-v1"
    before: CodeControlFlowCounts
    after: CodeControlFlowCounts


ComparisonOperator = Literal["<", "<=", ">", ">=", "==", "!="]
ComparisonCase = Literal["less", "equal", "greater"]


class CodeBoundaryChange(WireModel):
    before_line: Counter
    after_line: Counter
    before_operator: ComparisonOperator
    after_operator: ComparisonOperator
    changed_cases: Annotated[tuple[ComparisonCase, ...], Field(max_length=3)] = Field(
        default=(), exclude_if=lambda value: not value
    )


class CodeDiffEvidence(WireModel):
    earlier: CodeSubmissionRef
    regions: Annotated[tuple[CodeDiffRegion, ...], Field(max_length=16)]
    truncated: bool
    control_flow: CodeControlFlowDiff | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    boundary_changes: Annotated[tuple[CodeBoundaryChange, ...], Field(max_length=8)] = Field(
        default_factory=tuple, exclude_if=lambda value: not value
    )


class SubmissionCodeEvidence(WireModel):
    source: CodeSubmissionRef
    source_available: bool
    line_count: Measurement | None
    compiler_locations: Annotated[tuple[CompilerLocation, ...], Field(max_length=8)]
    diagnostics_truncated: bool
    comparison: CodeDiffEvidence | None = None
    compiler_correction: CompilerCorrection | None = None
    method: Literal["kf-code-evidence-v1"] = "kf-code-evidence-v1"
