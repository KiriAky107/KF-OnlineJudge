from typing import Literal

from pydantic import AwareDatetime

from kf_oj.contracts.judging import WireModel

DiagnosticMethod = Literal["optimization", "ubsan", "differential", "boundary"]
DiagnosticCohort = Literal["all", "linked_review", "unlinked"]


class DiagnosticObservationCount(WireModel):
    method: DiagnosticMethod
    observed_count: int
    untracked_count: int
    eligible_count: int
    finding_count: int
    mean_claim_to_finish_ms: int | None
    max_sandbox_budget_us: int | None


class DiagnosticObservations(WireModel):
    start_at: AwareDatetime
    end_at: AwareDatetime
    cohort: DiagnosticCohort
    items: tuple[DiagnosticObservationCount, ...]


TrainingDraftPurpose = Literal["focused_training", "periodic_review"]
PracticeCohort = Literal["direct", "assistant_saved", "shared"]
SelfTestCohort = Literal["direct_sample", "diagnostic_sample"]


class TrainingDraftCount(WireModel):
    purpose: TrainingDraftPurpose
    draft_count: int
    saved_count: int


class PracticeAcceptanceCount(WireModel):
    cohort: PracticeCohort
    exercise_count: int
    attempted_count: int
    accepted_count: int


class SelfTestComparisonCount(WireModel):
    cohort: SelfTestCohort
    completed_count: int
    compared_count: int
    mismatch_count: int


class PracticeObservations(WireModel):
    start_at: AwareDatetime
    end_at: AwareDatetime
    follow_up_days: int
    drafts: tuple[TrainingDraftCount, ...]
    practice: tuple[PracticeAcceptanceCount, ...]
    self_tests: tuple[SelfTestComparisonCount, ...]
